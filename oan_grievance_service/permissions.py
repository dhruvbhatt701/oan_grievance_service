"""FR-01 / 3.1.1 Role-Based Access Control, deny-by-default.

The service runs on three capability roles and only three. A role answers *what actions
exist for you*; it never answers *which cases you may touch*. That second question is
answered by the Grievance RBAC Assignment records for the user - region, department and
category - applied as a permission query condition, so it filters list views, reports
and the API uniformly rather than being re-checked per screen.

Seniority is deliberately absent from the role list. The former L1 / L2 / Department
Head roles were rungs of a hierarchy, not distinct capabilities, and are replaced by
position in the reporting chain. See
.docs/sla_workflows_and_lifecycle_specification.md §10.1.

The FSD is explicit that routing eligibility does not by itself grant edit rights: an
explicit case assignment or approval permission is required. That distinction is what
this module enforces.
"""

import frappe

from oan_grievance_service.services import constants as C

ROLE_SUBMITTER = "Grievance Submitter"
ROLE_OFFICER = "Grievance Officer"
ROLE_ADMIN = "Grievance Admin"

GRIEVANCE_ROLES = (ROLE_ADMIN, ROLE_OFFICER, ROLE_SUBMITTER)

# FSD Appendix F: administrators see all regions, departments and categories.
UNRESTRICTED_ROLES = {ROLE_ADMIN, "System Manager", "Administrator"}

# The only states in which a case is actually waiting on its submitter. Outside these,
# a submitter reads their case but cannot alter it.
SUBMITTER_WRITABLE_STATUSES = frozenset({C.MORE_INFO_NEEDED, C.PENDING_SUBMITTER})


def active_scopes(user=None):
	"""The user's live RBAC assignments, honouring the effective date window."""
	user = user or frappe.session.user
	today = frappe.utils.today()
	query = """
		SELECT
			p.name AS assignment_name,
			p.administrative_area_scope,
			p.department_scope,
			p.category_scope,
			c.role_level,
			c.is_primary,
			c.max_open_cases
		FROM `tabGrievance RBAC Assignment Officer` c
		JOIN `tabGrievance RBAC Assignment` p ON p.name = c.parent
		WHERE c.user = %(user)s
		  AND c.active = 1
		  AND p.active = 1
		  AND p.effective_from <= %(today)s
		  AND (p.effective_to IS NULL OR p.effective_to = '' OR p.effective_to >= %(today)s)
	"""
	try:
		return frappe.db.sql(query, {"user": user, "today": today}, as_dict=True)
	except Exception:
		return []


def find_officer_by_role_level(role_level, department=None, administrative_area=None):
	"""Dynamically resolve an officer user from active Grievance RBAC Assignments.

	Honours role_level, geographic jurisdiction (area tree interval), line department
	(NULL for nodal officers who cover all departments in an area), and primary post priority.
	"""
	today = frappe.utils.today()
	query = """
		SELECT
			c.user,
			c.is_primary,
			p.administrative_area_scope,
			p.department_scope
		FROM `tabGrievance RBAC Assignment Officer` c
		JOIN `tabGrievance RBAC Assignment` p ON p.name = c.parent
		WHERE c.role_level = %(role_level)s
		  AND c.active = 1
		  AND p.active = 1
		  AND p.effective_from <= %(today)s
		  AND (p.effective_to IS NULL OR p.effective_to = '' OR p.effective_to >= %(today)s)
		ORDER BY c.is_primary DESC, p.modified DESC
	"""
	try:
		officers = frappe.db.sql(query, {"role_level": role_level, "today": today}, as_dict=True)
	except Exception:
		officers = []

	if not officers:
		return None

	target_lft = None
	if administrative_area:
		target_lft = frappe.db.get_value("Grievance Administrative Area", administrative_area, "lft")

	for o in officers:
		# Department filter: if assignment specifies a department, it must match.
		if department and o.department_scope and o.department_scope != department:
			continue
		# Area filter: if assignment specifies an area, target area must be in its subtree.
		if target_lft is not None and o.administrative_area_scope:
			area_info = frappe.db.get_value(
				"Grievance Administrative Area",
				o.administrative_area_scope,
				["lft", "rgt"],
				as_dict=True,
			)
			if area_info and area_info.lft is not None and area_info.rgt is not None:
				if not (area_info.lft <= int(target_lft) <= area_info.rgt):
					continue
		return o.user

	return None


