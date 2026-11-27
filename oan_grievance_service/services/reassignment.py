"""Reassignment rules: resolve receiving desk, validate, route to receiving dept head, and apply.

Rules:
- Reassignment requires the approval of the receiving department's head.
- For a move within the same department, that is the department's own head.
- Approver = department_head_of(target department, area).
- Decider may act directly if admin, if the desk flag reassignment_requires_approval is OFF,
  if the move stays in the department and hands the case to an officer who reports to the user,
  if the case sits in the fallback "Other" category, the move takes it to another and the user is an
  officer on an "Other" desk (the receiving category's head does not rule),
  or if the user is the head of the target department.
- Category and type follow receiving desks; SLA restarts if category changed.
- What an officer may move (`check_officer_move`, `officer_move_limits`): an administrator moves
  anything, and the fallback desk triages a case to its real category. Every other officer keeps
  the category. The officers in line for a case are the next level up and those below the
  requester on a desk covering it; a move to anyone else (a colleague at the same level, an
  officer of another department) always waits for the receiving head, never applies directly.
"""

import frappe
from frappe import _

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	covering_rows,
	department_head_of,
	query_active_officer_assignments,
)
from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
	GrievanceRoleLevel,
)
from oan_grievance_service.permissions import (
	desk_teams,
	is_department_head,
	is_unrestricted,
	oversees_officer,
	seat_covers,
)
from oan_grievance_service.services import audit, notifications
from oan_grievance_service.services import constants as C
from oan_grievance_service.services.routing import (
	department_desks,
	find_matching_assignment,
	officer_desks,
	pick_officer_by_strategy,
)

TRIGGER_FIELDS = ("assigned_dept", "assigned_to", "service_category")


def is_reassignment(request) -> bool:
	"""Whether the change request is a reassignment (touches assigned_dept, assigned_to or service_category)."""
	changes = getattr(request, "changes", None) or []
	return any(getattr(r, "fieldname", None) in TRIGGER_FIELDS for r in changes)


def target_department(grievance, request):
	"""The target department: request's assigned_dept if changed, else grievance.assigned_dept."""
	changes = getattr(request, "changes", None) or []
	for r in changes:
		if getattr(r, "fieldname", None) == "assigned_dept":
			return r.new_value
	return grievance.assigned_dept


def target_category(grievance, request):
	"""The target category: request's service_category if changed, else grievance.service_category."""
	for r in getattr(request, "changes", None) or []:
		if getattr(r, "fieldname", None) == "service_category":
			return r.new_value
	return grievance.service_category


def desk_requires_approval(grievance) -> bool:
	"""Check whether the source desk requires department head approval for reassignment."""
	dept = getattr(grievance, "assigned_dept", None)
	officer = getattr(grievance, "assigned_to", None)

	routing_rule = getattr(grievance, "routing_rule", None)
	if routing_rule:
		rule_dept = frappe.db.get_value("Grievance RBAC Assignment", routing_rule, "department_scope")
		if not rule_dept or rule_dept == dept:
			val = frappe.db.get_value(
				"Grievance RBAC Assignment", routing_rule, "reassignment_requires_approval"
			)
			if val is not None:
				return bool(val)

	if dept and officer:
		desks = officer_desks(grievance, dept, officer)
		if desks:
			val = desks[0].get("reassignment_requires_approval")
			if val is not None:
				return bool(val)
			val = frappe.db.get_value(
				"Grievance RBAC Assignment", desks[0].get("name"), "reassignment_requires_approval"
			)
			if val is not None:
				return bool(val)

	if dept:
		desks = department_desks(grievance, dept)
		if desks:
			val = desks[0].get("reassignment_requires_approval")
			if val is not None:
				return bool(val)
			val = frappe.db.get_value(
				"Grievance RBAC Assignment", desks[0].get("name"), "reassignment_requires_approval"
			)
			if val is not None:
				return bool(val)

	desk = find_matching_assignment(grievance)
	if desk:
		if not dept or not desk.get("department_scope") or desk.get("department_scope") == dept:
			val = desk.get("reassignment_requires_approval")
			if val is not None:
				return bool(val)
			val = frappe.db.get_value(
				"Grievance RBAC Assignment", desk.get("name"), "reassignment_requires_approval"
			)
			if val is not None:
				return bool(val)

	return True


