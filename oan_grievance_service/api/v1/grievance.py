"""FR-02 submission and FR-06 submitter actions, exposed for the mobile app, web
portal, IVR and call centre channels described in FSD 3.2.1.

Every entry point is whitelisted, validates its own input, and routes through the
service layer so the audit trail and notifications cannot be bypassed.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime
from oan_auth_service.api.router import prefixed
from oan_auth_service.api.utils import (
	SafeEmail,
	handle_api_errors,
	parse_multi_value,
	require_role,
	success_response,
	validate_request,
)
from pydantic import BaseModel, Field

from oan_grievance_service.api.v1._options import get_grievance_types, get_service_categories
from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.services import assignment, audit, identity, lifecycle, routing, sla, submission
from oan_grievance_service.services import constants as C

# Aliased: several entry points take a `ticket_number` argument, which would
# otherwise shadow the module inside them.
from oan_grievance_service.services import ticket_number as tn

route = prefixed("/api/v1/grievances")


class SubmitGrievanceRequest(BaseModel):
	"""Case fields may arrive on the request, from a draft, or from the profile.

	Authenticated submitters omit type/name/mobile — `_resolve_submitter_identity`
	fills them from the session profile, and `CLIENT_IMMUTABLE_FIELDS` ignores any
	client-supplied copies. Walk-in/IVR staff still send them.

	`administrative_area` may be omitted when intake sends `woreda` / `kebele`.
	Wizard fields may live only on the draft (`client_uuid`); after merge,
	`identity.validate_submission_payload(require_presence=True)` enforces them
	with per-field errors (STG-321 / STG-328).

	Field names match draft `payload` keys exactly
	(`submission.SHARED_SUBMISSION_FIELD_KEYS`) — no renaming on carry-over.

	Phone is plain optional str at the schema edge (not SafePhone): bare Ethiopian
	9-digit numbers are accepted and normalised in the domain layer, then checked
	with `oan_auth_service.api.utils.validate_phone_string`. Email uses SafeEmail.
	"""

	model_config = {"extra": "allow"}

	submitter_type: str | None = None
	submitter_name: str | None = None
	contact_mobile: str | None = None
	submission_channel: str | None = None
	administrative_area: str | None = None
	service_category: str | None = None
	grievance_type: str | None = None
	description: str | None = None
	contact_email: SafeEmail | None = None
	assisted_by_officer: str | None = None
	is_anonymous: int | None = Field(0, ge=0, le=1)
	consent_given: int | None = Field(None, ge=0, le=1)
	client_uuid: str | None = None
	client_submission_uuid: str | None = None
	desired_outcome: str | None = None
	administrative_unit: str | None = None


ALLOWED_GRIEVANCE_ROLES = [
	"Grievance Submitter",
	"Grievance Officer",
	"Grievance Admin",
	"System Manager",
	"Administrator",
]


def active_channels() -> list[str]:
	"""The intake channels currently open, read from the master.

	Held as data rather than a tuple here because opening a channel is an
	operational decision, not a release: `Grievance Submission Type` is seeded by
	install.py and editable from the desk thereafter. `is_active` is what closes one,
	so a channel that is retired still resolves on historical grievances.
	"""
	return frappe.get_all(
		"Grievance Submission Type",
		filters={"is_active": 1},
		pluck="name",
		order_by="submission_type_name asc",
	)


# Staff may file on another person's behalf; a submitter may only file as themselves.
STAFF_ROLES = frozenset({"Grievance Officer", "Grievance Admin", "System Manager", "Administrator"})

# Columns the server derives on submission. Accepting any of these from the caller
# would let a client forge case ownership -- permissions.py scopes read and write
# access on `submitter` and `assisted_by_officer` -- or rewrite the filing-time
# area snapshot that Grievance.set_administrative_area_metadata is meant to own.
CLIENT_IMMUTABLE_FIELDS = frozenset(
	{
		"submitter",
		"submitter_name",
		"contact_mobile",
		"contact_email",
		"assisted_by_officer",
		"area_lft",
		"area_path_code",
		"status",
	}
)


def _resolve_submitter_identity(kwargs):
	"""Decide who a grievance belongs to, server-side, and snapshot their profile.

	A submitter always files as themselves: the session decides the owning profile,
	never the request. Staff may file on someone's behalf for the assisted and call
	centre channels, in which case the named submitter stays the owner and the staff
	member is recorded as the assisting officer for the audit trail.

	The name and contact columns are a filing-time copy of the profile, so a case
	keeps showing who reported it even after they later change their phone number.
	Staff taking a walk-in or IVR report from someone with no profile yet supply
	those details directly, because there is nothing to copy from.
	"""
	user = frappe.session.user
	is_staff = bool(set(frappe.get_roles(user)) & STAFF_ROLES)

	if is_staff:
		profile_name = kwargs.get("submitter")
		identity = {"submitter": profile_name, "assisted_by_officer": user}
	else:
		profile_name = frappe.db.get_value("Grievance Submitter Profile", {"user": user}, "name")
		if not profile_name:
			frappe.throw(
				_("No submitter profile associated with your user account."),
				title=_("Profile Not Found"),
			)
		identity = {"submitter": profile_name, "assisted_by_officer": None}

	if not profile_name:
		identity.update(
			{
				"submitter_name": kwargs.get("submitter_name"),
				"contact_mobile": kwargs.get("contact_mobile"),
				"contact_email": kwargs.get("contact_email"),
			}
		)
		return identity

	profile = frappe.db.get_value(
		"Grievance Submitter Profile",
		profile_name,
		["submitter_type", "submitter_name", "contact_mobile", "contact_email", "active", "is_blocked"],
		as_dict=True,
	)
	if not profile:
		frappe.throw(_("Unknown submitter profile."), title=_("Invalid Submitter"))

	# The schema records is_blocked as blocking new submissions while leaving existing
	# cases visible, so it is enforced here rather than in the permission layer.
	if profile.is_blocked or not profile.active:
		frappe.throw(
			_("This submitter profile cannot file new grievances."),
			title=_("Submitter Blocked"),
		)

	identity.update(
		{
			"submitter_type": profile.submitter_type,
			"submitter_name": profile.submitter_name,
			"contact_mobile": profile.contact_mobile,
			"contact_email": profile.contact_email,
		}
	)
	return identity


def resolve_administrative_area(area_identifier):
	"""Resolve an area identifier (ID, path_code, or unique code) to canonical doc name.

	Note: area_name is intentionally excluded because display names recur across
	regions/woredas (e.g. over 100 kebeles named '1' or '2') and resolving by name
	causes silent misrouting to an arbitrary region.
	"""
	if not area_identifier:
		return None
	if frappe.db.exists("Grievance Administrative Area", area_identifier):
		return area_identifier
	return frappe.db.get_value(
		"Grievance Administrative Area", {"path_code": area_identifier}, "name"
	) or frappe.db.get_value("Grievance Administrative Area", {"code": area_identifier}, "name")


@route("", methods=("POST",), summary="Submit a new grievance")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
@validate_request(SubmitGrievanceRequest)
def submit(**kwargs):
	"""FSD 4.1: validate, generate the ticket, acknowledge, then route.

	Returns the ticket number and the acknowledgement outcome, which is what the
	FSD 3.11.5 wizard success state displays.
	"""
	from oan_grievance_service.services import notifications

	# STG-328 / STG-325: draft wizard state is the base; request values overlay.
	kwargs, already_from_draft = submission.merge_draft_into_submission(kwargs)
	if already_from_draft:
		return _existing_grievance_response(already_from_draft)

	# Identity is resolved first so the required-field check sees the profile snapshot:
	# a submitter filing for themselves need not send back their own name and number.
	resolved_identity = _resolve_submitter_identity(kwargs)
	resolved = {
		**{field: value for field, value in kwargs.items() if field not in CLIENT_IMMUTABLE_FIELDS},
		**resolved_identity,
	}

	# Support intake forms providing woreda and optional kebele:
	canonical_area = resolve_administrative_area(resolved.get("administrative_area"))
	if not canonical_area and kwargs.get("kebele"):
		canonical_area = resolve_administrative_area(kwargs.get("kebele"))
	if not canonical_area and kwargs.get("woreda"):
		canonical_area = resolve_administrative_area(kwargs.get("woreda"))

	if canonical_area:
		resolved["administrative_area"] = canonical_area

	# If kebele was provided as free text and administrative_unit is empty, preserve it
	if kwargs.get("kebele") and not resolved.get("administrative_unit"):
		if resolved.get("administrative_area") != kwargs.get("kebele"):
			resolved["administrative_unit"] = kwargs.get("kebele")

	# Resolve grievance_type if caller provided the type_name instead of document ID
	gtype = resolved.get("grievance_type")
	if gtype and not frappe.db.exists("Grievance Type", gtype):
		gtype_id = frappe.db.get_value(
			"Grievance Type",
			{"type_name": gtype, "is_active": 1},
			"name",
		)
		if gtype_id:
			resolved["grievance_type"] = gtype_id

	# Normalise before insert so the doc (and find_or_create_submitter) get +251…
	# form even when contact_mobile came from the profile snapshot, not kwargs.
	if resolved.get("contact_mobile"):
		resolved["contact_mobile"] = submission.normalise_mobile(resolved["contact_mobile"])
	kwargs["contact_mobile"] = resolved.get("contact_mobile")

	# Per-field required + format validation (STG-321 / STG-328) before DocType insert.
	identity.validate_submission_payload(resolved, require_presence=True)

	# The Link field already refuses a channel that does not exist. This refuses one
	# that exists but has been switched off, which the link check cannot see.
	if resolved.get("submission_channel") not in active_channels():
		frappe.throw(_("Unknown or closed submission channel."), title=_("Invalid Channel"))

	# A retry must not lodge a second case. This read settles the ordinary retry --
	# one that arrives after the first attempt committed. It cannot settle two
	# retries in flight at once, because both would read nothing and both would
	# insert; that case is caught on the unique index at insert time below.
	client_submission_uuid = kwargs.get("client_submission_uuid")
	if client_submission_uuid:
		original = _existing_submission(client_submission_uuid)
		if original:
			return original

	doc = frappe.new_doc("Grievance")
	for field, value in resolved.items():
		if doc.meta.has_field(field):
			doc.set(field, value)
	# Born a Draft; the Submit action below is what makes it a grievance.
	doc.workflow_state = C.DRAFT
	# FR-02 duplicate detection matches on the submitter, so a grievance without one
	# can never be found to duplicate anything. Only fall back to creating a profile
	# when identity resolution found none -- staff taking a walk-in or IVR report
	# from someone who has never registered. Overwriting unconditionally would throw
	# away the session-resolved profile and let a submitter file against a profile of
	# their own choosing by varying contact_mobile.
	if not doc.submitter:
		doc.submitter = submission.find_or_create_submitter(resolved)
	submission.record_consent(doc)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.UniqueValidationError:
		# Another retry carrying the same client_submission_uuid committed while this
		# one was building its document. The index is the only thing that can settle
		# that race, and it just did: hand back the ticket the winner created rather
		# than a 500 the client cannot act on.
		if not client_submission_uuid:
			raise
		frappe.db.rollback()
		original = _existing_submission(client_submission_uuid)
		if not original:
			raise
		return original

	# FSD 4.1 step 5: Draft to Submitted through the workflow, which submits the
	# document and, with it, freezes what the submitter filed.
	lifecycle.submit(doc)

	if kwargs.get("is_anonymous"):
		_request_anonymity(doc, kwargs.get("anonymity_justification"))

	attachments = _claim_draft(kwargs.get("client_uuid"), doc)
	duplicates = detect_duplicates(doc)

	# FSD 4.1 step 6: acknowledge before routing, so the submitter always gets a ticket.
	notifications.queue(doc, C.EVENT_SUBMISSION_RECEIVED)
	if duplicates:
		notifications.queue(doc, C.EVENT_DUPLICATE_DETECTED)

	# FSD 4.1 step 7: routing decides auto-assignment or the manual queue.
	rule = routing.apply_routing(doc)
	doc.reload()

	return success_response(
		data={
			"ticket_number": doc.ticket_number,
			"status": doc.status,
			"assigned_department": doc.assigned_dept,
			"auto_routed": bool(rule),
			"sla_due_date": doc.sla_due_date,
			"possible_duplicates": [d.duplicate_of for d in duplicates],
			"area_path_code": doc.area_path_code,
			"attachments": attachments,
			"duplicate_submission": False,
		},
		message=_("Grievance submitted successfully"),
	)


def _existing_submission(client_uuid):
	"""The response for an already-lodged submission, or None if there isn't one.

	Shared by the pre-insert check and the unique-index recovery so a retry gets the
	same answer whichever of the two settles it.
	"""
	existing = frappe.db.get_value(
		"Grievance",
		{"client_submission_uuid": client_uuid},
		["name", "ticket_number", "status"],
		as_dict=True,
	)
	if not existing:
		return None

	return success_response(
		data={
			"ticket_number": existing.ticket_number,
			"status": existing.status,
			"duplicate_submission": True,
		},
		message=_("Grievance already submitted"),
	)


def _existing_grievance_response(grievance_name):
	"""Idempotent reply when a draft was already claimed as this grievance."""
	existing = frappe.db.get_value(
		"Grievance",
		grievance_name,
		["name", "ticket_number", "status"],
		as_dict=True,
	)
	if not existing:
		frappe.throw(_("No grievance found for that draft."), title=_("Not Found"))
	return success_response(
		data={
			"ticket_number": existing.ticket_number,
			"status": existing.status,
			"duplicate_submission": True,
		},
		message=_("Grievance already submitted"),
	)


def _request_anonymity(doc, justification):
	"""FSD 9.2: anonymity is requested at submission and approved separately."""
	frappe.get_doc(
		{
			"doctype": "Grievance Anonymity Request",
			"grievance": doc.name,
			# The request and the grievance use different vocabularies: the request
			# is "Pending", the flag it drives on the grievance is "Pending Approval".
			"status": "Pending",
			"requested_at": now_datetime(),
			"justification": justification,
		}
	).insert(ignore_permissions=True)
	doc.db_set("anonymity_status", "Pending Approval", update_modified=False)


def _claim_draft(client_uuid, doc):
	"""Bind the draft this submission came from to the grievance it became, and
	move any files uploaded against it."""
	if not client_uuid:
		return 0

	draft_name = frappe.db.get_value("Grievance Draft", {"client_uuid": client_uuid}, "name")
	if not draft_name:
		return 0

	draft = frappe.get_doc("Grievance Draft", draft_name)
	submission._assert_draft_owner(draft)
	if draft.submitted_as and draft.submitted_as != doc.name:
		frappe.throw(
			_("This draft has already been submitted as {0}.").format(draft.submitted_as),
			title=_("Already Submitted"),
		)

	moved = submission.attach_draft_files(draft_name, doc.name)
	frappe.db.set_value("Grievance Draft", draft_name, "submitted_as", doc.name, update_modified=False)
	return moved


def detect_duplicates(grievance, window_days=7):
	"""FSD 3.2.3 / E3: match on submitter identity, grievance type and time proximity."""
	if not grievance.submitter:
		return []

	candidates = frappe.get_all(
		"Grievance",
		filters={
			"name": ["!=", grievance.name],
			"submitter": grievance.submitter,
			"grievance_type": grievance.grievance_type,
			"creation": [">=", frappe.utils.add_days(now_datetime(), -window_days)],
		},
		pluck="name",
	)

	rows = []
	for candidate in candidates:
		rows.append(
			frappe.get_doc(
				{
					"doctype": "Grievance Duplicate",
					"grievance": grievance.name,
					"duplicate_of": candidate,
					"detected_at": now_datetime(),
					"detection_method": "Identity + Type + Time Proximity",
					"similarity_score": 1.0,
				}
			).insert(ignore_permissions=True)
		)
	return rows


def _resolve_area_filter_identifier(identifier: str) -> str:
	"""Resolve an area identifier, path_code, code, or region area_name for filtering."""
	if not identifier:
		return ""
	identifier = str(identifier).strip()
	resolved = resolve_administrative_area(identifier)
	if resolved:
		return resolved
	# Check if identifier is a Region by display area_name (e.g. 'Oromia', 'Amhara')
	region_doc = frappe.db.get_value(
		"Grievance Administrative Area",
		{"area_name": identifier, "level_name": "Region"},
		"name",
	)
	if region_doc:
		return region_doc
	# Fallback to general area_name
	return (
		frappe.db.get_value("Grievance Administrative Area", {"area_name": identifier}, "name") or identifier
	)


@route("", methods=("GET",), summary="List grievances with filtering, pagination, and sorting")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def list_grievances(
	page: int | str = 1,
	page_size: int | str = 20,
	limit: int | str | None = None,
	status: str | list | None = None,
	service_category: str | list | None = None,
	category: str | list | None = None,
	grievance_type: str | list | None = None,
	assigned_dept: str | list | None = None,
	department: str | list | None = None,
	assigned_to: str | None = None,
	administrative_area: str | list | None = None,
	region: str | list | None = None,
	submission_channel: str | list | None = None,
	escalated: bool | str | None = None,
	is_escalated: bool | str | None = None,
	is_anonymous: bool | str | None = None,
	submitter: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	search: str | None = None,
	sort_by: str = "creation",
	sort_order: str = "desc",
	**kwargs,
):
	"""Retrieve paginated and filtered list of grievances.

	Enforces deny-by-default RBAC through permission query conditions:
	- Grievance Submitters only see their own cases and assisted submissions.
	- Grievance Officers only see cases matching their RBAC scope (administrative area subtree,
	  department, category) or directly assigned to them.
	- Grievance Admins and System Managers see all cases.

	Supports multi-select values (list, JSON array, or comma-separated string) for status,
	service_category/category, administrative_area/region, grievance_type, department, and submission_channel.
	"""
	import math

	page_num = max(1, int(page))
	effective_limit = limit if limit is not None else page_size
	page_size_num = min(100, max(1, int(effective_limit)))
	offset = (page_num - 1) * page_size_num

	filters = []

	status_list = parse_multi_value(status or kwargs.get("status"))
	if len(status_list) == 1:
		filters.append(["status", "=", status_list[0]])
	elif len(status_list) > 1:
		filters.append(["status", "in", status_list])

	cat_list = parse_multi_value(service_category or category or kwargs.get("category"))
	if len(cat_list) == 1:
		filters.append(["service_category", "=", cat_list[0]])
	elif len(cat_list) > 1:
		filters.append(["service_category", "in", cat_list])

	type_list = parse_multi_value(grievance_type or kwargs.get("type"))
	if len(type_list) == 1:
		filters.append(["grievance_type", "=", type_list[0]])
	elif len(type_list) > 1:
		filters.append(["grievance_type", "in", type_list])

	dept_list = parse_multi_value(assigned_dept or department or kwargs.get("dept"))
	if len(dept_list) == 1:
		filters.append(["assigned_dept", "=", dept_list[0]])
	elif len(dept_list) > 1:
		filters.append(["assigned_dept", "in", dept_list])

	if assigned_to:
		target_user = frappe.session.user if assigned_to == "me" else assigned_to
		filters.append(["assigned_to", "=", target_user])

	channel_list = parse_multi_value(submission_channel or kwargs.get("channel"))
	if len(channel_list) == 1:
		filters.append(["submission_channel", "=", channel_list[0]])
	elif len(channel_list) > 1:
		filters.append(["submission_channel", "in", channel_list])

	if submitter:
		if submitter == "me":
			profile_name = frappe.db.get_value(
				"Grievance Submitter Profile", {"user": frappe.session.user}, "name"
			)
			if profile_name:
				filters.append(["submitter", "=", profile_name])
		else:
			filters.append(["submitter", "=", submitter])

	if is_anonymous is not None:
		val = 1 if str(is_anonymous).lower() in ("1", "true", "yes") else 0
		filters.append(["is_anonymous", "=", val])

	esc = is_escalated if is_escalated is not None else escalated
	if esc is not None:
		val = 1 if str(esc).lower() in ("1", "true", "yes") else 0
		filters.append(["escalated", "=", val])

	if from_date:
		filters.append(["creation", ">=", f"{from_date} 00:00:00" if len(from_date) == 10 else from_date])

	if to_date:
		filters.append(["creation", "<=", f"{to_date} 23:59:59" if len(to_date) == 10 else to_date])

	area_list = parse_multi_value(administrative_area or region or kwargs.get("region"))
	if len(area_list) == 1:
		canonical_area = _resolve_area_filter_identifier(area_list[0])
		area_bounds = frappe.db.get_value(
			"Grievance Administrative Area",
			canonical_area,
			["lft", "rgt"],
			as_dict=True,
		)
		if area_bounds and area_bounds.lft is not None and area_bounds.rgt is not None:
			filters.append(["area_lft", ">=", int(area_bounds.lft)])
			filters.append(["area_lft", "<=", int(area_bounds.rgt)])
		else:
			filters.append(["administrative_area", "=", canonical_area])
	elif len(area_list) > 1:
		area_names = set()
		for item in area_list:
			canonical = _resolve_area_filter_identifier(item)
			bounds = frappe.db.get_value(
				"Grievance Administrative Area",
				canonical,
				["lft", "rgt", "is_group"],
				as_dict=True,
			)
			if bounds and bounds.lft is not None and bounds.rgt is not None:
				if bounds.get("is_group") or (bounds.rgt - bounds.lft > 1):
					descendants = frappe.get_all(
						"Grievance Administrative Area",
						filters=[["lft", ">=", int(bounds.lft)], ["lft", "<=", int(bounds.rgt)]],
						pluck="name",
					)
					area_names.update(descendants)
				else:
					area_names.add(canonical)
			else:
				area_names.add(canonical)
		if area_names:
			filters.append(["administrative_area", "in", list(area_names)])

	or_filters = []
	if search:
		search_pattern = f"%{search.strip()}%"
		matching_types = frappe.get_all(
			"Grievance Type",
			filters={"type_name": ["like", search_pattern]},
			pluck="name",
		)
		or_filters = [
			["ticket_number", "like", search_pattern],
			["name", "like", search_pattern],
			["submitter_name", "like", search_pattern],
		]
		if matching_types:
			or_filters.append(["grievance_type", "in", matching_types])
		else:
			or_filters.append(["grievance_type", "like", search_pattern])

	allowed_sort_fields = {
		"creation",
		"modified",
		"ticket_number",
		"status",
		"sla_due_date",
		"service_category",
		"grievance_type",
	}
	order_field = sort_by if sort_by in allowed_sort_fields else "creation"
	order_direction = "asc" if str(sort_order).lower() == "asc" else "desc"
	order_by = f"`tabGrievance`.{order_field} {order_direction}"

	fields = [
		"name",
		"ticket_number",
		"status",
		"escalated",
		"submission_channel",
		"submitter",
		"submitter_name",
		"contact_mobile",
		"contact_email",
		"is_anonymous",
		"administrative_area",
		"service_category",
		"grievance_type",
		"description",
		"assigned_dept",
		"assigned_to",
		"sla_due_date",
		"confirmation_deadline",
		"creation as submitted_on",
		"modified as updated_at",
	]

	items = frappe.get_list(
		"Grievance",
		filters=filters,
		or_filters=or_filters if or_filters else None,
		fields=fields,
		order_by=order_by,
		start=offset,
		page_length=page_size_num,
	)

	total_records = frappe.get_list(
		"Grievance",
		filters=filters,
		or_filters=or_filters if or_filters else None,
		fields=[{"COUNT": "*", "as": "total"}],
		limit_page_length=1,
	)
	total_count = int(total_records[0].get("total", 0)) if total_records else 0
	total_pages = math.ceil(total_count / page_size_num) if total_count > 0 else 1

	for item in items:
		item["escalated"] = bool(item.get("escalated"))
		item["is_anonymous"] = bool(item.get("is_anonymous"))
		item["department"] = item.get("assigned_dept")

	audit.record_access(audit.ACTION_VIEW_LIST)

	return success_response(
		data={
			"items": items,
			"pagination": {
				"page": page_num,
				"page_size": page_size_num,
				"total_count": total_count,
				"total_pages": total_pages,
				"has_next": page_num < total_pages,
				"has_prev": page_num > 1,
			},
		},
		message=_("Grievances retrieved successfully"),
	)


@route("/<ticket_number>", methods=("GET",), summary="Get grievance details by ticket number")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def track(ticket_number: str):
	"""Grievance details and status lookup."""
	doc = _load(ticket_number)
	doc.check_permission("read")
	audit.record_access(audit.ACTION_VIEW_DETAIL, grievance=doc.name)

	attachments = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Grievance", "attached_to_name": doc.name},
		fields=["name", "file_name", "file_url", "file_size", "is_private"],
		order_by="creation asc",
	)

	return success_response(
		data={
			"name": doc.name,
			"ticket_number": doc.ticket_number,
			"ticket_number_display": tn.display(doc.ticket_number),
			"status": doc.status,
			"escalated": bool(doc.escalated),
			"submission_channel": doc.submission_channel,
			"submitter_type": doc.submitter_type,
			"submitter": doc.submitter,
			"submitter_name": doc.submitter_name,
			"contact_mobile": doc.contact_mobile,
			"contact_email": doc.contact_email,
			"assisted_by_officer": doc.assisted_by_officer,
			"is_anonymous": bool(doc.is_anonymous),
			"anonymity_status": doc.anonymity_status,
			"administrative_area": doc.administrative_area,
			"administrative_unit": doc.administrative_unit,
			"service_category": doc.service_category,
			"grievance_type": doc.grievance_type,
			"associated_service_provider": doc.associated_service_provider,
			"description": doc.description,
			"desired_outcome": doc.desired_outcome,
			"assigned_dept": doc.assigned_dept,
			"department": doc.assigned_dept,
			"assigned_to": doc.assigned_to,
			"routed_automatically": bool(doc.routed_automatically),
			"sla_days": doc.sla_days,
			"sla_start_at": doc.sla_start_at,
			"sla_due_date": doc.sla_due_date,
			"sla_consumed_percent": sla.consumed_percent(doc),
			"confirmation_deadline": doc.confirmation_deadline,
			"reopen_count": doc.reopen_count,
			"satisfaction_rating": doc.satisfaction_rating,
			"satisfaction_comments": doc.satisfaction_comments,
			"closure_reason": doc.closure_reason,
			"submitted_on": doc.creation,
			"created_at": doc.creation,
			"updated_at": doc.modified,
			"attachments": attachments,
		},
		message=_("Grievance details retrieved successfully"),
	)


@route("/<ticket_number>/confirm", methods=("POST",), summary="Confirm grievance resolution")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def confirm(ticket_number: str, rating: int | str | None = None, comments: str | None = None):
	"""FSD 3.6 / UC-03: the submitter confirms the resolution."""
	doc = _load(ticket_number)
	if doc.status != C.PENDING_SUBMITTER:
		frappe.throw(_("This grievance is not awaiting your confirmation."))

	if rating:
		doc.db_set("satisfaction_rating", int(rating), update_modified=False)
	if comments:
		doc.db_set("satisfaction_comments", comments, update_modified=False)

	lifecycle.confirm_resolution(doc)
	return success_response(
		data={"ticket_number": doc.ticket_number, "status": C.CLOSED},
		message=_("Resolution confirmed successfully"),
	)


@route("/<ticket_number>/reopen", methods=("POST",), summary="Reopen a resolved grievance")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def reopen(ticket_number: str, reason: str):
	"""FSD 3.6: reopen with a mandatory reason."""
	doc = _load(ticket_number)
	lifecycle.reopen(doc, reason)
	return success_response(
		data={"ticket_number": doc.ticket_number, "status": doc.status},
		message=_("Grievance reopened successfully"),
	)


class AssignGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str = Field(..., min_length=1)


class ReassignGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str = Field(..., min_length=1)
	department: str = Field(..., min_length=1)
	reason: str = Field(..., min_length=5)
	officer: str | None = None


@route("/<ticket_number>/assign", methods=("POST",), summary="Assign a grievance by the routing rule")
@frappe.whitelist()
@handle_api_errors
@require_role(STAFF_ROLES)
@validate_request(AssignGrievanceRequest)
def assign(ticket_number: str):
	"""STG-337: initial assignment follows the Category+Department routing rule."""
	doc = _load(ticket_number)
	return success_response(
		data=assignment.initial_assign(doc),
		message=_("Grievance assigned"),
	)


@route("/<ticket_number>/reassign", methods=("POST",), summary="Reassign a grievance to another department")
@frappe.whitelist()
@handle_api_errors
@require_role(STAFF_ROLES)
@validate_request(ReassignGrievanceRequest)
def reassign(ticket_number: str, department: str, reason: str, officer: str | None = None):
	"""STG-337: reassignment uses the same routing rule, with no approval and no SLA reset."""
	doc = _load(ticket_number)
	return success_response(
		data=assignment.reassign(doc, department, reason, officer=officer),
		message=_("Grievance reassigned"),
	)


@route("/<ticket_number>/reject", methods=("POST",), summary="Reject a grievance with a reason")
@frappe.whitelist()
@handle_api_errors
@require_role(STAFF_ROLES)
def reject(ticket_number: str, reason: str):
	"""FSD 3.4: an officer rejects an invalid or out-of-scope grievance.

	The reason is mandatory and the history row is what insists on it; the
	Workflow decides whether Reject is open from where the case is.
	"""
	doc = _load(ticket_number, ptype="write")
	lifecycle.reject(doc, reason)
	return success_response(
		data={"ticket_number": doc.ticket_number, "status": doc.status},
		message=_("Grievance rejected"),
	)


@route("/<ticket_number>/escalate", methods=("POST",), summary="Escalate an SLA-breached grievance")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def escalate(ticket_number: str, reason: str):
	"""FSD 3.7: the submitter escalates once the SLA window has elapsed."""
	doc = _load(ticket_number)
	# Throws when the case is already at the top of the chain, so reaching the response
	# means it actually moved. The old code reported success either way.
	sla.manual_escalate(doc, reason, by_submitter=True)
	doc.reload()
	return success_response(
		data={"ticket_number": doc.ticket_number, "escalated": bool(doc.escalated)},
		message=_("Grievance escalated successfully"),
	)


@route("/<ticket_number>/reply", methods=("POST",), summary="Reply to a More Info Needed request")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def reply(ticket_number: str, body: str):
	"""FSD Appendix C: the submitter answers a More Info Needed request."""
	doc = _load(ticket_number)
	lifecycle.submitter_replies(doc, body)
	return success_response(
		data={"ticket_number": doc.ticket_number, "status": doc.status},
		message=_("Reply submitted successfully"),
	)


@route("/<ticket_number>/timeline", methods=("GET",), summary="Get grievance timeline and thread details")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def timeline(
	ticket_number: str,
	is_internal: bool | str | None = None,
	limit: int | str = 20,
	cursor: str | None = None,
):
	"""Retrieve chronological unified conversation, activity timeline, and thread summary for a grievance.

	Submitters only see public entries (is_internal = 0).
	Staff (Officers, Admins) see all entries or can filter by is_internal flag.
	"""
	doc = _load(ticket_number)
	doc.check_permission("read")
	audit.record_access(audit.ACTION_VIEW_DETAIL, grievance=doc.name)

	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	is_staff = bool(roles & STAFF_ROLES)

	filters = {"grievance": doc.name}

	if not is_staff:
		filters["is_internal"] = 0
	elif is_internal is not None:
		filters["is_internal"] = 1 if str(is_internal).lower() in ("1", "true", "yes") else 0

	if cursor:
		filters["created_on"] = ["<", cursor]

	page_limit = int(limit)
	entries = frappe.get_all(
		"Grievance Timeline",
		filters=filters,
		fields=[
			"name",
			"entry_type",
			"is_internal",
			"body",
			"author_user",
			"author_submitter",
			"ref_doctype",
			"ref_docname",
			"created_on",
		],
		order_by="created_on desc, name desc",
		limit=page_limit + 1,
	)

	has_more = len(entries) > page_limit
	if has_more:
		entries = entries[:page_limit]

	next_cursor = entries[-1]["created_on"].isoformat() if (has_more and entries) else None

	for entry in entries:
		entry["is_internal"] = bool(entry.get("is_internal"))
		if entry["author_submitter"]:
			entry["author_type"] = "submitter"
			entry["author_name"] = doc.submitter_name or entry["author_submitter"]
		elif entry["author_user"]:
			entry["author_type"] = "officer"
			entry["author_name"] = (
				frappe.db.get_value("User", entry["author_user"], "full_name") or entry["author_user"]
			)
		else:
			entry["author_type"] = "system"
			entry["author_name"] = "System"

	return success_response(
		data={
			"ticket_number": doc.ticket_number,
			"status": doc.status,
			"escalated": bool(doc.escalated),
			"summary": {
				"description": doc.description,
				"desired_outcome": doc.desired_outcome,
				"service_category": doc.service_category,
				"grievance_type": doc.grievance_type,
				"administrative_area": doc.administrative_area,
				"administrative_unit": doc.administrative_unit,
				"submission_channel": doc.submission_channel,
			},
			"submitter": {
				"name": doc.submitter_name,
				"mobile": doc.contact_mobile,
				"email": doc.contact_email,
				"submitter_type": doc.submitter_type,
				"is_anonymous": bool(doc.is_anonymous),
				"assisted_by_officer": doc.assisted_by_officer,
			},
			"sla": {
				"sla_days": doc.sla_days,
				"sla_start_at": doc.sla_start_at,
				"sla_due_date": doc.sla_due_date,
				"sla_consumed_percent": sla.consumed_percent(doc),
				"next_escalation_at": doc.next_escalation_at,
				"confirmation_deadline": doc.confirmation_deadline,
			},
			"assignment": {
				"department": doc.assigned_dept,
				"assigned_to": doc.assigned_to,
				"routed_automatically": bool(doc.routed_automatically),
			},
			"timeline": entries,
			"has_more": has_more,
			"next_cursor": next_cursor,
		},
		message=_("Timeline retrieved successfully"),
	)


@route("/<ticket_number>/note", methods=("POST",), summary="Add internal or public note (staff only)")
@frappe.whitelist()
@handle_api_errors
@require_role(STAFF_ROLES)
def add_note(ticket_number: str, body: str, is_internal: bool | str = True):
	"""Staff-only endpoint to add an internal or public note to the case timeline."""
	doc = _load(ticket_number)
	internal = str(is_internal).lower() not in ("0", "false", "no")

	entry = GrievanceTimeline.record(
		grievance=doc.name,
		entry_type="note",
		is_internal=internal,
		body=body,
		author_user=frappe.session.user,
	)

	return success_response(
		data={
			"name": entry.name,
			"entry_type": entry.entry_type,
			"is_internal": bool(entry.is_internal),
			"author_type": "officer",
			"created_on": entry.created_on,
		},
		message=_("Note added successfully"),
	)


@route("/<ticket_number>/message", methods=("POST",), summary="Post a public message to the conversation")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def message(ticket_number: str, body: str):
	"""Post a public message to the case conversation thread."""
	doc = _load(ticket_number)
	user = frappe.session.user
	is_staff = bool(set(frappe.get_roles(user)) & STAFF_ROLES)

	entry = GrievanceTimeline.record(
		grievance=doc.name,
		entry_type="message",
		is_internal=False,
		body=body,
		author_user=user if is_staff else None,
		author_submitter=doc.submitter if not is_staff else None,
	)

	return success_response(
		data={
			"name": entry.name,
			"entry_type": entry.entry_type,
			"is_internal": False,
			"author_type": "officer" if is_staff else "submitter",
			"created_on": entry.created_on,
		},
		message=_("Message posted successfully"),
	)


def _load(ticket_number, ptype="read"):
	"""Fetch a grievance by ticket number as the submitter typed it.

	Normalised first: the number is printed grouped (3-001-002A-0) and read back
	over a phone line, so the hyphens, casing and the O/I/L substitutions the
	alphabet anticipates must not decide whether a farmer can reach their own
	case.
	"""
	normalized = tn.normalize(ticket_number)
	name = frappe.db.get_value("Grievance", {"ticket_number": normalized}, "name")
	if not name:
		if frappe.db.exists("Grievance", normalized):
			name = normalized
		elif frappe.db.exists("Grievance", ticket_number):
			name = ticket_number
		else:
			frappe.throw(_("No grievance found with that ticket number."), title=_("Not Found"))
	doc = frappe.get_doc("Grievance", name)
	doc.check_permission(ptype)
	return doc


def get_status_options() -> list[dict]:
	"""The lifecycle states, with their open/terminal flags."""
	all_statuses = [
		C.SUBMITTED,
		C.ASSIGNED,
		C.IN_PROGRESS,
		C.MORE_INFO_NEEDED,
		C.PENDING_SUBMITTER,
		C.RESOLVED,
		C.CLOSED,
		C.REJECTED,
	]

	return [
		{
			"status": status,
			"label": status,
			"is_open": 1 if status in C.OPEN_STATUSES else 0,
			"is_terminal": 1 if status in C.TERMINAL_STATUSES else 0,
		}
		for status in all_statuses
	]


@route("/options", methods=("GET",), summary="Get grievance options and dropdowns")
@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_GRIEVANCE_ROLES)
def options(service_category: str | None = None):
	"""Management and lookup options for submitters, grievance officers and admins.

	Returns reference lists for case filing, management, triage, and filtering,
	including departments, lifecycle statuses, categories, and types.

	Args:
	    service_category (str, optional): Filter grievance types by a specific service category (e.g. 'Inputs').

	Returns:
	    departments: Active grievance departments
	    statuses: Grievance lifecycle statuses with metadata
	    service_categories: Active service categories
	    grievance_types: Active grievance types (optionally filtered by service_category)
	    submission_channels: Active intake channels
	"""
	departments = frappe.get_all(
		"Grievance Department",
		filters={"active": 1},
		fields=["name as department_id", "dept_name as department_name", "email_account", "head_of_dept"],
		order_by="dept_name asc",
		ignore_permissions=True,
	)

	service_categories = get_service_categories()
	grievance_types = get_grievance_types(service_category=service_category)

	data = {
		"departments": departments,
		"statuses": get_status_options(),
		"service_categories": service_categories,
		"grievance_types": grievance_types,
		"submission_channels": active_channels(),
	}

	return success_response(data=data, message=_("Grievance options fetched successfully"))
