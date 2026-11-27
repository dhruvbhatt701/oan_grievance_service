"""The parts of a case's view that are safe for its submitter, defined once.

`grievance.timeline` (signed-in submitters and staff) and the link-and-PIN guest view
(`guest_access`, STG-518) both build on these, so a shared field means the same in both. Only
what a submitter may see is here: `timeline` adds its officer-only parts itself, behind its own
`is_staff` checks, and the guest view returns only the fields it names.

- Timeline entries: the query, the formatting context and `format_timeline_event`, with internal
  entries only when asked for.
- Attachments: one field list, and files on internal entries left out unless asked for.
- Case summary: the submitter-facing fields of the case.
"""

import frappe
from frappe import _
from frappe.utils import get_datetime
from oan_auth_service.api.utils import to_tz_aware_iso

from oan_grievance_service import permissions
from oan_grievance_service.api.v1._options import case_status
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import response_body, timeline_changes
from oan_grievance_service.services import ticket_number as tn

# The case's files as every view lists them. `file_size` is the attachment's `size_bytes`.
ATTACHMENT_FIELDS = (
	"name",
	"file_name",
	"file_url",
	"size_bytes as file_size",
	"mime_type",
	"document_type",
	"scan_status",
	"timeline_entry",
	"uploaded_by_user",
	"uploaded_by_submitter",
	"creation",
)


# Timeline entries
# ----------------


def resolve_timeline_author(
	author_user: str | None,
	author_type: str | None = None,
	submitter_type: str | None = None,
) -> tuple[str, str]:
	"""Determine author_type and author_role for timeline formatting."""
	if author_type == "submitter":
		return "submitter", submitter_type or "Grievance Submitter"
	if author_type == "officer":
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			current_level_of,
		)

		level_id = current_level_of(author_user) if author_user else None
		role_name = (
			frappe.db.get_value("Grievance Role Level", level_id, "level_name") if level_id else None
		) or "Grievance Officer"
		return "officer", role_name
	if author_user:
		if permissions.is_staff(author_user):
			from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
				current_level_of,
			)

			level_id = current_level_of(author_user)
			role_name = (
				frappe.db.get_value("Grievance Role Level", level_id, "level_name") if level_id else None
			) or "Grievance Officer"
			return "officer", role_name
		return "submitter", submitter_type or "Grievance Submitter"
	return "system", "System"


def history_details_for(history_names):
	"""The status history details keyed by row name."""
	if not history_names:
		return {}
	records = frappe.get_all(
		"Grievance Status History",
		filters={"name": ["in", list(history_names)]},
		fields=["name", "action", "from_status", "to_status", "changed_by"],
	)
	return {r["name"]: r for r in records}


def response_numbers_for(doc_name, up_to_name=None):
	"""The department responses of a case numbered in order, keyed by timeline entry."""
	public_entries = frappe.get_all(
		"Grievance Timeline",
		filters={"grievance": doc_name, "is_internal": 0},
		fields=[
			"name",
			"entry_type",
			"author_user",
			"author_type",
			"body",
			"ref_doctype",
			"ref_docname",
		],
		order_by="created_on asc, name asc",
	)

	if not public_entries:
		return {}

	submitter_type = frappe.db.get_value("Grievance", doc_name, "submitter_type")

	response_numbers = {}
	count = 1
	for e in public_entries:
		a_type, _role = resolve_timeline_author(
			e.get("author_user"),
			author_type=e.get("author_type"),
			submitter_type=submitter_type,
		)
		e_type = e.get("entry_type")
		if e_type in ("response", "dept_response") or (a_type == "officer" and e_type == "message"):
			e_type = "dept_response"

		if e_type == "dept_response":
			response_numbers[e["name"]] = count
			count += 1

		if up_to_name and e["name"] == up_to_name:
			break

	return response_numbers


