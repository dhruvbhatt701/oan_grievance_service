"""Profile resolution and hook integration for OAN Grievance Service."""

import frappe
from frappe import _
from oan_auth_service.api import operational_log as oplog
from pydantic import BaseModel, Field

from oan_grievance_service.api.v1.organization import OrganizationRecord, organization_record
from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	active_scopes,
	home_seat,
)
from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
	GrievanceRoleLevel,
)
from oan_grievance_service.permissions import ROLE_OFFICER, ROLE_REVIEW_OFFICER
from oan_grievance_service.services import constants as C
from oan_grievance_service.services.administrative_area import (
	external_area_id,
	match_administrative_area,
	refreshed_administrative_area,
)
from oan_grievance_service.services.constants import ORG_STATUS_ACTIVE, ORG_TYPES

# Submitter Profile fields the identity provider supplies (the "Identity provider details" section).
IDENTITY_PROFILE_FIELDS = ("id_source", "street_address", "locality", "region", "postal_code", "country")


def resolve_user_profile_hook(user_doc, roles=None) -> tuple[str, dict | None]:
	"""Hook subscriber for oan_auth_service.api.v1.auth.get_me.

	Returns ("grievance", profile_data) namespaced under data["profiles"]["grievance"]
	containing only domain attributes that auth_service does not already provide.
	"""
	user = user_doc.name
	user_roles = set(roles or frappe.get_roles(user))

	# 1. Officer or Reviewer Profile (operational bounds from active scopes)
	if user_roles & {ROLE_OFFICER, ROLE_REVIEW_OFFICER}:
		profile = _staff_profile(user, ROLE_OFFICER in user_roles)
		if profile:
			return "grievance", profile

	# 2. Submitter Profile (citizen / organization identity attributes)
	fields = [
		"name",
		"submitter_type",
		"submission_channel",
		"submitter_name",
		"contact_mobile",
		"contact_email",
		"dedupe_key",
		"administrative_area",
		"administrative_unit",
		"organization",
		*IDENTITY_PROFILE_FIELDS,
	]
	profile = frappe.db.get_value("Grievance Submitter Profile", {"user": user}, fields, as_dict=True)
	if profile:
		from oan_grievance_service.grievance_management.doctype.grievance_submitter_profile.grievance_submitter_profile import (
			split_dedupe_key,
		)
		from oan_grievance_service.services.identity import SCHEME_FAYDA, SCHEME_USER, mask_contact

		scheme, ident_val = split_dedupe_key(profile.get("dedupe_key"))
		# A user key is an internal login id, so it is never shown.
		identities = (
			[{"scheme": scheme, "value": ident_val}] if scheme and ident_val and scheme != SCHEME_USER else []
		)
		contact = mask_contact(profile, show_identity=True)

		# An NGO, FPO or cooperative representative also gets the organization itself: the
		# profile only carries the filing-time copy of its name and contacts.
		details = {}
		if profile.get("organization"):
			org = frappe.get_doc("Grievance Organization", profile.organization)
			record = OrganizationRecord.model_validate(organization_record(org)).model_dump(mode="json")
			details = {
				"registration_number": org.registration_number,
				"representative_name": org.representative_name,
				"organization": {**record, "can_file": org.workflow_state == ORG_STATUS_ACTIVE},
			}
			# The representative's own identity (their Fayda ID) stays; the organization adds its
			# registration number, which the profile's key may not carry.
			identities = [i for i in identities if i["scheme"] != "org"]
			identities.append({"scheme": "org", "value": org.registration_number})

		return "grievance", {
			**details,
			"profile_id": profile.get("name"),
			"type": profile.get("submitter_type"),
			"submitter_type": profile.get("submitter_type"),
			"submission_channel": profile.get("submission_channel"),
			# Only a fayda-anchored profile has one; a phone or org key is not a Fayda ID. An NGO,
			# FPO or cooperative's is its representative's, given at sign-up.
			"fayda_id": ident_val if scheme == SCHEME_FAYDA and ident_val else None,
			"submitter_name": profile.get("submitter_name"),
			"contact_mobile": contact["contact_mobile"],
			"country_code": contact["country_code"],
			"phone_number": contact["phone_number"],
			"contact_email": contact["contact_email"],
			"identities": identities,
			"administrative_area": external_area_id(profile.get("administrative_area")),
			"administrative_unit": profile.get("administrative_unit"),
			"id_source": profile.get("id_source"),
			"address": {
				"street_address": profile.get("street_address"),
				"locality": profile.get("locality"),
				"region": profile.get("region"),
				"postal_code": profile.get("postal_code"),
				"country": profile.get("country"),
			}
			if profile.get("id_source")
			else None,
		}

	return "grievance", None


