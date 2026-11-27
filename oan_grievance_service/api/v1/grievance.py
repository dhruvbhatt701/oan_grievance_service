"""Grievance submission and submitter actions, exposed for the mobile app, web
portal, IVR and call centre channels.

Every entry point is whitelisted, validates its own input, and routes through the
service layer so the audit trail and notifications cannot be bypassed.
"""

from typing import Literal

import frappe
from frappe import N_, _
from frappe.utils import get_datetime, now_datetime
from oan_auth_service.api.router import prefixed
from oan_auth_service.api.utils import (
	PageParams,
	SafeEmail,
	error_response,
	handle_api_errors,
	page_meta,
	parse_multi_value,
	require_permission,
	require_role,
	success_response,
	to_tz_aware_iso,
	validate_request,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from werkzeug.wrappers import Response

from oan_grievance_service import permissions
from oan_grievance_service.api.v1 import _case_view as case_view
from oan_grievance_service.api.v1._case_view import (
	format_timeline_event as _format_timeline_event,
)
from oan_grievance_service.api.v1._case_view import (
	history_details_for as _history_details_for,
)
from oan_grievance_service.api.v1._case_view import (
	response_numbers_for as _get_response_numbers,
)
from oan_grievance_service.api.v1._options import (
	active_channels,
	case_status,
	currently_escalated,
	expand_status_filter,
	get_department_officers,
	get_departments,
	get_grievance_types,
	get_service_categories,
	get_status_options,
	get_status_summary,
	public_status,
)
from oan_grievance_service.api.v1._schemas import (
	INTERNAL_NOTE_MAX,
	ActionTaken,
	Body,
	DecisionRequest,
	NonBlank,
	PersonName,
	ResolutionSummary,
	ResponseText,
	Text,
	blank_to_none,
)
from oan_grievance_service.grievance_management.doctype.grievance.grievance import (
	GrievanceSubmissionPayload,
)
from oan_grievance_service.grievance_management.doctype.grievance_settings.grievance_settings import (
	reopen_escalation_enabled,
)
from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.services import (
	audit,
	duplicates,
	identity,
	intake,
	lifecycle,
	organization,
	response_body,
	routing,
	sla,
	ticket_export,
	timeline_changes,
)
from oan_grievance_service.services import constants as C

# Aliased: several entry points take a `ticket_number` argument, which would
# otherwise shadow the module inside them.
from oan_grievance_service.services import ticket_number as tn
from oan_grievance_service.services.resolvers import (
	find_department,
	resolve_administrative_area,
	resolve_grievance_type,
)

route = prefixed("/api/v1/grievances")


class SubmitGrievanceRequest(GrievanceSubmissionPayload):
	"""Request body for the one-step submission endpoint."""

	model_config = ConfigDict(extra="allow")

	submitter_type: str | None = None
	submitter_name: PersonName | None = None
	submitter: str | None = None
	fayda_id: str | None = Field(None, title=N_("Fayda ID"))
	contact_mobile: str | None = None
	country_code: str | None = None
	phone_number: str | None = None
	phone: str | None = None
	contact_email: SafeEmail | None = None
	submission_channel: str | None = None
	administrative_area: str | None = None
	administrative_unit: str | None = None
	service_category: str | None = None
	grievance_type: str | None = None
	associated_service_provider: str | None = None
	desired_outcome: Text | None = Field(None, max_length=1000)
	is_anonymous: int | bool = 0
	consent_given: int | bool = 1
	client_submission_uuid: str | None = None
	client_uuid: str | None = None
	confirm_new: bool | int | str = Field(
		0,
		description="Send 1 after being told a similar grievance exists, to file this one anyway",
	)


class ListGrievancesRequest(PageParams):
	model_config = ConfigDict(extra="allow")

	limit: int | None = Field(None, ge=1, le=100)
	status: str | list | None = None
	service_category: str | list | None = None
	category: str | list | None = None
	grievance_type: str | list | None = None
	type: str | list | None = None
	assigned_dept: str | list | None = None
	department: str | list | None = None
	dept: str | list | None = None
	assigned_to: str | None = None
	administrative_area: str | list | None = None
	location: str | list | None = None
	region: str | list | None = None
	zone: str | list | None = None
	woreda: str | list | None = None
	kebele: str | list | None = None
	administrative_unit: str | list | None = None
	submission_channel: str | list | None = None
	channel: str | list | None = None
	escalated: bool | str | int | None = None
	is_escalated: bool | str | int | None = None
	is_anonymous: bool | str | int | None = None
	submitter: str | None = None
	from_date: str | None = None
	to_date: str | None = None
	search: str | None = None
	sort_by: str = "creation"
	sort_order: str = "desc"


class GrievanceActionRequest(Body):
	ticket_number: str | None = None
	action: NonBlank = Field(..., description="Workflow action, one of the case's available_actions")
	reason: ResponseText | None = Field(
		None, description="Shown to the submitter. For a department response, the response itself."
	)
	action_taken: ActionTaken | None = Field(
		None,
		description="Department response, first part: what was done. Sent with `resolution_summary` "
		"instead of `reason`; the two are stored together as the reason.",
	)
	resolution_summary: ResolutionSummary | None = Field(
		None,
		description="Department response, second part: the outcome for the submitter. "
		"Sent with `action_taken` instead of `reason`.",
	)
	note: str | None = Field(
		None, description="Optional detail recorded directly on the Grievance Status History audit row."
	)
	internal_notes: Text | None = Field(
		None,
		max_length=INTERNAL_NOTE_MAX,
		description="Staff only. Posted as a separate internal timeline entry.",
	)
	template: str | None = Field(None, description="Staff only. Response template the reason started from.")
	rating: int | None = Field(None, ge=1, le=5, description="Close Case by the submitter only")

	_blank = field_validator(
		"reason", "action_taken", "resolution_summary", "note", "internal_notes", "template", mode="before"
	)(blank_to_none)

	@model_validator(mode="after")
	def check_response_parts(self):
		if self.action_taken is None and self.resolution_summary is None:
			return self
		if self.action_taken is None or self.resolution_summary is None:
			raise ValueError("action_taken and resolution_summary are sent together")
		if self.reason is not None:
			raise ValueError("Send either reason or action_taken with resolution_summary, not both")
		return self


class GrievanceFeedbackRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	rating: int = Field(..., ge=1, le=5)
	comments: str | None = None
	feedback_type: str | None = "Resolution"
	feedback_channel: str | None = "Web Portal"


class ExportGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: NonBlank
	format: Literal["csv", "pdf"] = Field("pdf", description="File format of the exported form")
	lang: Literal["en", "am"] = Field(
		"en", description="Language of the form's own wording: en (English) or am (Amharic)"
	)

	@field_validator("format", "lang", mode="before")
	@classmethod
	def _lower(cls, value):
		return value.strip().lower() if isinstance(value, str) else value


class PostMessageRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	# Declared before `body`: its limit depends on whether this is an internal note.
	is_internal: bool | str | None = Field(
		None, description="Hide from the submitter (staff only). Defaults to a public message."
	)
	type: str | None = Field(
		None, description="Legacy alias: note, internal or internal_note posts an internal note."
	)
	body: str = Field(..., min_length=1)
	client_message_id: str | None = Field(
		None,
		max_length=64,
		title=N_("Client message ID"),
		description="Client-generated idempotency key (UUIDv4) to prevent duplicate submissions.",
	)

	@field_validator("body")
	@classmethod
	def _internal_note_length(cls, value, info):
		"""An internal note is capped at the frontend's limit; a public message is not."""
		internal = _parse_flag(info.data.get("is_internal"))
		if internal is None:
			internal = info.data.get("type") in ("note", "internal", "internal_note")
		if internal and len(value.strip()) > INTERNAL_NOTE_MAX:
			raise PydanticCustomError(
				"internal_note_too_long",
				"An internal note must not exceed {max_length} characters.",
				{"max_length": INTERNAL_NOTE_MAX},
			)
		return value


class ResponseTemplatesRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	action: str = Field(..., min_length=1, description="Workflow action the templates are written for")
	service_category: str | None = Field(None, description="Filter templates by service category")


class ReassignGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	target_department: str | None = Field(
		None, min_length=1, description="Omit to pick the desk by officer and category alone"
	)
	target_officer: str | None = None
	target_category: str | None = None
	target_grievance_type: str | None = None
	reason: Text | None = Field(None, max_length=1000)

	@model_validator(mode="after")
	def _require_a_target(self):
		targets = (
			self.target_department,
			self.target_officer,
			self.target_category,
			self.target_grievance_type,
		)
		if not any(targets):
			raise ValueError(
				"Name at least one of target_department, target_officer, target_category "
				"or target_grievance_type."
			)
		if self.target_officer and not (self.target_department or self.target_category):
			raise ValueError("Specify target_department or target_category when naming target_officer.")
		return self


class EscalateGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	reason: Text = Field(..., min_length=1, max_length=1000)
	target_officer: str | None = Field(
		None,
		min_length=1,
		description="An officer at the next level up (see /grievances/options with ticket_number). "
		"Omit to hand the case to the senior a missed deadline would reach",
	)


class DeferSLARequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	additional_days: int = Field(..., ge=1)
	reason: Text = Field(..., min_length=20, max_length=1000)


class ConfirmDuplicateRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	main_ticket: str | None = Field(
		None, min_length=1, description="The main ticket. Defaults to the best flagged match"
	)
	note: Text | None = Field(None, max_length=1000)


class DismissDuplicateRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str | None = None
	note: Text | None = Field(None, max_length=1000)


STAFF_ROLES = C.STAFF_ROLES


def _resolve_submitter_identity(kwargs):
	"""Decide who a grievance belongs to, server-side, and snapshot their profile.

	A submitter always files as themselves: the session decides the owning profile,
	never the request. Assisted submitters (Development Agents, call centre, walk-in
	intake staff) and administrators may file on someone's behalf, in which case the
	named submitter stays the owner and the filing user is recorded as the assisting
	officer for the audit trail.

	The name and contact columns are a filing-time copy of the profile, so a case
	keeps showing who reported it even after they later change their phone number.
	Staff taking a walk-in or IVR report from someone with no profile yet supply
	those details directly, because there is nothing to copy from.
	"""
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	is_assisted_filer = bool(roles & C.ASSISTED_FILER_ROLES)

	if is_assisted_filer:
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

	# An organization's profile files only for its verified, authorized representative.
	# Asked before the block flag so a pending organization hears why, not just "blocked".
	organization.assert_may_file(profile_name, user)

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


def _resolve_area_filter_identifiers(identifier: str, level_hint: str | None = None) -> list[str]:
	"""Resolve an area identifier, path_code, code, or area_name to matching canonical doc names."""
	if not identifier:
		return []
	identifier = str(identifier).strip()
	if frappe.db.exists("Grievance Administrative Area", identifier):
		return [identifier]

	by_path = frappe.get_all("Grievance Administrative Area", filters={"path_code": identifier}, pluck="name")
	if by_path:
		return by_path

	by_code = frappe.get_all("Grievance Administrative Area", filters={"code": identifier}, pluck="name")
	if by_code:
		return by_code

	if level_hint:
		by_level = frappe.get_all(
			"Grievance Administrative Area",
			filters={"area_name": identifier, "level_name": level_hint},
			pluck="name",
		)
		if by_level:
			return by_level

	by_region = frappe.get_all(
		"Grievance Administrative Area",
		filters={"area_name": identifier, "level_name": "Region"},
		pluck="name",
	)
	if by_region:
		return by_region

	by_name = frappe.get_all("Grievance Administrative Area", filters={"area_name": identifier}, pluck="name")
	if by_name:
		return by_name

	frappe.throw(
		_("Administrative area '{0}' could not be resolved.").format(identifier),
		frappe.DoesNotExistError,
		title=_("Invalid Area Filter"),
	)


def _resolve_area_filter_identifier(identifier: str, level_hint: str | None = None) -> str:
	"""Resolve an area identifier, path_code, code, or area_name for filtering (single name)."""
	matches = _resolve_area_filter_identifiers(identifier, level_hint)
	return matches[0] if matches else ""


@route(
	"",
	methods=("GET",),
	summary="List grievances with filtering, pagination, and sorting",
	tier="officer-core",
)
@validate_request(ListGrievancesRequest)
@handle_api_errors
@require_permission("Grievance", "read")
def list_grievances(
	page: int = 1,
	page_size: int = 20,
	limit: int | None = None,
	status: str | list | None = None,
	service_category: str | list | None = None,
	category: str | list | None = None,
	grievance_type: str | list | None = None,
	type: str | list | None = None,
	assigned_dept: str | list | None = None,
	department: str | list | None = None,
	dept: str | list | None = None,
	assigned_to: str | None = None,
	administrative_area: str | list | None = None,
	location: str | list | None = None,
	region: str | list | None = None,
	zone: str | list | None = None,
	woreda: str | list | None = None,
	kebele: str | list | None = None,
	administrative_unit: str | list | None = None,
	submission_channel: str | list | None = None,
	channel: str | list | None = None,
	escalated: bool | str | int | None = None,
	is_escalated: bool | str | int | None = None,
	is_anonymous: bool | str | int | None = None,
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
	service_category/category, administrative_area/location/region/zone/woreda/kebele,
	grievance_type, department, and submission_channel.

	Status filters use the queue cards (All, Submitted, Reopened, In Progress, More Info
	Needed, Rejected, Resolved, Closed; officers also get Assigned). Workflow
	stages that are not a card are reported as the card they roll up into.

	Ordered by submission date, most recent first, unless `sort_by` / `sort_order` say otherwise.
	Escalated cases come first whatever the order.
	"""
	effective_limit = limit if limit is not None else page_size
	offset = (page - 1) * effective_limit

	filters = []

	status_raw = next((val for val in (status, kwargs.get("status")) if val is not None), None)
	status_vals = parse_multi_value(status_raw)
	if status_vals:
		expanded = expand_status_filter(status_vals)
		if expanded:
			filters.append(["status", "in", expanded])

	MULTI_SELECT_FIELDS = {
		"service_category": [service_category, category, kwargs.get("category")],
		"grievance_type": [grievance_type, type, kwargs.get("type")],
		"assigned_dept": [assigned_dept, department, dept, kwargs.get("dept"), kwargs.get("department")],
		"submission_channel": [submission_channel, channel, kwargs.get("channel")],
	}
	for fieldname, candidates in MULTI_SELECT_FIELDS.items():
		raw = next((val for val in candidates if val is not None), None)
		vals = parse_multi_value(raw)
		if vals:
			if fieldname == "grievance_type":
				resolved_vals = [resolve_grievance_type(v) or v for v in vals]
				filters.append([fieldname, "in", resolved_vals])
			else:
				filters.append([fieldname, "in", vals])

	if assigned_to:
		target_user = frappe.session.user if assigned_to == "me" else assigned_to
		filters.append(["assigned_to", "=", target_user])

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

	tier_filters = [
		(kebele or kwargs.get("kebele"), "Kebele"),
		(woreda or kwargs.get("woreda"), "Woreda"),
		(zone or kwargs.get("zone"), "Zone"),
		(region or kwargs.get("region"), "Region"),
		(location or kwargs.get("location"), None),
		(administrative_area or kwargs.get("administrative_area"), None),
		(administrative_unit or kwargs.get("administrative_unit"), None),
	]

	active_tier_area_sets = []
	for val, lvl in tier_filters:
		if val is not None:
			raw_items = parse_multi_value(val)
			if not raw_items:
				continue
			tier_area_names = set()
			canonical_names = set()
			for item in raw_items:
				canonical_names.update(_resolve_area_filter_identifiers(item, lvl))

			if canonical_names:
				bounds_rows = frappe.get_all(
					"Grievance Administrative Area",
					filters={"name": ["in", list(canonical_names)]},
					fields=["name", "lft", "rgt", "is_group"],
				)
				range_filters = []
				for b in bounds_rows:
					if b.lft is not None and b.rgt is not None and (b.get("is_group") or (b.rgt - b.lft > 1)):
						range_filters.append([["lft", ">=", int(b.lft)], ["lft", "<=", int(b.rgt)]])
					else:
						tier_area_names.add(b.name)

				for rf in range_filters:
					descendants = frappe.get_all(
						"Grievance Administrative Area",
						filters=rf,
						pluck="name",
					)
					tier_area_names.update(descendants)

			if tier_area_names:
				active_tier_area_sets.append(tier_area_names)

	if active_tier_area_sets:
		matching_areas = active_tier_area_sets[0]
		for s in active_tier_area_sets[1:]:
			matching_areas = matching_areas.intersection(s)

		if not matching_areas:
			# The area filters exclude each other, so nothing can match: skip the query.
			audit.record_access(audit.ACTION_VIEW_LIST)
			return _grievance_page([], page, effective_limit, total_count=0)
		filters.append(["administrative_area", "in", list(matching_areas)])

	or_filters = []
	if search:
		term = search.strip()
		search_pattern = f"%{term}%"
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
		# A ticket is printed grouped and read back over a phone line, but
		# stored flat, so a pasted `B-001-0012-0` matches nothing on the raw
		# pattern alone. Added beside the raw clause rather than replacing it:
		# grievances numbered under the old hyphenated format carry real hyphens in the stored
		# value, and cleaning the term would stop those matching.
		ticket_term = tn.clean(term)
		if ticket_term and ticket_term != term:
			or_filters.append(["ticket_number", "like", f"%{ticket_term}%"])
			or_filters.append(["name", "like", f"%{ticket_term}%"])
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
		"escalated",
	}
	order_field = sort_by if sort_by in allowed_sort_fields else "creation"
	order_direction = "asc" if str(sort_order).lower() == "asc" else "desc"
	# Newest submission first unless the caller asks otherwise (STG-516); escalated cases stay
	# grouped at the top, as before.
	if order_field == "escalated":
		order_by = f"`tabGrievance`.escalated {order_direction}, `tabGrievance`.creation desc"
	else:
		order_by = f"`tabGrievance`.escalated desc, `tabGrievance`.{order_field} {order_direction}"
	order_by += ", `tabGrievance`.name desc"

	fields = [
		"name",
		"ticket_number",
		"status",
		"escalated",
		"duplicate_of",
		"submission_channel",
		"submitter",
		"submitter_name",
		"contact_mobile",
		"contact_email",
		"is_anonymous",
		"administrative_area",
		# Read by `officer_may_act` to place the case on an officer's desk; not returned.
		"area_lft",
		"service_category",
		"grievance_type",
		"description",
		"assigned_dept",
		"assigned_to",
		"sla_due_date",
		"state_deadline",
		"on_hold_since",
		"is_sensitive",
		"handling_class",
		"priority",
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
		limit=effective_limit,
	)

	total_records = frappe.get_list(
		"Grievance",
		filters=filters,
		or_filters=or_filters if or_filters else None,
		fields=[{"COUNT": "*", "as": "total"}],
		order_by=None,
		limit=1,
	)
	total_count = int(total_records[0].get("total", 0)) if total_records else 0

	from oan_grievance_service.services.administrative_area import (
		external_area_ids,
		format_administrative_location,
		get_administrative_hierarchy,
	)

	area_cache = {}
	unique_areas = {item.get("administrative_area") for item in items if item.get("administrative_area")}
	ext_area_map = external_area_ids(unique_areas)
	for area_id in unique_areas:
		h = get_administrative_hierarchy(area_id)
		loc = format_administrative_location(h)
		area_cache[area_id] = {"hierarchy": h, "location": loc}

	unique_types = {item.get("grievance_type") for item in items if item.get("grievance_type")}
	type_names = {}
	if unique_types:
		type_names = dict(
			frappe.get_all(
				"Grievance Type",
				filters={"name": ["in", list(unique_types)]},
				fields=["name", "type_name"],
				as_list=True,
			)
		)

	is_staff = permissions.is_staff(frappe.session.user)
	assignee_names = _officer_names(item.get("assigned_to") for item in items) if is_staff else {}
	timer_cache = {}
	officer_may_act = permissions.officer_write_check() if is_staff else None
	flagged = _flagged_duplicates(items) if is_staff else {}
	for item in items:
		item["was_escalated"] = bool(item.get("escalated"))
		item["escalated"] = currently_escalated(item)
		item["possible_duplicate_of"] = flagged.get(item["name"])
		item["duplicate_of"] = tn.display(item["duplicate_of"]) if item.get("duplicate_of") else None
		deadline = sla.deadline_view(item, timer_cache)
		item["due_date"] = to_tz_aware_iso(deadline["due_date"])
		item["due_kind"] = deadline["due_kind"]
		item["overdue_seconds"] = deadline["overdue_seconds"]
		# Only the side the case is waiting on can act on it: the submitter while a
		# reply or confirmation is owed, an officer who owns the case otherwise.
		if is_staff:
			item["can_act"] = deadline["awaiting"] == "officer" and officer_may_act(item)
		else:
			item["can_act"] = deadline["awaiting"] == "submitter"
		item.pop("area_lft", None)
		held = sla.pause_view(item, timer_cache)
		item.pop("on_hold_since", None)
		item["auto_close_at"] = held["auto_close_at"]
		item["is_paused"] = held["is_paused"]
		item["paused_since"] = to_tz_aware_iso(held["paused_since"])
		if is_staff:
			item["sla_due_date"] = held["sla_due_date"]
		state_deadline = item.pop("state_deadline", None)
		item["confirmation_deadline"] = state_deadline if item.get("status") == C.STATE_RESOLVED else None

		show_id = permissions.can_see_identity(item)
		contact = identity.mask_contact(item, show_identity=show_id)
		item["contact_mobile"] = contact["contact_mobile"]
		item["contact_email"] = contact["contact_email"]
		item["can_request_more_info"] = contact["can_request_more_info"]
		if not show_id:
			item["submitter_name"] = _("Anonymous Submitter")

		item["is_anonymous"] = bool(item.get("is_anonymous"))
		item["status"] = case_status(item)
		sensitive = bool(item.get("is_sensitive"))
		shown = _classification_view(item, is_staff)
		for fieldname in ("is_sensitive", "handling_class", "priority"):
			item.pop(fieldname, None)
		item.update(shown)
		if not is_staff and sensitive:
			# Neither the alias nor the column it aliases: the desk of a sensitive case is not the
			# submitter's to see.
			item["assigned_dept"] = None
		item["department"] = item.get("assigned_dept")
		item["assigned_to_name"] = assignee_names.get(item.get("assigned_to"))
		if not is_staff:
			item["assigned_to"] = None
		# Grouped for reading, as `timeline` returns it. Stored flat in DB,
		# but exposed formatted to clients as ticket_number.
		item["ticket_number"] = tn.display(item.get("ticket_number"))
		raw_area = item.get("administrative_area")
		area_info = area_cache.get(raw_area)
		item["administrative_area"] = ext_area_map.get(raw_area, raw_area)
		item["location"] = area_info["location"] if area_info else None
		item["administrative_hierarchy"] = area_info["hierarchy"] if area_info else None

		raw_type = item.get("grievance_type")
		resolved_type_name = type_names.get(raw_type, raw_type)
		item["grievance_type_id"] = raw_type
		item["grievance_type_name"] = resolved_type_name
		# Keep grievance_type as the ID for compatibility with filters
		item["grievance_type"] = raw_type

	audit.record_access(audit.ACTION_VIEW_LIST)

	return _grievance_page(items, page, effective_limit, total_count)


def _flagged_duplicates(items):
	"""{name: ticket of its best flagged match} for the listed cases still awaiting a ruling."""
	names = [item["name"] for item in items if item.get("status") != C.STATE_DUPLICATE]
	if not names:
		return {}
	rows = frappe.get_all(
		"Grievance Duplicate",
		filters={"grievance": ["in", names], "is_confirmed": 0, "is_dismissed": 0},
		fields=["grievance", "duplicate_of"],
		order_by="similarity_score asc, name desc",
	)
	# Worst first, so the best match is the one left standing.
	return {row.grievance: tn.display(row.duplicate_of) for row in rows}


def _grievance_page(items, page, page_size, total_count):
	"""The list endpoint's envelope, shared by the normal and the short-circuit path."""
	meta = page_meta(total_count, page, page_size)
	return success_response(
		data={
			"items": items,
			"pagination": meta,
		},
		pagination=meta,
		message=_("Grievances retrieved successfully"),
	)


# Enforced in `action`, not in the request schema: whether the action applies is decided
# against the loaded case (`lifecycle.actions_available`), and clients read its message. The
# comment a rejection needs on the decide endpoints depends on the request alone, so
# `DecisionRequest` checks it and reports it per field.
ACTIONS_REQUIRING_REASON = {
	"Close Case",
	"In Progress",
	"Refer Onward",
	"Reject",
	"Reopen",
	"Request More Info",
	"Resolve",
	"Submitter Reply",
}

ACTIONS_REQUIRING_RATING = {"Close Case"}


def _get_available_actions_for_user(doc):
	"""List actions available to the current user on this grievance with localized labels."""
	if not permissions.has_grievance_permission(doc, "write"):
		return []

	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	actions = lifecycle.actions_available(doc)
	result = []
	for act in actions:
		if act == "Reject" and not is_staff:
			continue
		if act in ("Assign", duplicates.ACTION_CONFIRM):
			continue
		result.append(
			{
				"action": act,
				"action_code": frappe.scrub(act),
				"label": _(act),
				"requires_reason": act in ACTIONS_REQUIRING_REASON,
				"requires_rating": act in ACTIONS_REQUIRING_RATING,
			}
		)

	return result


def _officer_names(users):
	"""{user id: full name} for the officers a response names; the id when a user has no name."""
	users = {u for u in users if u}
	if not users:
		return {}
	return {
		row.name: row.full_name or row.name
		for row in frappe.get_all("User", filters={"name": ["in", list(users)]}, fields=["name", "full_name"])
	}


def _assignee_name(doc):
	"""The assigned officer's name, for staff; None for anyone else or an unassigned case."""
	if not (doc.assigned_to and permissions.is_staff(frappe.session.user)):
		return None
	return _officer_names([doc.assigned_to]).get(doc.assigned_to)


def _assignee_level(doc) -> dict:
	"""{level, level_name}: the rung the assigned officer holds on the case (`sla.rung_on_case`)."""
	level = sla.rung_on_case(doc.assigned_to, doc) if doc.assigned_to else None
	return {
		"level": level,
		"level_name": frappe.db.get_value("Grievance Role Level", level, "level_name") if level else None,
	}


def _possible_duplicate(doc):
	"""What an officer needs to rule on a flag: the best match and how it compares.

	None for a submitter, for a case nobody flagged and for one already ruled on. The main
	ticket's own details are left out when the officer cannot read it.
	"""
	if doc.workflow_state == C.STATE_DUPLICATE or not permissions.is_staff(frappe.session.user):
		return None
	pending = duplicates.pending_reviews(doc.name)
	if not pending:
		return None
	best = pending[0]
	view = {
		"ticket_number": tn.display(best.duplicate_of),
		"match_level": best.match_level,
		"can_review": permissions.has_grievance_permission(doc, "write"),
	}
	main = frappe.get_doc("Grievance", best.duplicate_of)
	if permissions.has_grievance_permission(main, "read"):
		view.update(
			{
				"status": case_status(main),
				"service_category": main.service_category,
				"grievance_type": main.grievance_type,
				"description": main.description,
				"same_submitter": bool(doc.submitter) and main.submitter == doc.submitter,
				"same_category": main.service_category == doc.service_category,
				"same_type": main.grievance_type == doc.grievance_type,
			}
		)
	return view


def _classification_view(source, is_staff) -> dict:
	"""The sensitive, handling class and priority an officer sees on a case, from the case itself.

	Never worked out here: `services/classification.py` wrote them at filing. A submitter is
	shown none of them: that a case is classed sensitive is for the people who work it.
	"""
	if not is_staff:
		return {}
	return {
		"is_sensitive": bool(source.get("is_sensitive")),
		"handling_class": source.get("handling_class") or C.HANDLING_STANDARD,
		# Urgent, or null: priority has no other value.
		"priority": C.PRIORITY_URGENT if source.get("priority") == C.PRIORITY_URGENT else None,
	}


def _current_state(doc, extra=None, as_submitter=False):
	"""The `current_state` block every mutating endpoint echoes back.

	One definition, so a field added for one endpoint's client is there for all of
	them and the shape cannot drift between them. `extra` carries the fields only
	one endpoint has a reason to report. `as_submitter` gives the submitter's view whoever
	the caller is: for a sensitive case filed on someone's behalf, which the filer must not
	learn more about than the submitter would.
	"""
	user = frappe.session.user
	is_staff = permissions.is_staff(user) and not as_submitter
	state = {
		"status": case_status(doc),
		"escalated": currently_escalated(doc),
		"was_escalated": bool(doc.escalated),
		"assigned_to": doc.assigned_to if is_staff else None,
		"assigned_to_name": _assignee_name(doc) if is_staff else None,
		"department": doc.assigned_dept if is_staff or not doc.is_sensitive else None,
		**_classification_view(doc, is_staff),
		"updated_at": to_tz_aware_iso(doc.modified),
		"available_actions": _get_available_actions_for_user(doc),
		"can_request_more_info": identity.is_reachable(doc),
		"duplicate_of": tn.display(doc.duplicate_of) if doc.duplicate_of else None,
		"possible_duplicate": _possible_duplicate(doc) if is_staff else None,
	}
	if extra:
		state.update(extra)
	return state


@route(
	"",
	methods=("POST",),
	summary="Submit a new grievance",
	tier="citizen-intake",
	request_types=("application/json", "multipart/form-data"),
)
@validate_request(SubmitGrievanceRequest)
@handle_api_errors
@require_permission("Grievance", "create")
def submit(**kwargs):
	"""File a grievance: one insert, straight into Submitted. Files sent as multipart
	attach to the submission entry."""
	from oan_auth_service.api.utils import assemble_phone_number

	from oan_grievance_service.api.v1.attachment import prepare_uploads, store_uploads
	from oan_grievance_service.grievance_management.doctype.grievance.grievance import (
		validate_submission_payload,
	)
	from oan_grievance_service.grievance_masters.doctype.grievance_service_category.grievance_service_category import (
		get_default_category,
	)
	from oan_grievance_service.grievance_masters.doctype.grievance_type.grievance_type import (
		get_fallback_type,
	)

	client_submission_uuid = (
		kwargs.get("client_submission_uuid") or kwargs.get("client_uuid") or frappe.generate_hash(length=32)
	)
	existing = frappe.db.get_value("Grievance", {"client_submission_uuid": client_submission_uuid}, "name")
	if existing:
		frappe.throw(
			_("This submission has already been filed as {0}.").format(tn.display(existing)),
			title=_("Already Submitted"),
		)

	if not kwargs.get("consent_given", 1):
		frappe.throw(
			_("The submitter must consent to the processing of their personal data."),
			title=_("Consent Required"),
		)

	doc = frappe.new_doc("Grievance")
	doc.client_submission_uuid = client_submission_uuid
	doc.consent_given = 1
	doc.consent_recorded_at = now_datetime()
	doc.is_anonymous = 1 if kwargs.get("is_anonymous") else 0
	doc.submission_channel = kwargs.get("submission_channel") or "Web Portal"
	for fieldname in ("administrative_unit", "associated_service_provider", "description", "desired_outcome"):
		doc.set(fieldname, kwargs.get(fieldname))

	area = kwargs.get("administrative_area")
	if area:
		doc.administrative_area = resolve_administrative_area(area) or area

	doc.service_category = kwargs.get("service_category") or get_default_category()
	grievance_type = kwargs.get("grievance_type")
	if grievance_type:
		doc.grievance_type = resolve_grievance_type(grievance_type, doc.service_category)
		if not doc.grievance_type:
			frappe.throw(
				_("Grievance type '{0}' does not exist.").format(grievance_type),
				frappe.ValidationError,
				title=_("Invalid Grievance Type"),
			)
	elif doc.service_category == get_default_category():
		doc.grievance_type = get_fallback_type(doc.service_category)

	contact_mobile = kwargs.get("contact_mobile") or kwargs.get("phone_number") or kwargs.get("phone")
	if contact_mobile:
		contact_mobile = assemble_phone_number(contact_mobile, country_code=kwargs.get("country_code"))
	doc.submitter_type = kwargs.get("submitter_type")
	doc.submitter_name = kwargs.get("submitter_name")
	doc.contact_mobile = contact_mobile
	doc.contact_email = kwargs.get("contact_email")

	profile_fields = ("submitter_type", "submitter_name", "contact_mobile", "contact_email")
	ident = _resolve_submitter_identity({"submitter": kwargs.get("submitter")})
	if not ident.get("submitter") and ident.get("assisted_by_officer") and kwargs.get("fayda_id"):
		linked = identity.find_or_create_submitter({**doc.as_dict(), "fayda_id": kwargs.get("fayda_id")})
		ident = _resolve_submitter_identity({"submitter": linked})
	if ident.get("submitter"):
		doc.submitter = ident["submitter"]
		for fieldname in profile_fields:
			doc.set(fieldname, ident.get(fieldname) or doc.get(fieldname))
	doc.assisted_by_officer = ident.get("assisted_by_officer")
	doc.submitter_type = doc.submitter_type or "Individual Farmer"

	if not kwargs.get("can_request_more_info", True):
		doc.contact_mobile = None
		doc.contact_email = None
	elif doc.contact_mobile:
		doc.contact_mobile = identity.validate_mobile(doc.contact_mobile)

	validate_submission_payload(doc.as_dict())
	repeats = _similar_recent_grievances(doc, confirmed=_parse_flag(kwargs.get("confirm_new")))
	if repeats:
		frappe.response["http_status_code"] = 409
		return error_response(
			message=_("You already have a similar grievance. Is this a new one?"),
			code="POSSIBLE_DUPLICATE",
			details={"possible_duplicates": repeats},
		)
	# Checked before the insert, so a bad file refuses the filing instead of leaving a
	# case without its evidence.
	prepared = prepare_uploads(None)
	doc.insert(ignore_permissions=True)

	submission = intake.record_submission(doc)
	attachments = store_uploads(doc, prepared, timeline_entry=submission.name) if prepared else []
	intake.dispatch_filed(doc)

	# A sensitive case gives its submitter the PIN to check it with, and no hint of the desk. Filed on
	# someone's behalf, the filer gets neither the PIN (the submitter has it by SMS) nor the staff
	# view of the case: the permission rules bar them from it once it is filed.
	from oan_grievance_service.services import classification

	on_behalf = bool(doc.is_sensitive and doc.assisted_by_officer)
	if not doc.is_sensitive:
		sensitive_receipt = {"routing_rule": getattr(doc, "routing_rule", None)}
	elif on_behalf:
		sensitive_receipt = {"routing_rule": None}
	else:
		sensitive_receipt = {"status_pin": classification.pin_of(doc), "routing_rule": None}
		# TEMPORARY (STG-518), while SMS and email are not delivering: the link and PIN are shown
		# once to the submitter who filed for themselves. Off unless `guest_access_show_on_submit`.
		from oan_grievance_service.services import guest_access

		shown_link = guest_access.link_for_display(doc)
		if shown_link:
			sensitive_receipt["guest_access"] = shown_link
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": case_status(doc),
			"workflow_state": doc.workflow_state,
			"client_submission_uuid": doc.client_submission_uuid,
			**sensitive_receipt,
			"can_request_more_info": bool(doc.contact_mobile or doc.contact_email),
			"attachments": attachments,
			"current_state": _current_state(doc, as_submitter=on_behalf),
			"timeline_event": _timeline_event_with(submission, doc, attachments),
		},
		message=_("Grievance submitted successfully"),
	)