def may_decide(user, grievance, request) -> bool:
	"""Whether `user` may directly approve this reassignment without waiting for higher authority."""
	if is_unrestricted(user):
		return True
	target_dept = target_department(grievance, request)
	head_of_target = is_department_head(
		user, target_dept, grievance.administrative_area, category=target_category(grievance, request)
	)
	# A pick outside the line (a same-level colleague, another department's officer) is the
	# receiving head's to approve, whatever the desk's flag says.
	if out_of_line(user, grievance, request):
		return head_of_target
	if not desk_requires_approval(grievance):
		return True
	# The fallback ("Other") desk triages: its officers send a case on to its real category with
	# no ruling from that category's head. Nobody else gets this.
	if target_category(grievance, request) != grievance.service_category and _triages_from_fallback(
		user, grievance
	):
		return True
	if target_dept == grievance.assigned_dept and _hands_down_to_subordinate(user, grievance, request):
		return True
	return head_of_target


def _hands_down_to_subordinate(user, grievance, request) -> bool:
	"""Whether the request names an officer below `user` on a desk of theirs covering the case."""
	officer = _named_officer(request)
	return bool(officer) and officer != user and oversees_officer(user, officer, grievance)


def _triages_from_fallback(user, grievance) -> bool:
	"""Whether `user` sits on the fallback desk and the case is still in the fallback category."""
	return grievance.service_category == C.FALLBACK_SERVICE_CATEGORY and _sits_on_fallback_desk(user)


def next_level_officers(grievance) -> frozenset:
	"""The officers one level above the case's holder who can take it now.

	The lowest rung above the holder's (`sla.rung_on_case`; an unassigned case starts below the
	first rung) with at least one officer on a desk covering the case who can take it
	(`sla.can_take_escalation`). A vacant rung, or one whose officers are all on leave, is
	skipped, as breach escalation skips it. Empty at the top of the chain.
	"""
	from oan_grievance_service.services import sla

	chain = GrievanceRoleLevel.get_chain()
	rank = {rung.name: index for index, rung in enumerate(chain)}
	holder = grievance.get("assigned_to")
	start = rank.get(sla.rung_on_case(holder, grievance), -1) + 1 if holder else 0
	rows = covering_rows(
		query_active_officer_assignments(
			fields=lambda c, p, u: [
				c.user,
				c.role_level,
				c.administrative_area.as_("administrative_area_scope"),
				p.department_scope,
				p.category_scope,
			],
			order_by=None,
			department=grievance.assigned_dept,
			category=grievance.service_category,
		),
		grievance.assigned_dept,
		grievance.service_category,
		grievance.administrative_area,
	)
	for rung in chain[start:]:
		officers = frozenset(
			row.user
			for row in rows
			if row.role_level == rung.name
			and row.user
			and row.user != holder
			and sla.can_take_escalation(row.user, grievance)
		)
		if officers:
			return officers
	return frozenset()


def officer_move_limits(grievance, user) -> frozenset | None:
	"""The officers in line for `user` to hand the case to, or None when the desks alone decide.

	None for an administrator, and for an officer on the fallback desk triaging a case still in
	the fallback category. For any other officer: the next level up (`next_level_officers`) and
	the officers below them on a desk of theirs covering the case. These are what the reassign
	picker offers. A move to anyone else is not refused, but always waits for the receiving head
	(`may_decide`).
	"""
	if is_unrestricted(user) or _triages_from_fallback(user, grievance):
		return None
	allowed = set(next_level_officers(grievance))
	for seat, team in desk_teams(user):
		if seat_covers(grievance, seat):
			allowed |= team
	return frozenset(allowed)


def _named_officer(request):
	for r in getattr(request, "changes", None) or []:
		if getattr(r, "fieldname", None) == "assigned_to":
			return r.new_value
	return None


def out_of_line(user, grievance, request) -> bool:
	"""Whether the request names an officer outside `officer_move_limits` for `user`."""
	officer = _named_officer(request)
	if not officer:
		return False
	limits = officer_move_limits(grievance, user)
	return limits is not None and officer not in limits


