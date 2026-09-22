"""Document event handlers registered in hooks.py.

These are the joins between a saved record and the workflow the FSD describes, kept
out of the doctype controllers so the sequence is readable in one place.
"""

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle, notifications, sla

# Workflow moves
# --------------
# Frappe's engine drives a move by saving, submitting or cancelling the Grievance, so
# the Grievance controller calls these from that save: `before_workflow_action` from
# validate, where a throw abandons the move, and `after_workflow_action` once the new
# state is written. Between them they are the whole audit and side-effect layer; a
# desk button, an API call and a scheduled job all arrive here the same way.


def before_workflow_action(doc, from_state):
	"""The guards a move must pass, evaluated before it is written.

	The role check is not here: the Workflow's own `allowed` column settles who may
	take an action, and Frappe refuses the rest before this runs.
	"""
	to_state = doc.workflow_state
	context = frappe.flags.grievance_transition or frappe._dict()

	# FSD 3.4 / 3.6: the history row will refuse a rejection or reopen with no
	# reason; asking its rule here refuses the move before anything is written.
	from oan_grievance_service.grievance_management.doctype.grievance_status_history.grievance_status_history import (
		require_reason,
	)

	require_reason(from_state, to_state, context.reason)

	# FSD D-2: a case only reaches the submitter, or goes back to them for more
	# detail, on the strength of a formal response saying so.
	if to_state == C.PENDING_SUBMITTER and not _latest_response_is(doc, "Resolved", "Partially Resolved"):
		frappe.throw(
			_("A grievance reaches Pending Submitter only on a Resolved or Partially Resolved response."),
			title=_("Response Required"),
		)
	if to_state == C.MORE_INFO_NEEDED and not (
		_latest_response_is(doc, "Requires further info") or _has_open_info_request(doc)
	):
		frappe.throw(
			_("Ask the submitter a question before moving the grievance to More Info Needed."),
			title=_("Information Request Required"),
		)

	# FSD 4.1 step 7 / 4.2 step 1: work starts in a department, never nowhere.
	if to_state == C.IN_PROGRESS and not doc.assigned_dept:
		frappe.throw(
			_("Assign the grievance to a department before work on it starts."),
			title=_("Assignment Required"),
		)

	# Evidence before a resolution is a per-site rule, off unless the site turns it
	# on: a farmer reporting a missing payment often has nothing to attach.
	if (
		to_state == C.PENDING_SUBMITTER
		and frappe.conf.get("grievance_require_evidence_before_resolution")
		and not frappe.db.count("Grievance Attachment", {"grievance": doc.name})
	):
		frappe.throw(
			_("Attach supporting evidence before recording a resolution."),
			title=_("Evidence Required"),
		)


def after_workflow_action(doc, from_state):
	"""Record the move and carry out what the FSD attaches to arriving in a state."""
	context = frappe.flags.grievance_transition or frappe._dict()
	to_state = doc.workflow_state

	# A desk button arrives with no context; the Workflow still knows which
	# action joins the two states, so the trail names it either way.
	if not context.action:
		context.action = _action_between(doc, from_state, to_state)

	if to_state == C.SUBMITTED:
		_record_submission(doc)

	user = None if context.automated else frappe.session.user
	if user == "Guest":
		user = None

	# Refuses, and with it the whole move, when the FSD wants a reason and none came.
	history = frappe.get_doc(
		{
			"doctype": "Grievance Status History",
			"grievance": doc.name,
			"from_status": from_state,
			"to_status": to_state,
			"transition": context.action,
			"closure_type": context.closure_type,
			"is_automated": 1 if context.automated else 0,
			"changed_by": user,
			"timestamp": now_datetime(),
			"reason": context.reason,
			"notes": context.reason or context.note,
		}
	).insert(ignore_permissions=True)
	context.history = history

	timeline_body = f"Status changed from {from_state} to {to_state}"
	if context.reason:
		timeline_body += f": {context.reason}"
	elif context.note:
		timeline_body += f" ({context.note})"
	GrievanceTimeline.record(
		grievance=doc.name,
		entry_type="status_change",
		is_internal=False,
		body=timeline_body,
		author_user=user,
		ref_doctype="Grievance Status History",
		ref_docname=history.name,
	)

	# FSD 4.2 step 1: the SLA clock starts when the case reaches a department.
	if to_state == C.ASSIGNED:
		sla.start_clock(doc)

	# The clock stops while the case waits on the submitter and the deadline is
	# pushed out by the hold when they reply. A response says which it wants
	# (Grievance Response.sla_behaviour); every other move reads the site's list.
	# Terminal states freeze the clock as it stands: nothing resumes it.
	paused = sla.paused_statuses()
	if to_state in C.TERMINAL_STATUSES:
		pass
	elif context.sla_behaviour == "paused" or (not context.sla_behaviour and to_state in paused):
		sla.pause_clock(doc)
	elif context.sla_behaviour == "running" or (not context.sla_behaviour and from_state in paused):
		sla.resume_clock(doc)

	# FSD 3.6: entering Pending Submitter opens the confirmation window.
	if to_state == C.PENDING_SUBMITTER:
		doc.db_set(
			"confirmation_deadline",
			add_days(now_datetime(), lifecycle.confirmation_window_days()),
			update_modified=False,
		)

	if context.get("notify", True):
		event = lifecycle.STATUS_EVENT.get(to_state)
		if event:
			notifications.queue(doc, event)


