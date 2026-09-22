"""FR-03 Routing and Assignment with Nearest-Ancestor Administrative Area matching.

Auto-routing rules consider service category, administrative area (tree hierarchy)
and associated service provider. Where a rule matches, the grievance is assigned and the
status advances to Assigned. Where none matches, it stays Submitted and sits in the
nodal officer's manual queue.
"""

import frappe

from oan_grievance_service.services import constants as C

MATCH_FIELDS = (
	("service_category", "service_category"),
	("service_provider", "associated_service_provider"),
)


def find_matching_rule(grievance, department=None):
	"""Return the winning Grievance Routing Rule, or None for the manual queue.

	`department` limits the result to a rule whose assigned department is that
	value. Reassignment uses it so a move to another department still has to be
	one the category and area rules allow.

	Uses Nearest-Ancestor resolution for administrative_area:
	A rule matches when its category and provider match (or are unconstrained),
	and its administrative_area is an ancestor or exact match of the grievance's area.
	Among matching rules:
	1. Explicit rule_precedence (lower is evaluated first)
	2. Narrowest tree span (rgt - lft) = deepest/nearest ancestor
	3. Specificity count (number of constrained dimensions)
	"""
	case_area = grievance.get("administrative_area")
	case_lft = grievance.get("area_lft")
	case_rgt = None
	if case_area and case_lft is None:
		case_lft, case_rgt = frappe.db.get_value(
			"Grievance Administrative Area", case_area, ["lft", "rgt"]
		) or (
			None,
			None,
		)
	elif case_area and case_lft is not None:
		case_rgt = frappe.db.get_value("Grievance Administrative Area", case_area, "rgt")

	rules = frappe.get_all(
		"Grievance Routing Rule",
		filters={"active": 1},
		fields=[
			"name",
			"rule_precedence",
			"assigned_dept",
			"administrative_area",
			*[rule_field for rule_field, _ in MATCH_FIELDS],
		],
		order_by="rule_precedence asc",
	)

	candidates = []
	for rule in rules:
		specificity = 0
		matched = True

		# Direct fields match
		for rule_field, doc_field in MATCH_FIELDS:
			constraint = rule.get(rule_field)
			if not constraint:
				continue
			specificity += 1
			if constraint != grievance.get(doc_field):
				matched = False
				break
		if not matched:
			continue

		# Administrative Area nearest-ancestor containment check
		rule_area = rule.get("administrative_area")
		area_span = 999999999  # Global default (no area constraint)
		if rule_area:
			specificity += 1
			if not case_area or case_lft is None:
				matched = False
			else:
				rule_lft, rule_rgt = frappe.db.get_value(
					"Grievance Administrative Area", rule_area, ["lft", "rgt"]
				) or (None, None)
				if rule_lft is None or rule_rgt is None:
					matched = False
				elif not (rule_lft <= int(case_lft) and rule_rgt >= int(case_rgt or case_lft)):
					matched = False
				else:
					area_span = int(rule_rgt) - int(rule_lft)

		if department and rule.assigned_dept != department:
			continue

		if matched:
			# Sorting tuple: (rule_precedence, area_span, -specificity)
			candidates.append((rule.rule_precedence or 0, area_span, -specificity, rule))

	if not candidates:
		return None
	candidates.sort(key=lambda row: (row[0], row[1], row[2]))
	return candidates[0][3]


def find_matching_assignment(grievance, department=None):
	"""Return the winning Grievance RBAC Assignment (Desk), or None.

	`department` keeps only desks whose department scope is that department, which
	is how a reassignment picks an officer inside the department the routing rule
	named.

	Uses Nearest-Ancestor resolution for administrative_area_scope:
	Matches when category and type match (or are unconstrained), and area is in subtree.
	"""
	case_area = grievance.get("administrative_area")
	case_lft = grievance.get("area_lft")
	case_rgt = None
	if case_area and case_lft is None:
		case_lft, case_rgt = frappe.db.get_value(
			"Grievance Administrative Area", case_area, ["lft", "rgt"]
		) or (
			None,
			None,
		)
	elif case_area and case_lft is not None:
		case_rgt = frappe.db.get_value("Grievance Administrative Area", case_area, "rgt")

	today = frappe.utils.today()
	assignments = frappe.get_all(
		"Grievance RBAC Assignment",
		filters={
			"active": 1,
			"effective_from": ["<=", today],
		},
		or_filters=[
			["effective_to", "is", "not set"],
			["effective_to", ">=", today],
		],
		fields=[
			"name",
			"department_scope",
			"category_scope",
			"administrative_area_scope",
			"routing_strategy",
		],
	)

	candidates = []
	for a in assignments:
		if department and a.department_scope != department:
			continue

		specificity = 0
		matched = True

		if a.category_scope:
			specificity += 1
			if a.category_scope != grievance.get("service_category"):
				matched = False
		if not matched:
			continue

		rule_area = a.administrative_area_scope
		area_span = 999999999
		if rule_area:
			specificity += 1
			if not case_area or case_lft is None:
				matched = False
			else:
				rule_lft, rule_rgt = frappe.db.get_value(
					"Grievance Administrative Area", rule_area, ["lft", "rgt"]
				) or (None, None)
				if rule_lft is None or rule_rgt is None:
					matched = False
				elif not (rule_lft <= int(case_lft) and rule_rgt >= int(case_rgt or case_lft)):
					matched = False
				else:
					area_span = int(rule_rgt) - int(rule_lft)

		if matched:
			candidates.append((area_span, -specificity, a))

	if not candidates:
		return None
	candidates.sort(key=lambda row: (row[0], row[1]))
	return candidates[0][2]


