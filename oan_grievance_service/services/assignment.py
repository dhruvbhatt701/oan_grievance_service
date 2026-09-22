"""STG-337 assignment and reassignment.

Initial assignment and a later move to another department both go through the
Category+Department routing rule (`Grievance Routing Rule`) and the RBAC desk
that holds officers for that department. Reassignment does not wait for approval
and does not restart the SLA clock.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.permissions import can_assign, can_reassign
from oan_grievance_service.services import audit, lifecycle, notifications, routing
from oan_grievance_service.services import constants as C

# Fields that define the running clock. A reassignment copies these back if a
# workflow save touched them, so the window the case already had keeps running.
SLA_CLOCK_FIELDS = (
	"sla_days",
	"sla_start_at",
	"sla_due_date",
	"reminder_50_sent",
	"reminder_80_sent",
	"next_escalation_at",
	"total_hold_time",
	"on_hold_since",
)

REASSIGNABLE_STATUSES = frozenset({C.ASSIGNED, C.IN_PROGRESS})


def initial_assign(grievance):
	"""Route a Submitted grievance by the Category+Department rule and start work."""
	if not can_assign():
		audit.log_denied("assign", grievance=grievance.name)
		frappe.throw(
			_("Only a grievance officer or an administrator can assign a grievance."),
			frappe.PermissionError,
			title=_("Assignment Not Permitted"),
		)

	if grievance.status != C.SUBMITTED:
		frappe.throw(
			_("Initial assignment is only available while the grievance is Submitted."),
			frappe.ValidationError,
			title=_("Not Awaiting Assignment"),
		)
	if grievance.assigned_dept:
		frappe.throw(
			_("This grievance is already assigned. Reassign it to move it to another department."),
			frappe.ValidationError,
			title=_("Already Assigned"),
		)

	rule = routing.find_matching_rule(grievance)
	if not rule:
		frappe.throw(
			_("No routing rule matches this grievance's category and area."),
			frappe.ValidationError,
			title=_("No Routing Rule"),
		)

	officer = routing.resolve_officer(grievance, rule.assigned_dept)
	_write_assignment(grievance, rule.assigned_dept, officer, rule.name)
	# apply_workflow reloads the document and drops ignore_permissions, so the
	# save runs as Administrator. The timeline below still names the caller.
	actor = _actor()
	lifecycle.assign(
		grievance,
		note=f"Assigned by routing rule {rule.name}" + (f" ({actor})" if actor else ""),
		automated=True,
	)

	grievance.reload()
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="assignment",
		is_internal=False,
		body=_assignment_body("Assigned", rule.assigned_dept, officer, rule.name),
		author_user=_actor(),
		ref_doctype="Grievance Routing Rule",
		ref_docname=rule.name,
	)
	notifications.queue(grievance, C.EVENT_ASSIGNED_MANUAL)
	return _payload(grievance, rule_name=rule.name)


def reassign(grievance, department, reason, officer=None):
	"""Move the case to another department using the same routing rule. SLA keeps running."""
	if not can_reassign(grievance):
		audit.log_denied("reassign", grievance=grievance.name)
		frappe.throw(
			_("Only the assigned officer or an administrator can reassign this grievance."),
			frappe.PermissionError,
			title=_("Reassignment Not Permitted"),
		)

	department = (department or "").strip()
	reason = (reason or "").strip()
	officer = (officer or "").strip() or None

	if grievance.status not in REASSIGNABLE_STATUSES:
		frappe.throw(
			_("A grievance can be reassigned only while it is Assigned or In Progress."),
			frappe.ValidationError,
			title=_("Reassignment Not Available"),
		)
	if len(reason) < 5:
		frappe.throw(
			_("A reason of at least 5 characters is required to reassign a grievance."),
			frappe.ValidationError,
			title=_("Reason Required"),
		)
	if not frappe.db.exists("Grievance Department", department):
		frappe.throw(
			_("Department {0} does not exist.").format(department),
			frappe.ValidationError,
			title=_("Unknown Department"),
		)
	if department == grievance.assigned_dept:
		frappe.throw(
			_("Reassignment must move the grievance to a different department."),
			frappe.ValidationError,
			title=_("Same Department"),
		)

	rule = routing.find_matching_rule(grievance, department=department)
	if not rule:
		frappe.throw(
			_("No routing rule sends {0} grievances in this area to {1}.").format(
				grievance.service_category, department
			),
			frappe.ValidationError,
			title=_("No Routing Rule"),
		)

	target_officer = routing.resolve_officer(grievance, department, officer=officer)
	prior_department = grievance.assigned_dept
	prior_officer = grievance.assigned_to
	clock = _snapshot_clock(grievance)

	# Move the state while the current officer still holds the case. apply_workflow
	# reloads the row and checks write permission against whoever is assigned, so
	# the new department and officer are written after that save.
	if grievance.status == C.IN_PROGRESS:
		lifecycle.transition(
			grievance,
			C.ACTION_REFER_ONWARD,
			reason=reason,
			note=f"Reassigned to {department} by rule {rule.name}",
		)

	_write_assignment(grievance, department, target_officer, rule.name)

	grievance.reload()
	_restore_clock(grievance, clock)

	request = _audit_request(
		grievance,
		prior_department=prior_department,
		prior_officer=prior_officer,
		department=department,
		officer=target_officer,
		reason=reason,
	)
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="assignment",
		is_internal=False,
		body=_assignment_body("Reassigned", department, target_officer, rule.name, reason=reason)
		+ f" SLA continues (due {clock.get('sla_due_date') or 'not started'}).",
		author_user=_actor(),
		ref_doctype="Grievance Reassignment Request",
		ref_docname=request.name,
	)
	notifications.queue(grievance, C.EVENT_ASSIGNED_MANUAL)

	grievance.reload()
	payload = _payload(grievance, rule_name=rule.name)
	payload["audit"] = {"name": request.name, "auto_routed": 1, "sla_treatment": "Continue"}
	return payload


def _write_assignment(grievance, department, officer, rule_name):
	grievance.db_set("assigned_dept", department, update_modified=False)
	grievance.db_set("assigned_to", officer, update_modified=False)
	grievance.db_set("routing_rule", rule_name, update_modified=False)
	grievance.db_set("routed_automatically", 1, update_modified=False)


def _snapshot_clock(grievance):
	return {field: grievance.get(field) for field in SLA_CLOCK_FIELDS}


def _restore_clock(grievance, snapshot):
	"""Put a clock that was already running back where it was. Do not invent one."""
	if not snapshot.get("sla_due_date"):
		return
	for field, value in snapshot.items():
		if _same(grievance.get(field), value):
			continue
		grievance.db_set(field, value, update_modified=False)


def _same(left, right):
	if left == right:
		return True
	return str(left or "") == str(right or "")


def _audit_request(grievance, *, prior_department, prior_officer, department, officer, reason):
	request = frappe.get_doc(
		{
			"doctype": "Grievance Reassignment Request",
			"grievance": grievance.name,
			"decision": "Approved",
			"auto_routed": 1,
			"requested_at": now_datetime(),
			"initiated_by": _actor() or "Administrator",
			"prior_department": prior_department,
			"prior_officer": prior_officer,
			"target_department": department,
			"target_officer": officer,
			"reason": reason,
			"sla_treatment": "Continue",
		}
	)
	request.insert(ignore_permissions=True)
	return request


def _assignment_body(verb, department, officer, rule_name, reason=None):
	body = f"{verb} to {department}"
	if officer:
		body += f" ({officer})"
	body += f" by routing rule {rule_name}"
	if reason:
		body += f": {reason}"
	return body


def _actor():
	user = frappe.session.user
	return user if user and user != "Guest" else None


def _payload(grievance, rule_name):
	from oan_grievance_service.services import sla

	return {
		"ticket_number": grievance.ticket_number,
		"status": grievance.status,
		"assignment": {
			"department": grievance.assigned_dept,
			"assigned_to": grievance.assigned_to,
			"routing_rule": rule_name,
			"routed_automatically": bool(grievance.routed_automatically),
		},
		"sla": {
			"sla_days": grievance.sla_days,
			"sla_start_at": grievance.sla_start_at,
			"sla_due_date": grievance.sla_due_date,
			"sla_consumed_percent": sla.consumed_percent(grievance),
		},
	}