def _record_submission(doc):
	"""FSD 4.1 step 5: the first timeline entry. Routing and the acknowledgement are
	the intake API's next steps, not this hook's, so a test that submits a fixture
	does not route it."""
	user = frappe.session.user if frappe.session.user != "Guest" else None
	GrievanceTimeline.record(
		grievance=doc.name,
		entry_type="status_change",
		is_internal=False,
		body=f"Grievance submitted ({doc.ticket_number})",
		author_submitter=doc.submitter,
		author_user=doc.assisted_by_officer or user,
		ref_doctype="Grievance",
		ref_docname=doc.name,
	)


def _action_between(doc, from_state, to_state):
	from frappe.model.workflow import get_workflow

	for row in get_workflow(doc.doctype).transitions:
		if row.state == from_state and row.next_state == to_state:
			return row.action
	return None


def _latest_response_is(doc, *response_types):
	latest = frappe.get_all(
		"Grievance Response",
		filters={"grievance": doc.name},
		fields=["response_type"],
		order_by="response_date desc, creation desc",
		limit=1,
	)
	return bool(latest) and latest[0].response_type in response_types


def _has_open_info_request(doc):
	return bool(frappe.db.exists("Grievance Timeline", {"grievance": doc.name, "entry_type": "info_request"}))


def response_after_insert(doc, method=None):
	"""FSD 3.5 and Appendix D-2: the response outcome drives the next status."""
	grievance = frappe.get_doc("Grievance", doc.grievance)

	# D-3: response_date, responded_by, sequence and prior_status are filled in by the
	# controller before validation, because they are mandatory. The IP is captured here
	# because it is only meaningful for a request that actually reached the server.
	if getattr(frappe.local, "request_ip", None):
		doc.db_set("ip_address", frappe.local.request_ip, update_modified=False)

	# Record formal response in unified timeline spine
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="response",
		is_internal=False,
		body=doc.resolution_summary or doc.action_taken or f"Formal Response ({doc.response_type})",
		author_user=doc.responded_by or frappe.session.user,
		ref_doctype="Grievance Response",
		ref_docname=doc.name,
	)

	# D-2: the outcome names the move; the Workflow decides whether it is open from
	# here (a response filed against a case that is not In Progress is refused).
	action = C.RESPONSE_OUTCOME_ACTION.get(doc.response_type)
	if action and action in lifecycle.actions_available(grievance):
		lifecycle.transition(
			grievance,
			action,
			note=f"Response {doc.name} ({doc.response_type})",
			sla_behaviour=doc.sla_behaviour,
		)

	doc.db_set("new_status", grievance.status, update_modified=False)

	# FSD 4.3: a structured response clears the escalation flag.
	sla.clear_escalation(grievance)

	notifications.queue(grievance, C.EVENT_RESPONSE_SENT)
	doc.db_set("notification_sent", 1, update_modified=False)
	doc.db_set("notification_sent_at", now_datetime(), update_modified=False)