def check_officer_move(grievance, request):
	"""Refuse a category change by an officer: a misfiled case goes to an administrator.

	Administrators, and the fallback desk triaging a case to its real category, are exempt. A
	department move stays open to officers, decided by the receiving head as before.
	"""
	user = request.requested_by
	if is_unrestricted(user) or _triages_from_fallback(user, grievance):
		return
	if any(
		getattr(r, "fieldname", None) == "service_category" for r in getattr(request, "changes", None) or []
	):
		frappe.throw(
			_("Only an administrator can move a grievance to another category."),
			frappe.PermissionError,
			title=_("Change Not Permitted"),
		)


def _sits_on_fallback_desk(user) -> bool:
	"""Whether `user` is an active officer on a desk for the fallback category itself."""
	rows = query_active_officer_assignments(
		user=user, fields=lambda c, p, u: [p.category_scope], order_by=None
	)
	return any(row.get("category_scope") == C.FALLBACK_SERVICE_CATEGORY for row in rows)


def approver(grievance, request):
	"""The target department's head who must rule on the reassignment. None -> admin queue."""
	target_dept = target_department(grievance, request)
	return department_head_of(
		target_dept, grievance.administrative_area, category=target_category(grievance, request)
	)


def resolve(grievance, department=None, officer=None, actor=None, category=None, grievance_type=None) -> dict:
	"""Resolve receiving department, officer, category and type for a reassignment.

	When only one of department and category is named, the unnamed one is kept if some desk
	still matches it (a case with no department matches desks with none). Only then are desks
	outside it considered, and the case takes the chosen desk's department, cleared when that
	desk has none. When an officer is named, target department or category must be specified.
	Returns a dict of fields that differ from current grievance values.
	Throws ValidationError if the move is invalid or underspecified.
	"""
	actor = actor or frappe.session.user

	if officer and not department and not category:
		frappe.throw(
			_("Specify target department or category when naming an officer."),
			frappe.ValidationError,
			title=_("Department or Category Required"),
		)

	# 1. Candidate desks
	if officer:
		candidate_desks = officer_desks(grievance, department, officer)
	else:
		candidate_desks = department_desks(grievance, department)

	if not candidate_desks:
		if officer:
			frappe.throw(
				_("Invalid Officer: {0} does not cover {1} in this area.").format(officer, department)
				if department
				else _("Invalid Officer: {0} does not cover this area.").format(officer),
				frappe.ValidationError,
				title=_("Invalid Officer"),
			)
		frappe.throw(
			_("No assignment in {0} covers this area.").format(department)
			if department
			else _("No assignment covers this area."),
			frappe.ValidationError,
			title=_("No Desk Available"),
		)

	# 2. Category: "Accepts X" means category_scope is empty/None or equals X
	if category:
		cat_desks = [
			d for d in candidate_desks if not d.get("category_scope") or d.get("category_scope") == category
		]
		if not cat_desks:
			target_name = officer or department
			frappe.throw(
				_("{0} does not handle {1}").format(target_name, category)
				if target_name
				else _("No desk handles {0}").format(category),
				frappe.ValidationError,
				title=_("Category Not Supported"),
			)
		candidate_desks = cat_desks
		chosen_cat = category
	else:
		accepting_current = [
			d
			for d in candidate_desks
			if not d.get("category_scope") or d.get("category_scope") == grievance.service_category
		]
		if accepting_current:
			chosen_cat = grievance.service_category
			exact = [d for d in accepting_current if d.get("category_scope") == chosen_cat]
			candidate_desks = exact or accepting_current
		else:
			distinct_cats = sorted(
				list({d.get("category_scope") for d in candidate_desks if d.get("category_scope")})
			)
			if len(distinct_cats) == 1:
				chosen_cat = distinct_cats[0]
				candidate_desks = [
					d
					for d in candidate_desks
					if not d.get("category_scope") or d.get("category_scope") == chosen_cat
				]
			elif len(distinct_cats) > 1:
				frappe.throw(
					_("Choose target_category: {0}").format(", ".join(distinct_cats)),
					frappe.ValidationError,
					title=_("Choose Category"),
				)
			else:
				chosen_cat = grievance.service_category

	# A category-only move stays in the case's department when a desk there takes it.
	if not department:
		same_dept = [
			d
			for d in candidate_desks
			if (d.get("department_scope") or None) == (grievance.assigned_dept or None)
		]
		candidate_desks = same_dept or candidate_desks

	# 3. Type from surviving desks
	if grievance_type:
		type_cat = frappe.db.get_value("Grievance Type", grievance_type, "service_category")
		if not type_cat or type_cat != chosen_cat:
			frappe.throw(
				_("Grievance Type {0} does not belong to category {1}").format(grievance_type, chosen_cat),
				frappe.ValidationError,
				title=_("Invalid Grievance Type"),
			)
		type_desks = [
			d
			for d in candidate_desks
			if not d.get("grievance_type_scope") or d.get("grievance_type_scope") == grievance_type
		]
		if not type_desks:
			target_name = officer or department
			frappe.throw(
				_("{0} does not handle type {1}").format(target_name, grievance_type)
				if target_name
				else _("No desk handles type {0}").format(grievance_type),
				frappe.ValidationError,
				title=_("Grievance Type Not Supported"),
			)
		candidate_desks = type_desks
		chosen_type = grievance_type
	else:
		fixed_types = {
			d.get("grievance_type_scope") for d in candidate_desks if d.get("grievance_type_scope")
		}
		if len(fixed_types) == 1 and all(d.get("grievance_type_scope") for d in candidate_desks):
			chosen_type = next(iter(fixed_types))
		else:
			current_type = grievance.grievance_type
			current_type_cat = (
				frappe.db.get_value("Grievance Type", current_type, "service_category")
				if current_type
				else None
			)
			if current_type and current_type_cat == chosen_cat:
				chosen_type = current_type
			else:
				# If there is only one type under chosen category, check if desks allow it, else throw
				all_cat_types = frappe.get_all(
					"Grievance Type",
					filters={"service_category": chosen_cat, "is_active": 1},
					pluck="name",
				)
				if len(all_cat_types) == 1:
					chosen_type = all_cat_types[0]
				else:
					frappe.throw(
						_("Choose target_grievance_type for {0}").format(chosen_cat),
						frappe.ValidationError,
						title=_("Choose Grievance Type"),
					)

	# 4. Officer
	# Sensitivity follows the type the case is moving to, not the one it has: a case moved to a
	# sensitive type must leave a Kebele-level officer, and must not be handed to one.
	from oan_grievance_service.permissions import is_kebele_level_officer

	sensitive = (
		bool(frappe.db.get_value("Grievance Type", chosen_type, "is_sensitive"))
		if chosen_type
		else bool(grievance.get("is_sensitive"))
	)
	chosen_desk = candidate_desks[0]
	if officer:
		if sensitive and is_kebele_level_officer(officer):
			frappe.throw(
				_("A sensitive case cannot be assigned to a Kebele-level officer."),
				frappe.ValidationError,
				title=_("Invalid Officer"),
			)
		chosen_officer = officer
	else:
		current_officer = grievance.assigned_to
		chosen_officer = None
		if current_officer and not (sensitive and is_kebele_level_officer(current_officer)):
			surviving = {d.get("name"): d for d in candidate_desks}
			kept = [
				surviving[cd.get("name")]
				for cd in officer_desks(grievance, department, current_officer)
				if cd.get("name") in surviving
			]
			if kept:
				chosen_officer, chosen_desk = current_officer, kept[0]

		if not chosen_officer:
			desk_doc = frappe.get_doc("Grievance RBAC Assignment", chosen_desk.get("name"))
			chosen_officer = pick_officer_by_strategy(desk_doc, grievance, sensitive=sensitive)
			if not chosen_officer:
				frappe.throw(_("No active officer; name one."), frappe.ValidationError, title=_("No Officer"))

	# 5. Difference comparison
	# The department follows the desk the officer sits on, and is cleared when that desk has none.
	department = department or chosen_desk.get("department_scope") or None
	changes = {}
	if (department or None) != (grievance.assigned_dept or None):
		changes["assigned_dept"] = department
	if chosen_officer != grievance.assigned_to:
		changes["assigned_to"] = chosen_officer
	if chosen_cat != grievance.service_category:
		changes["service_category"] = chosen_cat
	if chosen_type != grievance.grievance_type:
		changes["grievance_type"] = chosen_type

	if not changes:
		frappe.throw(_("Nothing to change"), frappe.ValidationError, title=_("Nothing To Change"))

	return changes