def format_timeline_event(entry, doc, from_status=None, to_status=None, context=None):
	"""One timeline entry as the API shows it to the current user."""
	if not entry:
		return None
	context = context or {}
	history_map = context.get("history_map", {})
	user_names = context.get("user_names", {})

	is_dict = isinstance(entry, dict)

	def get_field(f, d=None):
		return entry.get(f, d) if is_dict else getattr(entry, f, d)

	author_user = get_field("author_user")
	author_type_field = get_field("author_type")
	author_type, author_role = resolve_timeline_author(
		author_user,
		author_type=author_type_field,
		submitter_type=doc.submitter_type,
	)

	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	ref_name = get_field("ref_docname")
	if ref_name and not history_map and not context.get("history_fetched"):
		history_map = history_details_for([ref_name])

	hist = history_map.get(ref_name) or {}
	action_val = hist.get("action")
	from_status_val = from_status or hist.get("from_status")
	to_status_val = to_status or hist.get("to_status")

	author_name = None
	if is_staff:
		if author_type == "officer" and author_user:
			author_name = (
				user_names.get(author_user)
				or frappe.db.get_value("User", author_user, "full_name")
				or author_user
			)
		elif author_type == "submitter":
			show_id = permissions.can_see_identity(doc)
			author_name = doc.submitter_name if show_id else _("Anonymous Submitter")
	else:
		if author_type == "submitter":
			author_name = doc.submitter_name

	created_val = get_field("created_on")
	body = get_field("body")
	entry_type = get_field("entry_type")
	is_internal = bool(get_field("is_internal", False))
	entry_name = get_field("name")

	if not is_internal and (
		entry_type in ("response", "dept_response") or (author_type == "officer" and entry_type == "message")
	):
		entry_type = "dept_response"

	parts = response_body.split(body)

	# A change request's note is stored with raw ids; the API shows names. Only the
	# internal note: the public assignment line is already plain text.
	changes = None
	if is_internal and ref_name and get_field("ref_doctype") == timeline_changes.CHANGE_REQUEST:
		change_requests = context.get("change_requests")
		if change_requests is None:
			change_requests = timeline_changes.load([ref_name])
		shown = change_requests.get(ref_name)
		if shown:
			body, changes = shown["body"], shown["changes"]

	result = {
		"id": entry_name,
		"name": entry_name,
		"entry_type": entry_type,
		"body": body,
		"is_internal": is_internal,
		"author_role": author_role,
		"author_type": author_type,
		"created_on": to_tz_aware_iso(created_val),
		# Explicitly include null values for backward compatibility
		"from_status": from_status_val,
		"to_status": to_status_val,
	}

	if entry_type == "dept_response":
		response_numbers = context.get("response_numbers")
		if response_numbers is None:
			response_numbers = response_numbers_for(doc.name, up_to_name=entry_name)
		result["response_number"] = response_numbers.get(entry_name) or 1

	if changes is not None:
		result["changes"] = changes
	if action_val:
		result["action"] = action_val
	if parts:
		result["action_taken"] = parts.get("action_taken")
		result["resolution_summary"] = parts.get("resolution_summary")
	else:
		result["action_taken"] = None
		result["resolution_summary"] = None
	if author_name:
		result["author_name"] = author_name

	return result