def _similar_recent_grievances(doc, confirmed):
	"""The farmer's similar grievances of the last day, when they should be asked first.

	Asked only when Grievance Settings turns the question on, on an interactive channel, and
	only once: a farmer who has said this one is new is not asked again. Other channels cannot
	answer, so they file and are flagged instead.
	"""
	if (
		confirmed
		or doc.submission_channel not in duplicates.INTERACTIVE_CHANNELS
		or not duplicates.prompt_enabled()
	):
		return []
	return [
		{
			"ticket_number": tn.display(row["name"]),
			"status": public_status(row["status"]),
			"service_category": row["service_category"],
			"grievance_type": row["grievance_type"],
			"description": (row["description"] or "")[:200],
			"submitted_on": to_tz_aware_iso(row["creation"]),
			"match_level": row["match_level"],
		}
		for row in duplicates.likely_repeats(doc)
	]


def _actions_for(history_names):
	"""The workflow action each status history row was taken under, keyed by row name."""
	if not history_names:
		return {}
	return dict(
		frappe.get_all(
			"Grievance Status History",
			filters={"name": ["in", list(history_names)], "action": ["is", "set"]},
			fields=["name", "action"],
			as_list=True,
		)
	)


def _timeline_event_with(entry, doc, attachments):
	"""A write's new timeline entry exactly as GET /timeline lists it, files included,
	so the client can append it without refetching. Statuses come from the linked
	history row, as they do there, never from the caller."""
	event = _format_timeline_event(entry, doc)
	if event is not None:
		event["attachments"] = attachments
	return event