def _staff_profile(user, is_officer) -> dict | None:
	"""The `/me` grievance profile of an officer or a Reviewer, from their live desk seats.

	`department`, `category` and `administrative_area` are their own desk's (`home_seat`): an
	officer who co-handles another department's category still belongs to theirs. An officer's
	reviewer seats do not count. `departments` and `service_categories` list every desk they sit
	on. A Reviewer gets a profile even before they are placed on a desk, so the portal has a
	role to land them on; an officer on no desk gets none, as before.
	"""
	scopes = active_scopes(user)
	if is_officer:
		chain = {rung.name for rung in GrievanceRoleLevel.get_chain()}
		scopes = [scope for scope in scopes if scope.get("role_level") in chain]
		if not scopes:
			return None
	home = home_seat(scopes) or {}
	level = home.get("role_level")
	return {
		"type": (level and frappe.db.get_value("Grievance Role Level", level, "level_name"))
		or level
		or (ROLE_OFFICER if is_officer else ROLE_REVIEW_OFFICER),
		"department": home.get("department_scope"),
		"administrative_area": external_area_id(home.get("administrative_area_scope")),
		"category": home.get("category_scope"),
		"departments": sorted({s.department_scope for s in scopes if s.get("department_scope")}),
		"service_categories": sorted({s.category_scope for s in scopes if s.get("category_scope")}),
	}


class RegistrationExtras(BaseModel):
	"""Extra fields this app reads from the `POST /api/v1/auth/register` body.

	oan_auth_service forwards every non-core body field to each `on_user_registered`
	subscriber. This model is the contract for the ones this app consumes. Clients send
	it as `profile.grievance` in the register body (flat top-level fields still work,
	deprecated). The hook parses with it, and the spec generator publishes it as
	`GrievanceRegistrationExtras` plus an OpenAPI Overlay for auth's register body.
	Unknown keys are ignored because they belong to other installed apps. Values may
	arrive as JSON numbers, so IDs and phones accept int as well as text.
	"""

	model_config = {"extra": "ignore"}

	submitter_type: str | None = Field(
		default=None, description="Submitter type. Defaults to Individual Farmer."
	)
	submitter_name: str | None = Field(default=None, description="Used when full_name is empty.")
	fayda_id: str | int | None = Field(
		default=None, description="Fayda National ID. Required for Individual Farmer."
	)
	registration_number: str | None = Field(default=None, description="Organization registration number.")
	dedupe_key: str | None = Field(default=None, description="Ignored for Individual Farmer.")
	administrative_area: str | None = None
	administrative_unit: str | None = None
	preferred_language: str | None = None
	contact_mobile: str | int | None = Field(default=None, description="Fallback for phone_number.")
	contact_email: str | None = Field(default=None, description="Fallback for email.")