def reassignment_on_update(doc, method=None):
	"""FSD 3.3.1: the target office gets no rights until L2 approves.

	The reassignment is committed here, on approval, and nowhere else, which is what
	makes 'the reassigned officer may act only after the approved assignment is
	committed' true rather than aspirational.

	STG-337 auto-routes through the assignment API and writes the request already
	committed (`auto_routed`). That path has no approval step; the service has
	already moved the case and kept the SLA clock, so this hook must not apply
	the change again or reset the clock.
	"""
	from oan_grievance_service.permissions import can_approve_reassignment

	if doc.auto_routed:
		return

	if doc.decision == "Pending":
		notifications.queue(frappe.get_doc("Grievance", doc.grievance), C.EVENT_REASSIGNMENT_REQUESTED)
		return

	if doc.get_doc_before_save() and doc.get_doc_before_save().decision != "Pending":
		return

	if not can_approve_reassignment():
		frappe.throw(
			_("Only a supervising officer may approve or reject a reassignment."),
			title=_("Approval Not Permitted"),
		)

	doc.db_set("decided_at", now_datetime(), update_modified=False)
	doc.db_set("approver", frappe.session.user, update_modified=False)

	if doc.decision != "Approved":
		return

	grievance = frappe.get_doc("Grievance", doc.grievance)
	grievance.db_set("assigned_dept", doc.target_department, update_modified=False)
	if doc.target_officer:
		grievance.db_set("assigned_to", doc.target_officer, update_modified=False)

	# FSD 3.3.1: SLA treatment follows configured policy and is never implicit.
	# Appendix D-2 assumes a reset for a referral; 3.3.1 makes it a decision. The
	# field carries that decision, and an unset field means the clock continues.
	if doc.sla_treatment == "Reset":
		grievance.db_set("sla_due_date", None, update_modified=False)
		grievance.db_set("sla_start_at", None, update_modified=False)
		grievance.db_set("reminder_50_sent", 0, update_modified=False)
		grievance.db_set("reminder_80_sent", 0, update_modified=False)
		sla.start_clock(grievance)

	# Record assignment event in unified timeline (not as a fake status change)
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="assignment",
		is_internal=False,
		body=f"Reassigned to {doc.target_department}"
		+ (f" ({doc.target_officer})" if doc.target_officer else "")
		+ f" (SLA: {doc.sla_treatment or 'Continue'})",
		author_user=frappe.session.user,
		ref_doctype="Grievance Reassignment Request",
		ref_docname=doc.name,
	)


def deferral_on_update(doc, method=None):
	"""FSD 3.11.7: an approved deferral extends the SLA window."""
	from oan_grievance_service.permissions import can_approve_deferral

	if doc.status == "Pending":
		return
	before = doc.get_doc_before_save()
	if before and before.status != "Pending":
		return

	if not can_approve_deferral(assignee=frappe.db.get_value("Grievance", doc.grievance, "assigned_to")):
		frappe.throw(
			_("Only a supervising officer may decide a deferral."),
			title=_("Approval Not Permitted"),
		)

	doc.db_set("approver", frappe.session.user, update_modified=False)
	doc.db_set("decided_at", now_datetime(), update_modified=False)

	if doc.status != "Approved":
		return

	from oan_grievance_service.grievance_sla.doctype.grievance_deferral_policy.grievance_deferral_policy import (
		max_deferral_days,
	)

	max_days = max_deferral_days()
	if doc.additional_days > max_days:
		frappe.throw(
			_("A deferral may not exceed {0} days.").format(max_days),
			title=_("Deferral Too Long"),
		)

	grievance = frappe.get_doc("Grievance", doc.grievance)
	sla.extend_for_deferral(grievance, doc.additional_days)


def anonymity_on_update(doc, method=None):
	"""FSD 9.2: approval masks the submitter from department officers."""
	if doc.status == "Pending":
		return
	before = doc.get_doc_before_save()
	if before and before.status != "Pending":
		return

	doc.db_set("decided_by", frappe.session.user, update_modified=False)
	doc.db_set("decided_at", now_datetime(), update_modified=False)

	grievance = frappe.get_doc("Grievance", doc.grievance)
	grievance.db_set("anonymity_status", doc.status, update_modified=False)

	if doc.status == "Approved":
		grievance.db_set("is_anonymous", 1, update_modified=False)
		grievance.db_set("anonymity_approved_by", frappe.session.user, update_modified=False)
	elif doc.status == "Rejected":
		grievance.db_set("is_anonymous", 0, update_modified=False)
		lifecycle.reject(grievance, "Anonymity refused and identity not disclosed", automated=True)