# The timeline entry an action writes, where it is not a plain status change.
_ACTION_ENTRY_TYPES = {
	"Close Case": "resolution",
	"Reject": "rejection",
	"Request More Info": "info_request",
	"Submitter Reply": "info_response",
	"Resolve": "response",
	"Refer Onward": "response",
}


@route(
	"/<ticket_number>/action",
	methods=("POST",),
	summary="Execute a workflow action on a grievance",
	tier="citizen-intake",
	request_types=("application/json", "multipart/form-data"),
)
@validate_request(GrievanceActionRequest)
@handle_api_errors
@require_permission("Grievance", "write")
def action(
	ticket_number: str,
	action: str,
	reason: str | None = None,
	action_taken: str | None = None,
	resolution_summary: str | None = None,
	note: str | None = None,
	internal_notes: str | None = None,
	template: str | None = None,
	rating: int | None = None,
	**kwargs,
):
	"""Take a workflow action on a grievance.

	Every action carries a `reason`, which the submitter sees. A department response
	may instead come as `action_taken` and `resolution_summary`, stored together as
	the reason. Which actions the caller may take from the case's state is the
	Grievance Workflow's call. Files sent as multipart attach to the action's
	timeline entry. A Reopen escalates the case to the officer one level above
	whoever resolved it last.
	"""
	if action_taken is not None or resolution_summary is not None:
		if not (action_taken and action_taken.strip() and resolution_summary and resolution_summary.strip()):
			frappe.throw(
				_(
					"Both 'action_taken' and 'resolution_summary' are required when submitting a two-part resolution."
				),
				frappe.ValidationError,
				title=_("Resolution Details Required"),
			)
		reason = response_body.compose(action_taken, resolution_summary)
	doc = _load(ticket_number, ptype="write")
	user = frappe.session.user
	is_staff = permissions.is_staff(user)

	matching_action = next((a for a in lifecycle.actions_available(doc) if a.lower() == action.lower()), None)
	if not matching_action:
		frappe.throw(
			_("Action '{0}' is not available for this grievance in status '{1}'.").format(action, doc.status),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)
	if not is_staff and (internal_notes or template):
		frappe.throw(
			_("Only staff members can add internal notes or use a response template."),
			frappe.PermissionError,
		)

	if matching_action in ACTIONS_REQUIRING_REASON and (not reason or not reason.strip()):
		frappe.throw(
			_("A reason is required for action '{0}'.").format(matching_action),
			frappe.ValidationError,
			title=_("Reason Required"),
		)

	# Files are checked before the move so a bad one refuses the whole request.
	from oan_grievance_service.api.v1.attachment import prepare_uploads, store_uploads

	prepared = prepare_uploads(doc.name)

	if matching_action == duplicates.ACTION_CONFIRM:
		frappe.throw(
			_("Use the duplicate endpoints to confirm a duplicate; they name the main ticket."),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)

	if matching_action == "Assign":
		frappe.throw(
			_(
				"Direct 'Assign' action is not permitted on this endpoint. Use the assignment/reassignment API to assign an officer."
			),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)

	if matching_action == "In Progress" or (
		doc.workflow_state in (C.STATE_ASSIGNED, C.STATE_REOPENED)
		and matching_action in ("Request More Info", "Resolve")
	):
		_ensure_owner(doc)

	# Submitter closing the case accepts and confirms the resolution.
	confirmed = matching_action == "Close Case"
	# A reopen that escalates tells only the officer it lands on (`sla.escalate_on_reopen`), so the
	# move itself sends nothing; with the setting off, the move notifies as it always has.
	escalating_reopen = matching_action == "Reopen" and reopen_escalation_enabled()
	history = lifecycle.transition(
		doc, matching_action, reason=reason, note=note, confirmed=confirmed, notify=not escalating_reopen
	)
	if matching_action == "Close Case":
		doc.db_set("closure_reason", reason, update_modified=False)
	elif matching_action == "Reopen":
		doc.db_set("reopen_count", (doc.reopen_count or 0) + 1, update_modified=False)
		if escalating_reopen:
			sla.escalate_on_reopen(doc)
	elif matching_action == "Submitter Reply":
		from oan_grievance_service.services import notifications

		notifications.queue(doc, C.EVENT_SUBMITTER_RESPONDED)

	entry_type_override = _ACTION_ENTRY_TYPES.get(matching_action, "status_change")

	timeline_entry = GrievanceTimeline.record(
		grievance=doc.name,
		entry_type=entry_type_override,
		is_internal=False,
		body=reason or _("Action: {0}").format(matching_action),
		author_user=user,
		author_type="officer" if is_staff else "submitter",
		ref_doctype="Grievance Status History",
		ref_docname=history.name if history else None,
	)
	if internal_notes:
		GrievanceTimeline.record(
			grievance=doc.name,
			entry_type="note",
			is_internal=True,
			body=internal_notes.strip(),
			author_user=user,
			author_type="officer",
		)
	if template and frappe.db.exists("Grievance Response Template", template):
		from oan_grievance_service.grievance_masters.doctype.grievance_response_template.grievance_response_template import (
			record_use,
		)

		record_use(template)
	# Ratings normally arrive later through /feedback. One the submitter sends with the
	# close is recorded the same way; the reason is already on the close entry.
	if rating is not None and confirmed:
		frappe.get_doc(
			{
				"doctype": "Grievance Feedback",
				"grievance": doc.name,
				"rating": int(rating),
				"feedback_type": "Resolution",
				"submitted_by": user,
				"author_submitter": doc.submitter,
				"submitted_at": now_datetime(),
			}
		).insert(ignore_permissions=True)

	attachments = store_uploads(doc, prepared, timeline_entry=timeline_entry.name) if prepared else []

	doc.reload()
	frappe.publish_realtime(
		"timeline_updated",
		{"grievance": doc.name, "ticket_number": doc.ticket_number},
		room=f"doc:Grievance:{doc.name}",
		after_commit=True,
	)
	current_state = _current_state(doc)
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": case_status(doc),
			"action": matching_action,
			"attachments": attachments,
			"current_state": current_state,
			"timeline_event": _timeline_event_with(timeline_entry, doc, attachments),
			"available_actions": current_state["available_actions"],
		},
		message=_("Grievance updated successfully"),
	)