def on_user_registered_hook(user_doc, role=None, roles=None, **kwargs) -> None:
	"""Hook subscriber for oan_auth_service.api.v1.auth.register_user (on_user_registered).

	Automatically provisions or links a Grievance Submitter Profile when a user
	registers with role 'Grievance Submitter' (or default registration role).
	"""
	user_roles = set(roles or ([role] if role else []))
	if "Grievance Submitter" not in user_roles:
		return

	# Core fields (phone_number, country_code, email) pass through untouched; the rest
	# are read through the typed model so the documented contract is the one enforced.
	# Fields belong under `profile.grievance` in the register body. Flat top-level
	# fields are still accepted for existing clients (deprecated); the namespaced
	# value wins when both are sent.
	namespaced = kwargs.get("profile")
	namespaced = namespaced.get("grievance") if isinstance(namespaced, dict) else None
	extras = RegistrationExtras.model_validate({**kwargs, **(namespaced or {})})
	kwargs = {**kwargs, **extras.model_dump(exclude_none=True)}

	# The User insert that just ran leaves notices in the message log (no outgoing email
	# account, a taken username). The API error envelope joins the whole log into its
	# message, so without this a rejected registration would open with them and bury the
	# actual reason.
	frappe.clear_messages()

	user_name = user_doc.name if hasattr(user_doc, "name") else user_doc.get("name")
	full_name = user_doc.full_name if hasattr(user_doc, "full_name") else user_doc.get("full_name")
	mobile_no = user_doc.mobile_no if hasattr(user_doc, "mobile_no") else user_doc.get("mobile_no")

	# Assemble phone number if provided split
	incoming_phone = mobile_no or kwargs.get("contact_mobile") or kwargs.get("phone_number")
	country_code = kwargs.get("country_code")
	if incoming_phone:
		from oan_auth_service.api.utils import assemble_phone_number

		contact_mobile = assemble_phone_number(incoming_phone, country_code=country_code)
	else:
		contact_mobile = None

	# Extract real email if present
	email = None
	raw_email = getattr(user_doc, "email", None) or (
		user_doc.get("email") if isinstance(user_doc, dict) else None
	)
	login_email = (
		getattr(user_doc, "oan_login_email", None)
		or getattr(user_doc, "user_email", None)
		or (
			user_doc.get("oan_login_email") or user_doc.get("user_email")
			if isinstance(user_doc, dict)
			else None
		)
	)
	if login_email and "@" in str(login_email):
		email = str(login_email)
	elif raw_email and "@id.openagrinet.internal" not in raw_email:
		email = raw_email
	if not email:
		email = kwargs.get("contact_email") or kwargs.get("email")

	from oan_grievance_service.api.v1.submitter import (
		_create_or_update_submitter_profile,
		_is_farmer_submitter_type,
	)

	# The extra register fields arrive as JSON, so a number is possible: read the IDs as text
	# and let the format check report a bad Fayda ID instead of failing on .strip().
	submitter_type = str(kwargs.get("submitter_type") or "Individual Farmer").strip()
	fayda_id = str(kwargs.get("fayda_id") or "").strip() or None
	dedupe_key = kwargs.get("dedupe_key")

	registration_number = kwargs.get("registration_number")
	# An organization signs up as its representative, with their Fayda ID and no registration
	# number: the organization itself is registered at first sign-in (POST /api/v1/orgs).
	needs_fayda = _is_farmer_submitter_type(submitter_type) or (
		submitter_type in ORG_TYPES and not registration_number
	)
	if needs_fayda:
		if not fayda_id:
			frappe.throw(
				_("A Fayda ID is required to register as {0}.").format(submitter_type),
				frappe.ValidationError,
				title=_("Fayda ID Required"),
			)
		# An explicit key outranks the Fayda ID when the dedupe key is derived, so a caller
		# could register with no Fayda ID on the profile. Registration takes the Fayda ID and
		# nothing else here.
		dedupe_key = None

	_create_or_update_submitter_profile(
		user=user_name,
		submitter_type=submitter_type,
		submitter_name=full_name or kwargs.get("submitter_name"),
		contact_mobile=contact_mobile,
		contact_email=email,
		administrative_area=kwargs.get("administrative_area"),
		administrative_unit=kwargs.get("administrative_unit"),
		preferred_language=kwargs.get("preferred_language"),
		fayda_id=fayda_id,
		registration_number=registration_number,
		dedupe_key=dedupe_key,
		# Set here and not as a field default: a profile created anywhere else (an IVR or
		# webhook intake, a desk entry) must not be filed under Web Portal by accident.
		submission_channel="Web Portal",
	)