def timeline_entries(
	doc,
	*,
	include_internal: bool,
	internal: bool | str | None = None,
	limit: int | None = None,
	cursor: str | None = None,
	officer_names: bool = False,
) -> tuple[list[dict], bool, str | None]:
	"""The case's timeline entries, oldest first, formatted: (entries, has_more, next_cursor).

	Internal entries only with `include_internal`; then `internal` narrows to internal (true) or
	public (false) entries. `officer_names` looks up the officers' full names once for the page,
	for a staff caller. `cursor` is the `next_cursor` of the page before.
	"""
	timeline_dt = frappe.qb.DocType("Grievance Timeline")
	query = (
		frappe.qb.from_(timeline_dt)
		.select(
			timeline_dt.name,
			timeline_dt.entry_type,
			timeline_dt.is_internal,
			timeline_dt.body,
			timeline_dt.author_user,
			timeline_dt.author_type,
			timeline_dt.ref_doctype,
			timeline_dt.ref_docname,
			timeline_dt.created_on,
		)
		.where(timeline_dt.grievance == doc.name)
	)

	if not include_internal:
		query = query.where(timeline_dt.is_internal == 0)
	elif internal is not None:
		query = query.where(
			timeline_dt.is_internal == (1 if str(internal).lower() in ("1", "true", "yes") else 0)
		)

	if cursor:
		try:
			cursor_ts_raw, cursor_name = cursor.strip().split("|", 1)
			cursor_ts = get_datetime(cursor_ts_raw)
		except ValueError:
			frappe.throw(_("Invalid cursor."), frappe.ValidationError)
		query = query.where(
			(timeline_dt.created_on > cursor_ts)
			| ((timeline_dt.created_on == cursor_ts) & (timeline_dt.name > cursor_name))
		)

	query = query.orderby(timeline_dt.created_on, order=frappe.qb.asc).orderby(
		timeline_dt.name, order=frappe.qb.asc
	)
	if limit:
		query = query.limit(limit + 1)
	entries = query.run(as_dict=True)

	has_more = bool(limit) and len(entries) > limit
	if has_more:
		entries = entries[:limit]
	next_cursor = (
		f"{get_datetime(entries[-1]['created_on']).isoformat(sep=' ')}|{entries[-1]['name']}"
		if has_more and entries
		else None
	)

	user_names = {}
	officer_users = {e.get("author_user") for e in entries if officer_names and e.get("author_user")}
	if officer_users:
		user_names = {
			row.name: (row.full_name or row.name)
			for row in frappe.get_all(
				"User",
				filters={"name": ["in", list(officer_users)]},
				fields=["name", "full_name"],
			)
		}
	context = {
		"history_map": history_details_for(
			{e["ref_docname"] for e in entries if e.get("ref_doctype") == "Grievance Status History"}
		),
		"user_names": user_names,
		"history_fetched": True,
		"response_numbers": response_numbers_for(doc.name),
		"change_requests": timeline_changes.load(
			{
				e["ref_docname"]
				for e in entries
				if e.get("is_internal") and e.get("ref_doctype") == timeline_changes.CHANGE_REQUEST
			}
		),
	}

	formatted = [event for entry in entries if (event := format_timeline_event(entry, doc, context=context))]
	return formatted, has_more, next_cursor


# Attachments
# -----------


def case_attachments(doc, *, include_internal: bool) -> list[dict]:
	"""The case's files, oldest first, with their scan verdicts.

	Without `include_internal`, files on an internal entry are left out: evidence an officer put
	on an internal note is staff-only, as the note is. Rows are read past the attachment's own
	permission hook, which withholds unscanned files; the caller has already checked the case.
	"""
	rows = frappe.get_all(
		"Grievance Attachment",
		filters={"grievance": doc.name},
		fields=list(ATTACHMENT_FIELDS),
		order_by="creation asc",
		ignore_permissions=True,
	)
	if include_internal:
		return rows
	internal_entries = set(
		frappe.get_all("Grievance Timeline", filters={"grievance": doc.name, "is_internal": 1}, pluck="name")
	)
	return [row for row in rows if row.timeline_entry not in internal_entries]


def attachments_by_entry(attachments) -> dict[str, list]:
	"""`attachments` grouped by the timeline entry each hangs off; files on none are left out."""
	grouped = {}
	for attachment in attachments:
		if attachment.get("timeline_entry"):
			grouped.setdefault(attachment["timeline_entry"], []).append(attachment)
	return grouped


# Case summary
# ------------


def case_summary(doc, area_hierarchy=None) -> dict:
	"""The case as its submitter is told about it: reference, status, what was asked, where, when.

	`area_hierarchy` is the case area's hierarchy when the caller has it already.
	"""
	from oan_grievance_service.services.administrative_area import (
		format_administrative_location,
		get_administrative_hierarchy,
	)

	if area_hierarchy is None:
		area_hierarchy = get_administrative_hierarchy(doc.administrative_area)
	grievance_type_name = (
		frappe.db.get_value("Grievance Type", doc.grievance_type, "type_name") if doc.grievance_type else None
	)
	return {
		"ticket_number": tn.display(doc.ticket_number),
		"status": case_status(doc),
		"description": doc.description,
		"desired_outcome": doc.desired_outcome,
		"service_category": doc.service_category,
		"grievance_type_name": grievance_type_name or doc.grievance_type,
		"location": format_administrative_location(area_hierarchy),
		"sla": {
			"sla_due_date": to_tz_aware_iso(doc.sla_due_date),
			# Kept under its old name for existing clients: the confirmation window is the Resolved
			# state's timer.
			"confirmation_deadline": (
				to_tz_aware_iso(doc.state_deadline) if doc.workflow_state == C.STATE_RESOLVED else None
			),
		},
	}