def _ensure_owner(doc):
	"""Work starts with an owner: an officer or a department. A case without a department
	takes the officer's first desk department, when that desk has one."""
	if doc.assigned_dept:
		return
	if doc.assigned_to:
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			active_scopes,
		)

		scopes = active_scopes(doc.assigned_to)
		if scopes and scopes[0].get("department_scope"):
			doc.db_set("assigned_dept", scopes[0].get("department_scope"), update_modified=False)
	if not (doc.assigned_dept or doc.assigned_to):
		frappe.throw(
			_("Cannot start work on a grievance without an assigned officer."),
			frappe.ValidationError,
			title=_("Officer Required"),
		)


@route(
	"/<ticket_number>/feedback",
	methods=("POST",),
	summary="Submit citizen feedback / satisfaction rating",
	tier="citizen-intake",
)
@validate_request(GrievanceFeedbackRequest)
@handle_api_errors
@require_permission("Grievance", "write")
def feedback(
	ticket_number: str,
	rating: int,
	comments: str | None = None,
	feedback_type: str = "Resolution",
	feedback_channel: str = "Web Portal",
	**kwargs,
):
	"""Submit satisfaction rating and feedback for a resolved or closed grievance."""
	doc = _load(ticket_number, ptype="read")
	user = frappe.session.user

	submitter_profile = None
	if not permissions.is_staff(user):
		from oan_grievance_service.grievance_management.doctype.grievance_submitter_profile.grievance_submitter_profile import (
			profiles_of,
		)

		user_profiles = profiles_of(user)
		if doc.submitter in user_profiles:
			submitter_profile = doc.submitter
		elif user_profiles:
			submitter_profile = user_profiles[0]

	ip = getattr(frappe.local, "request_ip", None)

	fb_doc = frappe.get_doc(
		{
			"doctype": "Grievance Feedback",
			"grievance": doc.name,
			"rating": int(rating),
			"feedback_type": feedback_type,
			"feedback_channel": feedback_channel,
			"comments": comments.strip() if comments else None,
			"submitted_by": user,
			"author_submitter": submitter_profile,
			"submitted_at": now_datetime(),
			"ip_address": ip,
		}
	).insert(ignore_permissions=True)

	return success_response(
		data={
			"ticket_number": doc.ticket_number or doc.name,
			"feedback_id": fb_doc.name,
			"rating": fb_doc.rating,
			"comments": fb_doc.comments,
			"submitted_at": to_tz_aware_iso(fb_doc.submitted_at),
			"current_state": _current_state(doc),
		},
		message=_("Feedback submitted successfully"),
	)