def pick_officer_by_strategy(assignment_doc):
	"""Pick an officer from the assignment's child officers based on routing_strategy."""
	if not assignment_doc.get("officers"):
		return None

	active_officers = [o for o in assignment_doc.officers if getattr(o, "active", 1)]
	if not active_officers:
		return None

	strategy = getattr(assignment_doc, "routing_strategy", "Primary First") or "Primary First"

	if strategy == "Round Robin":

		def rr_key(o):
			val = getattr(o, "last_assigned_at", None)
			return (1, str(val)) if val else (0, "")

		active_officers.sort(key=rr_key)
		winner = active_officers[0]
		winner.last_assigned_at = frappe.utils.now_datetime()
		if winner.name:
			frappe.db.set_value(
				"Grievance RBAC Assignment Officer",
				winner.name,
				"last_assigned_at",
				winner.last_assigned_at,
				update_modified=False,
			)
		return winner.user

	elif strategy == "Least Loaded":
		scored = []
		for o in active_officers:
			open_count = frappe.db.count(
				"Grievance",
				filters={
					"assigned_to": o.user,
					"status": ["not in", [C.CLOSED, C.REJECTED, C.RESOLVED]],
				},
			)
			max_cap = getattr(o, "max_open_cases", 0) or 0
			at_cap = 1 if (max_cap > 0 and open_count >= max_cap) else 0
			scored.append((at_cap, open_count, o))

		scored.sort(key=lambda item: (item[0], item[1]))
		winner = scored[0][2]
		return winner.user

	else:  # Primary First
		active_officers.sort(key=lambda o: -int(getattr(o, "is_primary", 0) or 0))
		return active_officers[0].user


def resolve_officer(grievance, department, officer=None):
	"""Officer for `department` from the matching RBAC desk.

	With no `officer`, the desk's routing strategy picks one. A named officer is
	accepted only when they are an active member of that desk. Returns None when
	the department has no desk and the caller did not name an officer.
	"""
	from frappe import _

	assignment = find_matching_assignment(grievance, department=department)
	if officer:
		if not assignment or not frappe.db.exists(
			"Grievance RBAC Assignment Officer",
			{"parent": assignment.name, "user": officer, "active": 1},
		):
			frappe.throw(
				_("{0} is not an active officer on the routing desk for {1}.").format(officer, department),
				title=_("Officer Not Routed"),
			)
		return officer

	if not assignment:
		return None

	desk = frappe.get_doc("Grievance RBAC Assignment", assignment.name)
	return pick_officer_by_strategy(desk)


def apply_routing(grievance, commit_status=True):
	"""Route a grievance. Returns the rule that matched, or None.

	FSD 3.3 / Database Schema 8: resolves Tier 1 (department) and Tier 2 (officer) from the
	matching Grievance RBAC Assignment desk record.
	"""
	from oan_grievance_service.services import lifecycle, notifications

	assignment = find_matching_assignment(grievance)
	if assignment:
		doc = frappe.get_doc("Grievance RBAC Assignment", assignment.name)
		grievance.db_set("assigned_dept", doc.department_scope, update_modified=False)
		grievance.db_set("routing_rule", doc.name, update_modified=False)
		grievance.db_set("routed_automatically", 1, update_modified=False)

		officer_user = pick_officer_by_strategy(doc)
		if officer_user:
			grievance.db_set("assigned_to", officer_user, update_modified=False)

		if commit_status:
			lifecycle.assign(grievance, note=f"Auto-routed by assignment {doc.name}", automated=True)
			notifications.queue(grievance, C.EVENT_ASSIGNED_AUTO)
		return doc

	rule = find_matching_rule(grievance)
	if not rule:
		grievance.db_set("routed_automatically", 0, update_modified=False)
		return None

	grievance.db_set("assigned_dept", rule.assigned_dept, update_modified=False)
	grievance.db_set("routing_rule", rule.name, update_modified=False)
	grievance.db_set("routed_automatically", 1, update_modified=False)

	if commit_status:
		lifecycle.assign(grievance, note=f"Auto-routed by rule {rule.name}", automated=True)
		notifications.queue(grievance, C.EVENT_ASSIGNED_AUTO)

	return rule


def manual_assign(grievance, department, officer=None, assigned_by=None):
	"""FSD 3.3 / 4.1 step 8b: the nodal officer assigns from the manual queue."""
	from oan_grievance_service.services import lifecycle, notifications

	grievance.db_set("assigned_dept", department, update_modified=False)
	if officer:
		grievance.db_set("assigned_to", officer, update_modified=False)
	grievance.db_set("routed_automatically", 0, update_modified=False)

	lifecycle.assign(grievance, note=f"Manually assigned by {assigned_by or frappe.session.user}")
	notifications.queue(grievance, C.EVENT_ASSIGNED_MANUAL)


def manual_queue():
	"""FSD 3.3: grievances awaiting a nodal officer's routing decision."""
	return frappe.get_all(
		"Grievance",
		filters={"status": C.SUBMITTED, "assigned_dept": ["is", "not set"]},
		fields=["name", "ticket_number", "service_category", "administrative_area", "creation"],
		order_by="creation asc",
	)