def on_delegated_login_hook(user: str, principal: dict, login_option: str) -> bool:
	"""Hook subscriber for oan_auth_service's `oan_auth_on_delegated_login`.

	An IAM sign-in (eSignet for farmers) never goes through `register_user`, so
	`on_user_registered` never runs for it. This gives a Grievance Submitter who
	signs in that way the Submitter Profile that registration would have created,
	and keeps its national-ID details (national ID, address) current from the
	identity provider's claims. Names, mobile number, birth date and gender are
	already on the `User` (oan_auth_service copies them first).

	A development agent (Keycloak, `Grievance Assisted Submitter`) gets a Development
	Agent profile from their `User`; the identity provider supplies no national-ID
	details for them, so none are written and `id_source` stays empty. With no phone
	number on the `User`, the profile has none and is keyed on the user.

	Never refuses the login. A profile that cannot be built from the claims (no phone
	number, a taken Fayda ID) is logged and skipped; the user signs in with what they had.
	"""
	roles = set(frappe.get_all("Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role"))
	if roles & {C.ROLE_OFFICER, C.ROLE_REVIEW_OFFICER}:
		return False
	is_agent = C.ROLE_ASSISTED_SUBMITTER in roles
	if not is_agent and C.ROLE_SUBMITTER not in roles:
		return False

	profile_name = frappe.db.get_value("Grievance Submitter Profile", {"user": user}, "name")
	if is_agent:
		return False if profile_name else _create_agent_profile(user, principal, login_option)

	identity = _identity_fields(principal, login_option)
	frappe.db.savepoint("delegated_login_profile")
	try:
		if profile_name:
			return _refresh_identity(profile_name, identity)

		from oan_grievance_service.api.v1.submitter import _create_or_update_submitter_profile
		from oan_grievance_service.services.identity import FAYDA_PATTERN

		# eSignet's `individual_id` is the national ID. Only a 16-digit value is a Fayda ID;
		# anything else (a VID, a pairwise `sub`) is left out.
		individual_id = str(principal.get("individual_id") or "").strip()
		fayda_id = individual_id if FAYDA_PATTERN.match(individual_id) else None
		mobile = frappe.db.get_value("User", user, "mobile_no") or principal.get("phone_number")

		profile = _create_or_update_submitter_profile(
			user=user,
			submitter_type="Individual Farmer",
			submitter_name=principal.get("name"),
			contact_mobile=str(mobile or "").strip() or None,
			contact_email=principal.get("email"),
			administrative_area=identity.pop("administrative_area", None),
			administrative_unit=identity.get("locality"),
			fayda_id=fayda_id,
			submission_channel="Web Portal",
		)
		_refresh_identity(profile.name, identity)
		return True
	except Exception:
		frappe.db.rollback(save_point="delegated_login_profile")
		frappe.clear_messages()
		oplog.log_error(
			title="oan_grievance_service: no submitter profile from delegated login",
			message=f"login option {login_option!r}, user {oplog.mask_user(user)!r}\n{frappe.get_traceback()}",
			defer_insert=True,
		)
		return False


def _create_agent_profile(user: str, principal: dict, login_option: str) -> bool:
	"""A Development Agent profile for `user`, from their `User`. Logged and skipped when it cannot be built."""
	from oan_grievance_service.api.v1.submitter import _create_or_update_submitter_profile

	frappe.db.savepoint("delegated_login_profile")
	try:
		_create_or_update_submitter_profile(
			user=user,
			submitter_type="Development Agent",
			contact_email=principal.get("email"),
			submission_channel="Web Portal",
			mobile_optional=True,
		)
	except Exception:
		frappe.db.rollback(save_point="delegated_login_profile")
		frappe.clear_messages()
		oplog.log_error(
			title="oan_grievance_service: no submitter profile from delegated login",
			message=f"login option {login_option!r}, user {oplog.mask_user(user)!r}\n{frappe.get_traceback()}",
			defer_insert=True,
		)
		return False
	return True


def _identity_fields(principal: dict, login_option: str) -> dict:
	"""The national-ID details of `principal` as Submitter Profile fields, plus its `administrative_area` when one matches."""
	address = principal.get("address")
	address = address if isinstance(address, dict) else {}
	text = lambda value: str(value or "").strip() or None  # noqa: E731
	region = text(address.get("region"))
	fields = {
		"id_source": login_option,
		"street_address": text(address.get("street_address")),
		"locality": text(address.get("locality")),
		"region": region,
		"postal_code": text(address.get("postal_code")),
		"country": text(address.get("country")),
	}
	# The provider's names are matched to ours, region then woreda (`locality`) then kebele
	# (from `street_address`); the deepest unambiguous match is the area, and none leaves it out.
	area = match_administrative_area(region, fields["locality"], fields["street_address"])
	if area:
		fields["administrative_area"] = area
	return fields


def _refresh_identity(profile_name: str, identity: dict) -> bool:
	"""Write the identity provider's details onto the profile. True if any value changed."""
	fields = {k: v for k, v in identity.items() if k in IDENTITY_PROFILE_FIELDS}
	current = frappe.db.get_value(
		"Grievance Submitter Profile", profile_name, [*fields, "administrative_area"], as_dict=True
	)
	fields["administrative_area"] = refreshed_administrative_area(
		current, identity.get("administrative_area")
	)
	changes = {k: v for k, v in fields.items() if (current.get(k) or None) != v}
	if not changes:
		return False
	frappe.db.set_value("Grievance Submitter Profile", profile_name, changes, update_modified=False)
	return True