@route(
	"/<ticket_number>/timeline",
	methods=("GET",),
	summary="Get grievance timeline and thread details",
	tier="citizen-intake",
)
@handle_api_errors
@require_permission("Grievance", "read")
def timeline(
	ticket_number: str,
	is_internal: bool | str | None = None,
	limit: int | str = 50,
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
	is_staff = permissions.is_staff(user)

	page_limit = max(1, min(int(limit), 100))
	# Entries, files and the summary are the submitter-safe parts shared with the link-and-PIN view
	# (_case_view.py); everything officer-only below is added here, behind `is_staff`.
	entries, has_more, next_cursor = case_view.timeline_entries(
		doc,
		include_internal=is_staff,
		internal=is_internal,
		limit=page_limit,
		cursor=cursor,
		officer_names=is_staff,
	)

	show_identity = permissions.can_see_identity(doc)
	contact = identity.mask_contact(doc, show_identity=show_identity)
	masked_name = doc.submitter_name if show_identity else _("Anonymous Submitter")

	# Only scanned evidence is listed. Files attached straight to the case used
	# to be shown beside these rows without a verdict; the
	# link_attachments_to_files patch turned them into attachment rows.
	attachments = case_view.case_attachments(doc, include_internal=is_staff)
	files_by_entry = case_view.attachments_by_entry(attachments)
	for entry in entries:
		entry["attachments"] = files_by_entry.get(entry["name"], [])

	from oan_grievance_service.api.v1.change_request import get_pending_request
	from oan_grievance_service.api.v1.change_request import serialize as serialize_cr
	from oan_grievance_service.api.v1.change_request import serialize_public as serialize_cr_public

	deferral_req_name = get_pending_request(doc.name, ["sla_due_date"])
	reassign_req_name = get_pending_request(doc.name, ["assigned_dept", "assigned_to", "service_category"])

	active_deferral_request = None
	active_reassignment_request = None

	if deferral_req_name:
		req_doc = frappe.get_doc("Grievance Change Request", deferral_req_name)
		if is_staff:
			active_deferral_request = serialize_cr(req_doc)
		else:
			active_deferral_request = serialize_cr_public(req_doc)

	if reassign_req_name and is_staff:
		req_doc = frappe.get_doc("Grievance Change Request", reassign_req_name)
		active_reassignment_request = serialize_cr(req_doc)

	from oan_grievance_service.services.administrative_area import (
		external_area_id,
		get_administrative_hierarchy,
	)

	area_hierarchy = get_administrative_hierarchy(doc.administrative_area)
	summary = case_view.case_summary(doc, area_hierarchy)

	held = sla.pause_view(doc)

	return success_response(
		data={
			"name": doc.name,
			"ticket_number": summary["ticket_number"],
			"status": summary["status"],
			# Still in escalation; `was_escalated` keeps that it ever was.
			"escalated": currently_escalated(doc),
			"was_escalated": bool(doc.escalated),
			"description": summary["description"],
			"desired_outcome": summary["desired_outcome"],
			"service_category": summary["service_category"],
			"grievance_type": doc.grievance_type,
			"grievance_type_name": summary["grievance_type_name"],
			"administrative_area": external_area_id(doc.administrative_area),
			"administrative_hierarchy": area_hierarchy,
			"location": summary["location"],
			"administrative_unit": doc.administrative_unit,
			"submission_channel": doc.submission_channel,
			"submitter": {
				"name": masked_name,
				"contact_mobile": contact["contact_mobile"],
				"contact_email": contact["contact_email"],
				"submitter_type": doc.submitter_type,
				"is_anonymous": bool(doc.is_anonymous),
				"assisted_by_officer": doc.assisted_by_officer,
			},
			"sla": {
				"sla_days": doc.sla_days,
				"sla_start_at": to_tz_aware_iso(doc.sla_start_at),
				# Staff see the deadline as the pause holds it; the submitter, as the case states it.
				"sla_due_date": to_tz_aware_iso(held["sla_due_date"])
				if is_staff
				else summary["sla"]["sla_due_date"],
				"is_paused": held["is_paused"],
				"paused_since": to_tz_aware_iso(held["paused_since"]),
				"auto_close_at": to_tz_aware_iso(held["auto_close_at"]),
				"sla_consumed_percent": sla.consumed_percent(doc),
				"next_escalation_at": to_tz_aware_iso(doc.next_escalation_at),
				"confirmation_deadline": summary["sla"]["confirmation_deadline"],
				"active_deferral_request": active_deferral_request,
			},
			**_classification_view(doc, is_staff),
			"assignment": {
				"department": doc.assigned_dept if is_staff or not doc.is_sensitive else None,
				"assigned_to": doc.assigned_to if is_staff else None,
				"assigned_to_name": _assignee_name(doc),
				# The rung the case sits on: its assignee's on a desk covering it. Staff only.
				**(_assignee_level(doc) if is_staff else {"level": None, "level_name": None}),
				"routed_automatically": bool(doc.routed_automatically),
				"active_reassignment_request": active_reassignment_request if is_staff else None,
			},
			"available_actions": _get_available_actions_for_user(doc),
			"can_request_more_info": contact["can_request_more_info"],
			# Opening a case is this call, so it carries the duplicate flag an officer rules on
			# (staff only) and, once confirmed, the main ticket, as `current_state` does.
			"duplicate_of": tn.display(doc.duplicate_of) if doc.duplicate_of else None,
			"possible_duplicate": _possible_duplicate(doc),
			"attachments": attachments,
			"timeline": entries,
			"has_more": has_more,
			"next_cursor": next_cursor,
		},
		message=_("Timeline retrieved successfully"),
	)


@route(
	"/<ticket_number>/export",
	methods=("GET",),
	summary="Export a grievance and its timeline as the SOW Annexure form (CSV or PDF)",
	tier="citizen-intake",
	response_types=("application/pdf", "text/csv"),
)
@handle_api_errors
@require_permission("Grievance", "export")
@validate_request(ExportGrievanceRequest)
def export_form(ticket_number: str, format: str = "pdf", lang: str = "en", **kwargs):
	"""Download one grievance, with its timeline, laid out as the SOW Annexure complaint form.

	`format` is `pdf` (default) or `csv`. `lang` is `en` (default) or `am`: the form's own
	wording, such as its headings and labels, in that language, while what people wrote on the
	case stays as written. The Grievance `export` permission is checked first, by the route
	guard and before the query is validated, as the timeline route checks `read`. Then the case
	check, and the file holds only what the caller may already read on the case: staff get
	internal notes and officer names, a submitter the public record, and an anonymous
	submitter's identity stays sealed. A case the caller may not read is answered as 404, like
	one that does not exist. Every attempt that passes the permission, allowed, refused or
	failed, is written to the access audit trail, and a file is returned only once its row
	(with the file's size and SHA-256) is on record.

	The body is the file itself, not the JSON envelope; errors use the envelope.
	"""
	exported = ticket_export.export_ticket(ticket_number, format, lang)

	response = Response(exported.content, status=200, mimetype=exported.mimetype)
	response.headers.add("Content-Disposition", "attachment", filename=exported.filename)
	response.headers["Content-Length"] = str(len(exported.content))
	response.headers["Cache-Control"] = "private, no-store"
	response.headers["X-Content-Type-Options"] = "nosniff"

	# Tells handle_api_errors to hand the Response back untouched instead of wrapping it in the
	# JSON envelope (as the attachment view does).
	frappe.response["type"] = "download"
	return response


@route(
	"/<ticket_number>/message",
	methods=("POST",),
	summary="Post a message or an internal note, with optional files",
	tier="citizen-intake",
	request_types=("application/json", "multipart/form-data"),
)
@validate_request(PostMessageRequest)
@handle_api_errors
@require_permission("Grievance", "write")
def message(
	ticket_number: str,
	body: str,
	is_internal: bool | str | None = None,
	client_message_id: str | None = None,
	**kwargs,
):
	"""Post to the case thread: a public message, or an internal note when staff send
	`is_internal`. Files sent as multipart attach to the new entry.

	Never moves the case. A reply to an information request is the `Submitter Reply`
	action and an information request is the `Request More Info` action, both on /action.
	"""
	from oan_grievance_service.api.v1.attachment import prepare_uploads, store_uploads

	internal = bool(_parse_flag(is_internal))
	if is_internal is None and kwargs.get("type") in ("note", "internal", "internal_note"):
		internal = True
	doc = _load(ticket_number, ptype="write")
	duplicates.assert_not_duplicate(doc)
	user = frappe.session.user
	is_staff = permissions.is_staff(user)
	if internal and not is_staff:
		frappe.throw(_("Only staff members can post internal notes."), frappe.PermissionError)

	client_msg_id = client_message_id or kwargs.get("client_message_id")
	prepared = prepare_uploads(doc.name)
	entry = GrievanceTimeline.record(
		grievance=doc.name,
		entry_type="note" if internal else "message",
		is_internal=internal,
		body=body,
		author_user=user,
		author_type="officer" if is_staff else "submitter",
		client_message_id=client_msg_id,
	)
	attachments = store_uploads(doc, prepared, timeline_entry=entry.name) if prepared else []

	frappe.publish_realtime(
		"timeline_updated",
		{"grievance": doc.name, "ticket_number": doc.ticket_number},
		room=f"doc:Grievance:{doc.name}",
		after_commit=True,
	)

	return success_response(
		data={
			"name": entry.name,
			"ticket_number": tn.display(doc.ticket_number),
			"entry_type": entry.entry_type,
			"is_internal": bool(entry.is_internal),
			"author_type": "officer" if is_staff else "submitter",
			"created_on": to_tz_aware_iso(entry.created_on),
			"status": case_status(doc),
			"attachments": attachments,
			"current_state": _current_state(doc),
			"timeline_event": _timeline_event_with(entry, doc, attachments),
		},
		message=_("Note added successfully") if internal else _("Message posted successfully"),
	)


def add_note(ticket_number: str, body: str, **kwargs):
	"""Convenience wrapper for posting an internal note to a grievance thread."""
	return message(ticket_number=ticket_number, body=body, is_internal=True, **kwargs)


def _parse_flag(value):
	"""A boolean from a JSON body or a form field. The string "false" is truthy in
	Python, so a form-encoded flag has to be read, not tested."""
	if value is None:
		return None
	if isinstance(value, str):
		return value.strip().lower() not in ("", "0", "false", "no", "off")
	return bool(value)


def _change_response(doc, req, applied_message, pending_message, **extra):
	"""The answer every change-request-backed endpoint gives: the case as it now
	stands, and the request that holds the change."""
	from oan_grievance_service.api.v1.change_request import serialize

	doc.reload()
	current_state = _current_state(doc)
	timeline_entry = req.flags.timeline_entry
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": case_status(doc),
			"change_request": serialize(req),
			"current_state": current_state,
			"timeline_event": _format_timeline_event(timeline_entry, doc) if timeline_entry else None,
			**{k: doc.get(k) for k in extra.get("fields", ())},
		},
		message=applied_message if req.status == "Approved" else pending_message,
	)


