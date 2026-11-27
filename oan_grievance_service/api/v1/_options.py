"""Shared dropdown and reference data helpers for API options endpoints."""

import frappe
from frappe.query_builder import Order
from frappe.query_builder.functions import Coalesce, NullIf

from oan_grievance_service.permissions import is_staff
from oan_grievance_service.services import constants as C


def _staffed_desks() -> list[tuple[str | None, str | None]]:
	"""The (department, category) scopes of every active desk that has an active officer.

	A Reviewer does not staff a desk: they only read its cases.
	"""
	from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
		query_active_officer_assignments,
	)

	rows = query_active_officer_assignments(
		fields=lambda c, p, u: [p.department_scope.as_("department"), p.category_scope.as_("category")],
		order_by=None,
		distinct=True,
	)
	return [(row["department"] or None, row["category"] or None) for row in rows]


def get_service_categories(department: str | None = None) -> list[dict]:
	"""Retrieve the active grievance service categories that a desk with officers handles.

	With a `department` (its canonical id), only the categories that department has a staffed
	desk for. Without one, the fallback category is always listed too: it is where a case with
	no better home goes.
	"""
	names = {
		category
		for desk_department, category in _staffed_desks()
		if category and (not department or desk_department == department)
	}
	if not department:
		names.add(C.FALLBACK_SERVICE_CATEGORY)
	return frappe.get_all(
		"Grievance Service Category",
		filters={"is_active": 1, "name": ["in", sorted(names)]},
		fields=["name as category_name", "code", "sort_order"],
		order_by="sort_order asc, name asc",
		ignore_permissions=True,
	)


def get_grievance_types(service_category: str | None = None) -> list[dict]:
	"""Retrieve active grievance types, optionally filtered by service_category."""
	gtype_filters = [["is_active", "=", 1]]
	if service_category:
		gtype_filters.append(["service_category", "=", service_category])

	return frappe.get_all(
		"Grievance Type",
		filters=gtype_filters,
		fields=["name as grievance_type_id", "type_name", "service_category"],
		order_by="type_name asc",
		ignore_permissions=True,
	)


def active_channels() -> list[str]:
	"""The intake channels currently open, read from the master."""
	return frappe.get_all(
		"Grievance Submission Type",
		filters={"is_active": 1},
		pluck="name",
		order_by="submission_type_name asc",
	)


def get_submission_types() -> list[dict]:
	"""Retrieve active intake submission types."""
	return frappe.get_all(
		"Grievance Submission Type",
		filters={"is_active": 1},
		fields=["name as type_name", "code", "description"],
		order_by="name asc",
		ignore_permissions=True,
	)


def get_departments() -> list[dict]:
	"""Retrieve the grievance departments that have a desk with officers."""
	staffed = sorted({department for department, _category in _staffed_desks() if department})
	return frappe.get_all(
		"Grievance Department",
		filters={"name": ["in", staffed]},
		fields=["name as department_id", "dept_name as department_name", "short_name"],
		order_by="dept_name asc",
		ignore_permissions=True,
	)


def get_department_officers(
	department: str | None = None,
	service_category: str | None = None,
	administrative_area: str | None = None,
	exclude_user: str | None = None,
	exclude_role_levels: set[str] | frozenset[str] | None = None,
) -> list[dict]:
	"""Retrieve active officers assigned to a department, optionally filtered by category/area.

	Returns only what an officer picker needs: no email or reporting line. An officer
	on several matching desks appears once, with their primary desk first.
	`exclude_user` (the caller) is left out, so nobody is offered themselves. An officer
	whose matching desk rows are all on one of `exclude_role_levels` is left out too.
	"""
	if not department and not service_category:
		return []

	from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
		query_active_officer_assignments,
	)
	from oan_grievance_service.services.resolvers import resolve_administrative_area

	resolved_area = (
		(resolve_administrative_area(administrative_area) or administrative_area)
		if administrative_area
		else None
	)

	officers = query_active_officer_assignments(
		department=department,
		category=service_category,
		administrative_area=resolved_area,
		include_user_details=True,
		fields=lambda c, p, u: [
			c.user.as_("user_id"),
			Coalesce(NullIf(u.full_name, ""), u.name).as_("full_name"),
			c.role_level,
			c.is_primary,
			c.administrative_area.as_("administrative_area_scope"),
		],
		order_by=lambda c, p, u: [(c.is_primary, Order.desc), (u.full_name, Order.asc), (c.user, Order.asc)],
	)

	unique = {}
	for off in officers:
		if off["user_id"] in unique or off["user_id"] == exclude_user:
			continue
		if exclude_role_levels and off.get("role_level") in exclude_role_levels:
			continue
		off.pop("administrative_area_scope", None)
		off["is_primary"] = bool(off.get("is_primary"))
		unique[off["user_id"]] = off

	return list(unique.values())