def area_bounds(scopes):
	"""Nested Set intervals for every area named by `scopes`, in one query.

	Resolved live rather than denormalised onto the assignment row. Frappe's NestedSet
	shifts `lft`/`rgt` across the tree whenever a node is inserted or moved, so a stamped
	copy silently drifts out of the coordinate system it is compared against - and on a
	scope row that drift widens or narrows what an officer can see.
	"""
	names = {s.administrative_area_scope for s in scopes if s.administrative_area_scope}
	if not names:
		return {}
	return {
		a.name: (a.lft, a.rgt)
		for a in frappe.get_all(
			"Grievance Administrative Area",
			filters={"name": ["in", list(names)]},
			fields=["name", "lft", "rgt"],
		)
		if a.lft is not None and a.rgt is not None
	}


def _quote(values):
	return ", ".join(frappe.db.escape(v) for v in values if v)


def _submitter_profiles(user):
	"""Profiles this user owns.

	Resolved through the explicit `user` link rather than by matching a contact address,
	so changing a contact email cannot transfer someone else's cases, and two profiles
	sharing an address do not both match.
	"""
	return frappe.get_all("Grievance Submitter Profile", filters={"user": user}, pluck="name")


def get_subordinate_officers(user):
	"""Find all officers who report directly or indirectly to `user` (bottom-to-top hierarchy)."""
	if not user:
		return set()
	subordinates = {user}
	frontier = {user}
	today = frappe.utils.today()
	while frontier:
		query = """
			SELECT DISTINCT c.user
			FROM `tabGrievance RBAC Assignment Officer` c
			JOIN `tabGrievance RBAC Assignment` p ON p.name = c.parent
			WHERE c.reports_to IN %(frontier)s
			  AND c.active = 1
			  AND p.active = 1
			  AND p.effective_from <= %(today)s
			  AND (p.effective_to IS NULL OR p.effective_to = '' OR p.effective_to >= %(today)s)
		"""
		try:
			rows = frappe.db.sql(query, {"frontier": tuple(frontier), "today": today}, as_dict=True)
		except Exception:
			rows = []
		new_users = {r.user for r in rows if r.user and r.user not in subordinates}
		if not new_users:
			break
		subordinates.update(new_users)
		frontier = new_users
	return subordinates


def grievance_query_conditions(user=None):
	"""SQL appended to every Grievance list query. Deny-by-default.

	- Submitters only see their own cases and assisted submissions.
	- Officers see cases assigned to themselves and cases assigned to subordinate officers in their reporting chain.
	- Admins see all cases.
	"""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))

	if roles & UNRESTRICTED_ROLES:
		return ""

	clauses = []

	# FSD 3.1.1: a submitter reaches their own cases, and the assisted submissions they
	# filed on someone else's behalf. Both arms belong to the one Submitter role.
	if ROLE_SUBMITTER in roles:
		profiles = _submitter_profiles(user)
		if profiles:
			clauses.append(f"`tabGrievance`.submitter in ({_quote(profiles)})")
		clauses.append(f"`tabGrievance`.assisted_by_officer = {frappe.db.escape(user)}")

	# Grievance Officer: sees cases assigned to self/subordinates and cases within configured scope
	if ROLE_OFFICER in roles:
		scope_clauses = []
		scopes = active_scopes(user)
		bounds = area_bounds(scopes)
		for scope in scopes:
			parts = []
			if scope.department_scope:
				parts.append(f"`tabGrievance`.assigned_dept = {frappe.db.escape(scope.department_scope)}")
			if scope.category_scope:
				parts.append(f"`tabGrievance`.service_category = {frappe.db.escape(scope.category_scope)}")
			if scope.administrative_area_scope:
				area_lft, area_rgt = bounds.get(scope.administrative_area_scope, (None, None))
				if area_lft is not None and area_rgt is not None:
					parts.append(
						f"(`tabGrievance`.area_lft >= {int(area_lft)} and `tabGrievance`.area_lft <= {int(area_rgt)})"
					)

			if parts:
				scope_clauses.append("(" + " and ".join(parts) + ")")
			else:
				scope_clauses.append("1 = 1")

		team = get_subordinate_officers(user)
		scope_clauses.append(f"`tabGrievance`.assigned_to in ({_quote(team)})")
		clauses.append("(" + " or ".join(scope_clauses) + ")")

	if not clauses:
		return "1 = 0"
	return "(" + " or ".join(clauses) + ")"