def validate(grievance, request):
	"""Validate reassignment request changes against receiving desk rules."""
	changes = getattr(request, "changes", None) or []
	proposed_dept = target_department(grievance, request)
	proposed_officer = grievance.assigned_to
	proposed_cat = grievance.service_category
	proposed_type = grievance.grievance_type

	for r in changes:
		fn = getattr(r, "fieldname", None)
		if fn == "assigned_to":
			proposed_officer = r.new_value
		elif fn == "service_category":
			proposed_cat = r.new_value
		elif fn == "grievance_type":
			proposed_type = r.new_value

	if grievance.assigned_to and not proposed_officer:
		frappe.throw(
			_("Cannot clear assigned officer on an assigned grievance."),
			frappe.ValidationError,
			title=_("Invalid Officer"),
		)

	check_officer_move(grievance, request)

	if not is_unrestricted(request.requested_by) and proposed_officer:
		desks = officer_desks(grievance, proposed_dept, proposed_officer)
		accepting = [
			d
			for d in desks
			if (not d.get("category_scope") or d.get("category_scope") == proposed_cat)
			and (not d.get("grievance_type_scope") or d.get("grievance_type_scope") == proposed_type)
		]
		if not accepting:
			frappe.throw(_("Invalid Officer"), frappe.ValidationError, title=_("Invalid Officer"))