def on_user_registered(user_doc, role=None, roles=None, **kwargs):
	"""Handle user registration event broadcast from oan_auth_service.

	If 'Grievance Submitter' is among the assigned roles:
	1. Resolves submitter_type (defaulting to 'Individual Farmer').
	2. Validates that the submitter type exists.
	3. Derives and validates the canonical dedupe_key based on scheme rules.
	4. Populates general contact and submitter-type-specific fields.
	5. Creates or updates and links the Submitter Profile record to the User.
	"""
	from oan_grievance_service.grievance_masters.doctype.grievance_submitter_profile.grievance_submitter_profile import (
		build_dedupe_key,
	)
	from oan_grievance_service.services import identity

	assigned_roles = set(roles or [])
	if role:
		assigned_roles.add(role)

	# Only process if user is registering as a Grievance Submitter
	if "Grievance Submitter" not in assigned_roles:
		return None

	submitter_type = (kwargs.get("submitter_type") or "Individual Farmer").strip()

	if not frappe.db.exists("Grievance Submitter Type", submitter_type):
		frappe.throw(
			_("Submitter Type '{0}' does not exist.").format(submitter_type),
			frappe.ValidationError,
		)

	submitter_name = (
		kwargs.get("submitter_name")
		or kwargs.get("full_name")
		or f"{user_doc.first_name or ''} {user_doc.last_name or ''}".strip()
		or user_doc.name
	)

	contact_mobile = (
		kwargs.get("contact_mobile") or kwargs.get("phone_number") or user_doc.mobile_no or ""
	).strip()

	contact_email = (kwargs.get("contact_email") or kwargs.get("email") or "").strip() or None

	if not contact_email and user_doc.email and not user_doc.email.endswith("@id.openagrinet.internal"):
		contact_email = user_doc.email

	# Notification language belongs on the User record, the one identity primitive shared by
	# submitters and staff. Guarded because a bench need not have the Language record seeded.
	preferred_language = kwargs.get("preferred_language")
	if preferred_language and frappe.db.exists("Language", preferred_language):
		user_doc.db_set("language", preferred_language, update_modified=False)

	# Automatically derive dedupe_key from inputs (fayda_id, registration_number, farmer_id, phone, etc.)
	dedupe_key = identity.derive_dedupe_key(
		submitter_type=submitter_type,
		mobile=contact_mobile,
		fayda_id=kwargs.get("fayda_id"),
		national_id=kwargs.get("national_id"),
		registration_number=kwargs.get("registration_number"),
		org_number=kwargs.get("org_number"),
		farmer_id=kwargs.get("farmer_id"),
		dedupe_key=kwargs.get("dedupe_key"),
	)

	if not dedupe_key:
		frappe.throw(
			_("Submitter registration requires a valid contact phone number or identifier."),
			frappe.ValidationError,
		)

	# Check if a Submitter Profile already exists with this dedupe_key
	existing_name = frappe.db.get_value("Grievance Submitter Profile", {"dedupe_key": dedupe_key}, "name")
	if existing_name:
		profile = frappe.get_doc("Grievance Submitter Profile", existing_name)
		if profile.user and profile.user != user_doc.name:
			frappe.throw(
				_(
					"A Submitter Profile with dedupe key '{0}' is already registered under another account."
				).format(dedupe_key),
				frappe.DuplicateEntryError,
			)
		profile.user = user_doc.name
		if submitter_name:
			profile.submitter_name = submitter_name
		if contact_mobile:
			profile.contact_mobile = contact_mobile
		if contact_email:
			profile.contact_email = contact_email
		admin_area = kwargs.get("administrative_area") or kwargs.get("region")
		if admin_area:
			profile.administrative_area = admin_area
		if kwargs.get("administrative_unit") or kwargs.get("woreda"):
			profile.administrative_unit = kwargs.get("administrative_unit") or kwargs.get("woreda")
		profile.save(ignore_permissions=True)
	else:
		profile = frappe.new_doc("Grievance Submitter Profile")
		profile.user = user_doc.name
		profile.submitter_type = submitter_type
		profile.submitter_name = submitter_name
		profile.contact_mobile = contact_mobile
		profile.contact_email = contact_email
		profile.dedupe_key = dedupe_key
		profile.administrative_area = kwargs.get("administrative_area") or kwargs.get("region")
		profile.administrative_unit = kwargs.get("administrative_unit") or kwargs.get("woreda")
		profile.active = 1

		profile.insert(ignore_permissions=True)

	return profile