def has_grievance_permission(doc, ptype="read", user=None):
	"""Per-document check. Mirrors the list conditions for a single record."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))

	if roles & UNRESTRICTED_ROLES:
		return True

	if ROLE_SUBMITTER in roles:
		submitter = doc.get("submitter") if isinstance(doc, dict) else getattr(doc, "submitter", None)
		assisted = (
			doc.get("assisted_by_officer")
			if isinstance(doc, dict)
			else getattr(doc, "assisted_by_officer", None)
		)
		status = doc.get("status") if isinstance(doc, dict) else getattr(doc, "status", None)
		owns = bool(submitter) and submitter in _submitter_profiles(user)
		if owns or assisted == user:
			if ptype == "read":
				return True
			return ptype == "write" and status in SUBMITTER_WRITABLE_STATUSES

	if ROLE_OFFICER not in roles:
		return False

	team = get_subordinate_officers(user)
	assigned = doc.get("assigned_to") if isinstance(doc, dict) else getattr(doc, "assigned_to", None)
	if assigned in team:
		# Visibility granted for all cases in reporting chain; editing requires explicit assignment or supervisor
		return True if ptype == "read" else assigned == user

	dept = doc.get("assigned_dept") if isinstance(doc, dict) else getattr(doc, "assigned_dept", None)
	category = (
		doc.get("service_category") if isinstance(doc, dict) else getattr(doc, "service_category", None)
	)
	area = (
		doc.get("administrative_area") if isinstance(doc, dict) else getattr(doc, "administrative_area", None)
	)
	case_lft = doc.get("area_lft") if isinstance(doc, dict) else getattr(doc, "area_lft", None)
	if case_lft is None and area:
		case_lft = frappe.db.get_value("Grievance Administrative Area", area, "lft")

	scopes = active_scopes(user)
	bounds = area_bounds(scopes)
	for scope in scopes:
		if scope.department_scope and dept != scope.department_scope:
			continue
		if scope.category_scope and category != scope.category_scope:
			continue
		if scope.administrative_area_scope:
			scope_lft, scope_rgt = bounds.get(scope.administrative_area_scope, (None, None))
			if scope_lft is not None and scope_rgt is not None:
				if case_lft is None or not (scope_lft <= int(case_lft) <= scope_rgt):
					continue

		# FSD 3.1.1: scope grants visibility; editing still needs the case assigned.
		return ptype == "read"

	return False


def can_assign(user=None):
	"""Initial assignment is a staff action. The routing rule picks the target."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	return bool(roles & ({ROLE_OFFICER} | UNRESTRICTED_ROLES))


def can_reassign(grievance, user=None):
	"""STG-337: only the officer who holds the case, or an administrator, may reassign it."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if roles & UNRESTRICTED_ROLES:
		return True
	assigned = grievance.get("assigned_to") if isinstance(grievance, dict) else grievance.assigned_to
	return ROLE_OFFICER in roles and bool(assigned) and assigned == user


def can_approve_reassignment(user=None):
	"""FSD 3.3.1: a reassignment is decided by a supervisor, not by its requester.

	TODO(spec §10.5): the correct test is that the approver is a common ancestor of the
	current and target assignees in the reporting chain. Until chain.py lands this is
	role-only, which is broader than intended - any officer may approve.
	"""
	roles = set(frappe.get_roles(user or frappe.session.user))
	return bool(roles & ({ROLE_OFFICER} | UNRESTRICTED_ROLES))


def can_approve_deferral(user=None, assignee=None):
	"""FSD 3.11.7: supervisor approval unless policy explicitly permits self-approval.

	"Supervisor" is read off the escalation chain rather than a role name: the approver
	must sit strictly above the assigned officer, which is the same `level_order` walk
	escalation uses. Falling back to a role check when the chain cannot place either
	party keeps a misconfigured assignment from deadlocking every deferral.
	"""
	from oan_grievance_service.grievance_sla.doctype.grievance_deferral_policy.grievance_deferral_policy import (
		requires_supervisor_approval,
	)

	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	has_base_right = bool(roles & ({ROLE_OFFICER} | UNRESTRICTED_ROLES))

	if not has_base_right:
		return False
	if not requires_supervisor_approval() or roles & UNRESTRICTED_ROLES:
		return has_base_right
	if not assignee or assignee == user:
		# Self-approval is exactly what the policy is there to stop.
		return assignee != user

	return _outranks(user, assignee)


def _outranks(approver, assignee):
	"""True when the approver sits strictly higher in the escalation chain."""
	from oan_grievance_service.services import sla

	approver_level = sla.current_level_of(approver)
	assignee_level = sla.current_level_of(assignee)
	if not approver_level or not assignee_level:
		return True  # Chain cannot place them; fall back to the role check already passed.

	orders = {
		row.name: row.level_order
		for row in frappe.get_all(
			"Grievance Role Level", filters={"is_active": 1}, fields=["name", "level_order"]
		)
	}
	return orders.get(approver_level, 0) > orders.get(assignee_level, 0)