def on_pending(grievance, request):
	"""Queue notification when reassignment request goes pending."""
	if request.pending_with:
		notifications.queue(
			grievance, C.EVENT_REASSIGNMENT_REQUESTED, recipient_override=request.pending_with
		)


def receiving_desk(grievance):
	"""Find the desk that owns this case following reassignment."""
	dept = getattr(grievance, "assigned_dept", None)
	officer = getattr(grievance, "assigned_to", None)
	cat = getattr(grievance, "service_category", None)
	gtype = getattr(grievance, "grievance_type", None)

	if dept and officer:
		desks = officer_desks(grievance, dept, officer)
		matching = [
			d
			for d in desks
			if (not d.get("category_scope") or d.get("category_scope") == cat)
			and (not d.get("grievance_type_scope") or d.get("grievance_type_scope") == gtype)
		]
		if matching:
			return matching[0]

	if dept:
		desks = department_desks(grievance, dept)
		matching = [
			d
			for d in desks
			if (not d.get("category_scope") or d.get("category_scope") == cat)
			and (not d.get("grievance_type_scope") or d.get("grievance_type_scope") == gtype)
		]
		if matching:
			return matching[0]

	return find_matching_assignment(grievance)


def on_applied(grievance, request):
	"""Apply side effects of an approved reassignment (SLA reset, notifications)."""
	from oan_grievance_service.services import sla

	old_desk = grievance.get("routing_rule")
	new_desk = receiving_desk(grievance)
	new_desk_name = new_desk.get("name") if new_desk else None
	desk_changed = bool(new_desk_name and new_desk_name != old_desk)
	if new_desk_name and desk_changed:
		audit.record_version(grievance, {"routing_rule": new_desk_name})
		grievance.db_set("routing_rule", new_desk_name, update_modified=False)
		grievance.routing_rule = new_desk_name

	changes = getattr(request, "changes", None) or []
	changed = {getattr(r, "fieldname", None) for r in changes}
	if "service_category" in changed:
		sla.reset_clock(grievance)
	elif "grievance_type" in changed:
		# Same category, another type: a rule for that type may set other SLA days. The clock
		# keeps its start; only the window's length follows the new type.
		sla.follow_type_change(grievance)

	if desk_changed and "service_category" not in changed:
		sla.arm_escalation(grievance)

	officer_row = next((r for r in changes if getattr(r, "fieldname", None) == "assigned_to"), None)
	dept_row = next((r for r in changes if getattr(r, "fieldname", None) == "assigned_dept"), None)

	if officer_row and officer_row.new_value and "service_category" not in changed:
		# Handed to another level: the case runs on that level's SLA days, as an escalation does.
		# A category change has already restarted the window on the new assignee's level.
		old_rung = sla.rung_on_case(officer_row.old_value, grievance) if officer_row.old_value else None
		new_rung = sla.rung_on_case(officer_row.new_value, grievance)
		if new_rung and new_rung != old_rung:
			sla.start_level_window(grievance, new_rung)

	if officer_row and officer_row.new_value:
		if officer_row.new_value != request.decided_by:
			notifications.queue(grievance, C.EVENT_ASSIGNED_MANUAL, recipient_override=officer_row.new_value)
	elif dept_row and dept_row.new_value:
		notifications.queue(grievance, C.EVENT_ASSIGNED_MANUAL)