def get_identity_schemes() -> list[dict]:
	"""Retrieve supported identity schemes and descriptions."""
	return [
		{"scheme": "fayda", "label": "Fayda National ID", "description": "National Digital ID"},
		{
			"scheme": "org",
			"label": "Registration / Certificate Number",
			"description": "Official Organization / Enterprise Registration Number",
		},
		{"scheme": "phone", "label": "Phone Number", "description": "Mobile Phone Number"},
	]


def get_submitter_types() -> list[dict]:
	"""Retrieve active submitter types with their accepted identity schemes."""
	from oan_grievance_service.services.identity import SUBMITTER_TYPE_RULES

	types = frappe.get_all(
		"Grievance Submitter Type",
		filters={"is_active": 1},
		fields=["name as type_name", "code", "description"],
		order_by="name asc",
		ignore_permissions=True,
	)
	for t in types:
		rule = SUBMITTER_TYPE_RULES.get(t["type_name"])
		t["allowed_schemes"] = list(rule.schemes) if rule else ["phone"]
	return types


def get_role_levels() -> list[dict]:
	"""Retrieve active role levels for escalation hierarchy."""
	return frappe.get_all(
		"Grievance Role Level",
		filters={"is_active": 1},
		fields=["name as level_code", "level_name", "level_order", "escalation_hours", "description"],
		order_by="level_order asc",
		ignore_permissions=True,
	)


# KPI cards for the all-grievances queue. Workflow states
# that are not one of these cards roll up into In Progress so the queue does not
# grow a new status every time the workflow gains a stage.
#
# `workflow_states` are the engine states that count toward the card. `All` is
# every grievance. `officer_only` cards (Assigned) appear for staff
# only; for submitters those states roll into In Progress. A card with `rolls_into` has no
# KPI of its own: its states are counted and filtered under the card it names, while the
# case still shows its own status (a submitter must see More Info Needed). Terminal is read
# only from the active Workflow (no transition leaving the state) — there is
# no per-card default.
_STATUS_CARDS = (
	{
		"status": "All",
		"label": "All",
		"workflow_states": (),
		"officer_only": False,
	},
	{
		"status": "Submitted",
		"label": "Submitted",
		"workflow_states": (C.STATE_SUBMITTED,),
		"officer_only": False,
	},
	{
		"status": "Assigned",
		"label": "Assigned",
		"workflow_states": (C.STATE_ASSIGNED,),
		"officer_only": True,
	},
	{
		"status": "Reopened",
		"label": "Reopened",
		"workflow_states": (C.STATE_REOPENED,),
		"officer_only": False,
	},
	{
		"status": "In Progress",
		"label": "In Progress",
		"workflow_states": (C.STATE_IN_PROGRESS,),
		"officer_only": False,
	},
	{
		"status": "More Info Needed",
		"label": "More Info Needed",
		"workflow_states": (C.STATE_MORE_INFO_NEEDED,),
		"officer_only": False,
		"rolls_into": "In Progress",
	},
	{
		"status": "Duplicate",
		"label": "Duplicates",
		"workflow_states": (C.STATE_DUPLICATE,),
		"officer_only": False,
	},
	{
		"status": "Rejected",
		"label": "Rejected",
		"workflow_states": (C.STATE_REJECTED,),
		"officer_only": False,
	},
	{
		"status": "Resolved",
		"label": "Resolved",
		"workflow_states": (C.STATE_RESOLVED,),
		"officer_only": False,
	},
	{
		"status": "Closed",
		"label": "Closed",
		"workflow_states": (C.STATE_CLOSED,),
		"officer_only": False,
	},
)

_CARD_BY_STATUS = {card["status"]: card for card in _STATUS_CARDS}


def _card_states(status: str) -> tuple[str, ...]:
	"""The workflow states a card counts: its own, and those of the cards rolled into it."""
	states = list(_CARD_BY_STATUS[status]["workflow_states"])
	for card in _STATUS_CARDS:
		if card.get("rolls_into") == status:
			states.extend(card["workflow_states"])
	return tuple(states)


# Lower-cased, punctuation-stripped keys the list filter and the cards accept.
_STATUS_ALIASES = {
	"all": "All",
	"submitted": "Submitted",
	"assigned": "Assigned",
	"reopened": "Reopened",
	"in progress": "In Progress",
	"more info needed": "More Info Needed",
	"rejected": "Rejected",
	"duplicates": "Duplicate",
	"resolved": "Resolved",
	"closed": "Closed",
}
for _card in _STATUS_CARDS:
	for _state in _card["workflow_states"]:
		_STATUS_ALIASES.setdefault(_state.lower(), _card["status"])