@route(
	"/<ticket_number>/reassign",
	methods=("POST",),
	summary="Reassign grievance officer, category or department",
	tier="officer-core",
)
@validate_request(ReassignGrievanceRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def reassign(
	ticket_number: str,
	target_department: str | None = None,
	target_officer: str | None = None,
	target_category: str | None = None,
	target_grievance_type: str | None = None,
	reason: str | None = None,
	**kwargs,
):
	"""Request a new officer, category or department for the case.

	Applied at once for the receiving desk's head or an admin; otherwise it waits
	for that head and the case does not move.
	"""
	from oan_grievance_service.api.v1.change_request import raise_change_request
	from oan_grievance_service.services import reassignment

	doc = _load(ticket_number, ptype="write")
	changes = reassignment.resolve(
		doc,
		target_department,
		target_officer,
		frappe.session.user,
		category=target_category,
		grievance_type=target_grievance_type,
	)

	req = raise_change_request(doc, _("Reassignment"), changes, reason=reason)
	return _change_response(
		doc,
		req,
		_("Grievance reassigned successfully"),
		_("Reassignment requested; awaiting approval"),
		fields=(
			"assigned_dept",
			"assigned_to",
			"service_category",
			"grievance_type",
			"sla_due_date",
		),
	)


@route(
	"/<ticket_number>/escalate",
	methods=("POST",),
	summary="Escalate a grievance to the next level up",
	tier="officer-core",
)
@validate_request(EscalateGrievanceRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def escalate(ticket_number: str, reason: str, target_officer: str | None = None, **kwargs):
	"""Hand the case one level up, now, as a missed deadline would.

	Open to whoever may act on the case: its officer, an officer above them on its desk, or an
	administrator. Only while the case waits on an officer (Assigned, Reopened, In Progress).
	`target_officer` must be one of the officers at the next level up who can take the case;
	without it the case goes where a breach would send it. The case is flagged escalated, runs on
	the new level's SLA days when that level has its own, and the receiving officer is notified.
	The reason goes on the timeline, which the submitter can read.
	"""
	from oan_grievance_service.services import reassignment

	doc = _load(ticket_number, ptype="write")
	if doc.workflow_state not in (C.STATE_ASSIGNED, C.STATE_REOPENED, C.STATE_IN_PROGRESS):
		frappe.throw(
			_("Only a grievance waiting on an officer can be escalated."),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)
	above = reassignment.next_level_officers(doc)
	if target_officer and target_officer not in above:
		frappe.throw(
			_("You can hand this grievance over only to an officer at the next level up."),
			frappe.ValidationError,
			title=_("Invalid Officer"),
		)
	if not above:
		frappe.throw(
			_("Nobody above this level can take the grievance now."),
			frappe.ValidationError,
			title=_("No Higher Authority"),
		)
	sla.escalate(doc, reason=reason.strip(), target=target_officer or None, event=C.EVENT_MANUAL_ESCALATION)
	doc.reload()
	frappe.publish_realtime(
		"timeline_updated",
		{"grievance": doc.name, "ticket_number": doc.ticket_number},
		room=f"doc:Grievance:{doc.name}",
		after_commit=True,
	)
	return success_response(
		data={"ticket_number": tn.display(doc.ticket_number), "current_state": _current_state(doc)},
		message=_("Grievance escalated"),
	)


class DecideReassignmentRequest(DecisionRequest):
	model_config = ConfigDict(extra="allow")

	ticket_number: str | None = None


@route(
	"/<ticket_number>/reassign/decide",
	methods=("POST",),
	summary="Approve or reject a pending reassignment request",
	tier="officer-core",
)
@validate_request(DecideReassignmentRequest)
@handle_api_errors
@require_permission("Grievance", "write")
def decide_reassignment(ticket_number: str, decision: str, note: str | None = None, **kwargs):
	"""Approve or reject a pending reassignment request for this grievance."""
	doc = _load(ticket_number, ptype="read")
	from oan_grievance_service.api.v1.change_request import decide as cr_decide
	from oan_grievance_service.api.v1.change_request import get_pending_request

	request_name = get_pending_request(doc.name, ["assigned_dept", "assigned_to", "service_category"])
	if not request_name:
		frappe.throw(_("No pending reassignment request found for this grievance."), frappe.DoesNotExistError)

	return cr_decide(name=request_name, decision=decision, note=note)


@route(
	"/<ticket_number>/defer-sla",
	methods=("POST",),
	summary="Extend SLA deadline by deferral",
	tier="officer-core",
)
@validate_request(DeferSLARequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def defer_sla(
	ticket_number: str,
	additional_days: int,
	reason: str,
	**kwargs,
):
	"""Request the SLA deadline be pushed out by `additional_days`.

	A shorthand for a change request on `sla_due_date`, decided up the hierarchy
	unless the deferral policy lets officers defer without a supervisor.
	"""
	from frappe.utils import add_days

	from oan_grievance_service.api.v1.change_request import raise_change_request

	doc = _load(ticket_number, ptype="write")
	if not doc.sla_due_date:
		frappe.throw(_("This grievance has no SLA deadline to defer."), title=_("No SLA Deadline"))

	days = int(additional_days)
	new_due = add_days(get_datetime(doc.sla_due_date), days)
	req = raise_change_request(
		doc, _("SLA deferral: +{0} days").format(days), {"sla_due_date": str(new_due)}, reason=reason
	)
	return _change_response(
		doc,
		req,
		_("SLA deadline extended successfully"),
		_("Deferral requested; awaiting approval"),
		fields=("sla_due_date",),
	)


class DecideDeferralRequest(DecisionRequest):
	model_config = ConfigDict(extra="allow")

	ticket_number: str | None = None


@route(
	"/<ticket_number>/defer-sla/decide",
	methods=("POST",),
	summary="Approve or reject a pending SLA deferral request",
	tier="officer-core",
)
@validate_request(DecideDeferralRequest)
@handle_api_errors
@require_permission("Grievance", "write")
def decide_deferral(ticket_number: str, decision: str, note: str | None = None, **kwargs):
	"""Approve or reject a pending SLA deferral request for this grievance."""
	doc = _load(ticket_number, ptype="read")
	from oan_grievance_service.api.v1.change_request import decide as cr_decide
	from oan_grievance_service.api.v1.change_request import get_pending_request

	request_name = get_pending_request(doc.name, ["sla_due_date"])
	if not request_name:
		frappe.throw(_("No pending deferral request found for this grievance."), frappe.DoesNotExistError)

	return cr_decide(name=request_name, decision=decision, note=note)


@route(
	"/<ticket_number>/duplicate/confirm",
	methods=("POST",),
	summary="Confirm a grievance as a duplicate of its main ticket",
	tier="officer-core",
)
@validate_request(ConfirmDuplicateRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def confirm_duplicate(ticket_number: str, main_ticket: str | None = None, note: str | None = None, **kwargs):
	"""An officer rules the case is a duplicate of `main_ticket`.

	The main ticket defaults to the best match the system flagged. The case moves to
	Duplicate, a message says all further handling happens on the main ticket, and the
	case becomes read-only. It cannot be undone.
	"""
	doc = _load(ticket_number, ptype="write")
	main_name = _load(main_ticket).name if main_ticket else None
	_history, entry = duplicates.confirm(doc, main_name, note)
	doc.reload()
	frappe.publish_realtime(
		"timeline_updated",
		{"grievance": doc.name, "ticket_number": doc.ticket_number},
		room=f"doc:Grievance:{doc.name}",
		after_commit=True,
	)
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": case_status(doc),
			"duplicate_of": tn.display(doc.duplicate_of),
			"current_state": _current_state(doc),
			"timeline_event": _timeline_event_with(entry, doc, []),
		},
		message=_("Grievance marked as a duplicate"),
	)


@route(
	"/<ticket_number>/duplicate/dismiss",
	methods=("POST",),
	summary="Rule that a flagged grievance is not a duplicate",
	tier="officer-core",
)
@validate_request(DismissDuplicateRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def dismiss_duplicate(ticket_number: str, note: str | None = None, **kwargs):
	"""An officer rules the flag is wrong. The case stays a normal one."""
	doc = _load(ticket_number, ptype="write")
	entry = duplicates.dismiss(doc, note)
	return success_response(
		data={
			"ticket_number": tn.display(doc.ticket_number),
			"status": case_status(doc),
			"current_state": _current_state(doc),
			"timeline_event": _timeline_event_with(entry, doc, []),
		},
		message=_("Grievance kept as a normal case"),
	)


@route(
	"/<ticket_number>/response-templates",
	methods=("GET",),
	summary="Response templates for a workflow action, filled in for this grievance",
	tier="officer-core",
)
@validate_request(ResponseTemplatesRequest)
@handle_api_errors
@require_role(STAFF_ROLES)
def response_templates(ticket_number: str, action: str, service_category: str | None = None, **kwargs):
	"""Active templates for workflow `action` that fit the case's department and category,
	most specific first, rendered with the case's details for the officer to edit."""
	from oan_grievance_service.grievance_masters.doctype.grievance_response_template.grievance_response_template import (
		render,
		templates_for,
	)

	doc = _load(ticket_number, ptype="write")
	category = service_category or doc.service_category

	items = []
	for row in templates_for(doc, action, service_category=category):
		rendered = render(row, doc, "body")
		items.append(
			{
				"template": row.name,
				"title": row.title,
				"action": row.workflow_action,
				"workflow_action": row.workflow_action,
				"department": row.department,
				"service_category": row.service_category,
				"reason": rendered,
				"reason_parts": response_body.split(rendered),
				"note": render(row, doc, "note") if row.get("note") else None,
			}
		)
	return success_response(data={"items": items}, message=_("Response templates fetched successfully"))


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
	from oan_grievance_service.permissions import has_grievance_permission

	if not has_grievance_permission(doc, ptype):
		frappe.throw(
			_("Not permitted to access this grievance."), frappe.PermissionError, title=_("Forbidden")
		)
	return doc


@route("/summary", methods=("GET",), summary="KPI cards summarising grievance status", tier="officer-core")
@handle_api_errors
@require_permission("Grievance", "read")
def summary():
	"""Counts of visible grievances on each queue status card.

	The cards are All, Submitted, Reopened, In Progress, More Info Needed,
	Rejected, Resolved and Closed. Officers also get an Assigned card. Any other workflow
	state is counted under In Progress. Each card includes its display order and
	whether the workflow treats it as terminal (no per-card default).
	"""
	return success_response(
		data={"cards": get_status_summary()},
		message=_("Grievance status summary fetched successfully"),
	)


@route("/options", methods=("GET",), summary="Get grievance options and dropdowns", tier="officer-core")
@handle_api_errors
@require_permission("Grievance", "read")
def options(
	service_category: str | None = None,
	category: str | None = None,
	department: str | None = None,
	administrative_area: str | None = None,
	ticket_number: str | None = None,
	**kwargs,
):
	"""Management and lookup options for submitters, grievance officers and admins.

	Returns reference lists for case filing, management, triage, and filtering,
	including departments, lifecycle statuses, categories, and types. Departments and
	categories are only those with a desk that has an active officer.
	When `department` is provided, `service_categories` shrinks to the categories that
	department has an active category assignment for. When the caller is also staff, it
	cascades and includes active officers under that department (optionally narrowed by
	`service_category` and `administrative_area`). With only `service_category`, it lists the
	officers on desks for that category. The caller is never listed. Submitters
	never receive officers.

	Args:
	    service_category (str, optional): Filter grievance types & officers by service category (e.g. 'Inputs').
	    category (str, optional): Alias for service_category.
	    department (str, optional): Department ID. Limits service_categories to that department's
	        category assignments and, for staff, fetches its assigned officers.
	    administrative_area (str, optional): Administrative area to scope officer assignments.
	    ticket_number (str, optional): The case being reassigned. Scopes officers to its region
	        and category and leaves out the category admin.

	Returns:
	    departments: Departments with a staffed desk
	    statuses: Grievance lifecycle statuses with metadata
	    service_categories: Active service categories with a staffed desk (only the department's when `department` is given)
	    grievance_types: Active grievance types (optionally filtered by service_category)
	    submission_channels: Active intake channels
	    officers: Active officers under the specified department and/or service category (staff only, present only if either is passed)
	    can_change_category: With `ticket_number`, for staff: whether the caller may move the case to
	        another category (administrators, and the fallback desk triaging it)

	With `ticket_number` the officers are the assignees that case can be reassigned to: only
	officers whose region covers the case's administrative area (the case's own area wins over
	`administrative_area`), in the case's category unless another is named, and never the
	category admin (the top rung), who is not an assignee for a reassignment. For an officer
	who is not an administrator they are only the officers in line for the caller
	(`reassignment.officer_move_limits`): the next level up, which may be the category admin,
	and the officers below the caller on a desk covering the case. Any other pick waits for
	the receiving head's approval.
	"""
	cat = service_category or category
	exclude_role_levels = None
	move_limits = None
	if ticket_number and permissions.is_staff():
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			top_rung,
		)
		from oan_grievance_service.services import reassignment

		case = _load(ticket_number, ptype="read")
		administrative_area = case.administrative_area
		cat = cat or case.service_category
		move_limits = reassignment.officer_move_limits(case, frappe.session.user)
		exclude_role_levels = {top_rung()} - {None} if move_limits is None else None
	# One lookup serves both lists, so a short name works for either. An unknown department
	# matches nothing: no categories and no officers, and no further queries.
	department_id = find_department(department)
	service_categories = [] if department and not department_id else get_service_categories(department_id)
	grievance_types = get_grievance_types(service_category=cat)

	data = {
		"departments": get_departments(),
		"statuses": get_status_options(),
		"service_categories": service_categories,
		"grievance_types": grievance_types,
		"submission_channels": active_channels(),
	}

	if (department or cat) and permissions.is_staff():
		data["officers"] = get_department_officers(
			department=department_id,
			service_category=cat,
			administrative_area=administrative_area,
			exclude_user=frappe.session.user,
			exclude_role_levels=exclude_role_levels,
		)
		if move_limits is not None:
			data["officers"] = [off for off in data["officers"] if off["user_id"] in move_limits]
	if ticket_number and permissions.is_staff():
		# Whether the reassign form may offer another category or department for this case.
		data["can_change_category"] = move_limits is None

	return success_response(data=data, message=_("Grievance options fetched successfully"))