def _canonical_status(value: str) -> str | None:
	key = " ".join(str(value).strip().lower().replace("_", " ").replace("-", " ").split())
	return _STATUS_ALIASES.get(key)


def _workflow_terminals() -> dict[str, int] | None:
	"""Terminal flag per Workflow State, or None when Grievance has no active workflow.

	A state is terminal when the workflow defines no transition out of it.
	Duplicate state rows (one per allow_edit role) collapse to a single flag.
	"""
	wf_name = frappe.db.get_value("Workflow", {"document_type": "Grievance", "is_active": 1}, "name")
	if not wf_name:
		return None

	rows = frappe.get_all(
		"Workflow Document State",
		filters={"parent": wf_name},
		fields=["state"],
		order_by="idx asc",
		ignore_permissions=True,
	)
	outgoing = set(
		frappe.get_all(
			"Workflow Transition",
			filters={"parent": wf_name},
			pluck="state",
			ignore_permissions=True,
		)
	)
	terminals: dict[str, int] = {}
	for row in rows:
		state = row.get("state")
		if not state or state in terminals:
			continue
		terminals[state] = 0 if state in outgoing else 1
	return terminals


def _is_terminal(workflow_states: tuple[str, ...], terminals: dict[str, int] | None) -> int:
	"""Terminal only when every mapped workflow state is terminal on the active Workflow.

	Missing workflow or absent states are treated as non-terminal — there is no
	per-card default.
	"""
	if not terminals or not workflow_states:
		return 0
	present = [terminals[state] for state in workflow_states if state in terminals]
	if not present:
		return 0
	return 1 if all(present) else 0


def _status_cards() -> list[dict]:
	"""Queue statuses for the caller, in display order, with terminal from the workflow."""
	terminals = _workflow_terminals()
	include_officer_cards = is_staff()
	cards = []
	order = 0
	for card in _STATUS_CARDS:
		if (card["officer_only"] and not include_officer_cards) or card.get("rolls_into"):
			continue
		order += 1
		is_terminal = _is_terminal(card["workflow_states"], terminals)
		cards.append(
			{
				"status": card["status"],
				"label": card["label"],
				"order": order,
				"is_terminal": is_terminal,
				"is_open": 0 if is_terminal else 1,
			}
		)
	return cards


def get_status_options() -> list[dict]:
	"""Queue statuses for filters. Workflow stages that are not cards are omitted.

	Assigned is included only for officers and other staff.
	"""
	return _status_cards()


# Shown instead of the stage while an escalated case waits on an officer. Not a workflow state
# and not a queue card: the `escalated` flag stays the record, and the `escalated` list filter
# finds these cases.
STATUS_ESCALATED = "Escalated"
_ESCALATED_SHOWN_IN = frozenset({C.STATE_ASSIGNED, C.STATE_REOPENED, C.STATE_IN_PROGRESS})


def public_status(workflow_status: str | None, escalated: bool = False) -> str:
	"""The queue status a workflow state is shown as.

	Unknown open states are In Progress. Assigned maps to itself for staff and to
	In Progress for submitters (who do not get an Assigned KPI card). An `escalated` case is
	shown as Escalated while it waits on an officer (Assigned, Reopened, In Progress); while it
	waits on the submitter, or once it is decided, it shows its stage.
	"""
	if not workflow_status:
		return ""
	if escalated and workflow_status in _ESCALATED_SHOWN_IN:
		return STATUS_ESCALATED
	canonical = _canonical_status(workflow_status)
	if canonical == "Assigned" and not is_staff():
		return "In Progress"
	return canonical or "In Progress"


def case_status(case) -> str:
	"""`public_status` of a Grievance, or of a row with its `status` and `escalated`."""
	return public_status(case.get("status"), bool(case.get("escalated")))


def currently_escalated(case) -> bool:
	"""Whether an escalated case is still open, which is what a response's `escalated` reports.

	The stored flag stays set once a case is decided, so reports and the ticket export can say it
	was escalated (`was_escalated` in a response). A Resolved, Closed, Rejected or Duplicate case is
	no longer in escalation and shows no Escalated badge.
	"""
	from oan_grievance_service.services import sla

	return bool(case.get("escalated")) and sla.sla_category_of(case.get("status")) != sla.STOPPED


def expand_status_filter(values: list[str]) -> list[str] | None:
	"""Workflow states a queue-status filter matches.

	None means All: every grievance. An empty list means the caller
	passed nothing usable. For submitters, officer-only states (Assigned) are
	included when filtering In Progress so they still match the rolled-up card.
	"""
	if not values:
		return []

	include_officer_cards = is_staff()
	selected: list[str] = []
	for raw in values:
		canonical = _canonical_status(str(raw))
		if canonical == "All":
			return None
		if canonical:
			selected.extend(_card_states(canonical))
			if canonical == "In Progress" and not include_officer_cards:
				for card in _STATUS_CARDS:
					if card["officer_only"]:
						selected.extend(card["workflow_states"])
			continue
		text = str(raw).strip()
		if text:
			selected.append(text)

	seen: list[str] = []
	for state in selected:
		if state not in seen:
			seen.append(state)
	return seen


def get_status_summary() -> list[dict]:
	"""Counts for the KPI cards, limited to grievances the caller is allowed to see.

	A workflow state that is not one of the caller's cards is
	counted under In Progress so every visible case still sits on exactly one
	card, and All equals the sum of the other cards. Assigned is officer-only.
	"""
	rows = frappe.get_list(
		"Grievance",
		fields=["status", {"COUNT": "*", "as": "total"}],
		group_by="status",
	)
	counts: dict[str, int] = {}
	for row in rows:
		status = row.get("status") or ""
		if not status:
			continue
		counts[status] = counts.get(status, 0) + int(row.get("total") or 0)

	visible = _status_cards()
	visible_statuses = {card["status"] for card in visible}
	mapped: set[str] = set()
	for card in _STATUS_CARDS:
		if card["status"] not in visible_statuses:
			continue
		mapped.update(_card_states(card["status"]))
	unmapped = sum(total for status, total in counts.items() if status not in mapped)
	all_count = sum(counts.values())

	cards = []
	for card in visible:
		if card["status"] == "All":
			count = all_count
		else:
			count = sum(counts.get(state, 0) for state in _card_states(card["status"]))
			if card["status"] == "In Progress":
				count += unmapped
		card["count"] = count
		cards.append(card)
	return cards


def get_preferred_languages() -> list[dict]:
	"""Supported notification languages."""
	return [
		{"code": "am", "label": "Amharic"},
		{"code": "en", "label": "English"},
	]


def get_jurisdiction_countries() -> list[str]:
	"""Active country names from the Administrative Area tree (fallback: Ethiopia)."""
	country_nodes = frappe.get_all(
		"Grievance Administrative Area",
		filters={"level_name": "Country", "is_active": 1},
		fields=["area_name", "code", "country"],
		order_by="area_name asc",
		ignore_permissions=True,
	)

	names: set[str] = set()
	for node in country_nodes:
		if node.get("country"):
			names.add(node["country"])
		if node.get("area_name"):
			names.add(node["area_name"])

	if not names:
		distinct_countries = frappe.get_all(
			"Grievance Administrative Area",
			filters={"is_active": 1, "country": ["is", "set"]},
			distinct=True,
			pluck="country",
			ignore_permissions=True,
		)
		names = {c.strip() for c in distinct_countries if c and c.strip()}

	return sorted(names) if names else ["Ethiopia"]


def get_phone_extensions(search: str | None = None, country: str | None = None) -> list[dict]:
	"""Phone extensions for active jurisdiction countries only (Administrative Area)."""
	from frappe.geo.country_info import get_all

	all_geo_data = get_all()
	jurisdiction_names = get_jurisdiction_countries()

	extensions = []
	for j_name in jurisdiction_names:
		info = all_geo_data.get(j_name)
		if not info:
			for c_name, c_info in all_geo_data.items():
				if c_name.lower() == j_name.lower() or (c_info.get("code") or "").lower() == j_name.lower():
					info = c_info
					j_name = c_name
					break

		if info and info.get("isd"):
			isd_str = str(info["isd"]).strip()
			if not isd_str.startswith("+"):
				isd_str = f"+{isd_str}"
			extensions.append(
				{
					"country": j_name,
					"code": (info.get("code") or "").upper(),
					"isd": isd_str,
				}
			)

	if not extensions:
		extensions = [{"country": "Ethiopia", "code": "ET", "isd": "+251"}]

	extensions.sort(key=lambda x: (x["country"] != "Ethiopia", x["country"]))

	if country:
		c_term = country.strip().upper()
		extensions = [e for e in extensions if e["code"] == c_term or e["country"].upper() == c_term]

	if search:
		s_term = search.strip().lower()
		extensions = [
			e
			for e in extensions
			if s_term in e["country"].lower() or s_term in e["code"].lower() or s_term in e["isd"].lower()
		]

	return extensions


def clear_reference_cache(doc=None, method=None):
	"""Drop the cached lookups. Wired to master changes in hooks.

	Frappe calls doc_events handlers with (doc, method); neither is needed here
	because the whole prefix is dropped rather than one key.
	"""
	frappe.cache().delete_keys("grievance:")
