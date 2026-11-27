# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""Officer management over the existing User and Grievance RBAC Assignment Officer model.

There is no officer record of its own. An officer is a User with Grievance RBAC Assignment
Officer rows, one on each category desk they staff, so routing, permissions and escalation
read exactly what an admin edits here:

- the person (name, email, phone) is the User;
- the level (L1 Nodal, L2 Senior Nodal, L3 Department Head) and the designation are on the row;
- the service categories are the category desks the officer sits on;
- the region, the supervisor (`reports_to`) and availability are on the officer's rows.

A department head (L3) is an officer on the department's desks like the others, at the rung
`department_head`: cases reach them by escalation and they approve the department's
reassignments, and take first-line cases only as a fallback if no junior or senior officer is available.

A Reviewer is placed on the department's desks the same way, at the inactive level
`review_officer`. They are not assigned cases and cannot change any, and what they read is
limited to the scopes of their desks. The `role` of an account is read from its row levels.

An officer is never deleted. Status is read from the rows, so it has no field of its own and
nothing on User: Active is an active row, On Leave is an active row with `on_leave` set (the
desk and its permissions stay, but auto-routing skips the officer), and Inactive is a row with
`active` cleared. Changing status writes every desk row of the officer. A Reviewer is only
Active or Inactive, and Inactive also disables their login and ends their sessions, because a
Reviewer's rows are what limit what they can read.
"""

from collections import defaultdict
from collections.abc import Mapping
from types import SimpleNamespace

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count
from oan_auth_service.api.iam_users import is_delegated_user
from pypika.functions import Max
from pypika.terms import Case

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	home_seat,
	top_rung,
)
from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
	GrievanceRoleLevel,
)
from oan_grievance_service.permissions import is_unrestricted
from oan_grievance_service.services import account, category_assignment
from oan_grievance_service.services import constants as C
from oan_grievance_service.services.resolvers import (
	new_or_resolved_department,
	resolve_administrative_area,
	resolve_department,
	resolve_service_category,
)

DESK = category_assignment.DOCTYPE
ROW = "Grievance RBAC Assignment Officer"


def get_level_codes() -> dict[str, str]:
	"""Map 'L1', 'L2', 'L3', 'L4' etc. to the level_code based on level_order in GrievanceRoleLevel."""
	chain = GrievanceRoleLevel.get_chain()
	return {f"L{i + 1}": rung.name for i, rung in enumerate(chain)}


def get_code_levels() -> dict[str, str]:
	return {code: level for level, code in get_level_codes().items()}


def reviewer_role_level() -> str:
	"""The role level assigned to reviewers (read-only oversight)."""
	return "review_officer"


def resolve_level_code(key: str | None) -> str | None:
	"""Resolve a tier token ('L1', 'l1'), role level code ('nodal_officer'),
	or display label ('Kebele Nodal Officer') to the canonical Grievance Role Level name.
	"""
	if not key:
		return None
	k = str(key).strip()
	codes = get_level_codes()
	if k.upper() in codes:
		return codes[k.upper()]
	chain = GrievanceRoleLevel.get_chain()
	for rung in chain:
		name = getattr(rung, "name", None) or rung.get("name")
		label = getattr(rung, "level_name", None) or rung.get("level_name")
		if k.lower() == str(name).lower():
			return name
		if label and k.lower() == str(label).lower():
			return name
	return None


class DynamicLevelCodes(Mapping):
	def __getitem__(self, key):
		resolved = resolve_level_code(key)
		if resolved is not None:
			return resolved
		raise KeyError(key)

	def __iter__(self):
		return iter(get_level_codes())

	def __len__(self):
		return len(get_level_codes())

	def __contains__(self, key):
		return resolve_level_code(key) is not None


class DynamicCodeLevels(Mapping):
	def __getitem__(self, key):
		codes = get_code_levels()
		if key in codes:
			return codes[key]
		if key in get_level_codes():
			return key
		raise KeyError(key)

	def __iter__(self):
		return iter(get_code_levels())

	def __len__(self):
		return len(get_code_levels())

	def __contains__(self, key):
		return key in get_code_levels() or key in get_level_codes()


class DynamicRoleCodes(Mapping):
	def __getitem__(self, role):
		if role == DEFAULT_ROLE:
			return tuple(get_code_levels())
		elif role == REVIEWER_ROLE:
			return (reviewer_role_level(),)
		raise KeyError(role)

	def __iter__(self):
		return iter((DEFAULT_ROLE, REVIEWER_ROLE))

	def __len__(self):
		return 2


LEVEL_CODES = DynamicLevelCodes()
CODE_LEVELS = DynamicCodeLevels()
DEFAULT_STATUS = "Active"
DEFAULT_ROLE = "Officer"
REVIEWER_ROLE = "Reviewer"
# The Frappe role a User must hold, and the role levels that make a row that role's.
FRAPPE_ROLES = {DEFAULT_ROLE: C.ROLE_OFFICER, REVIEWER_ROLE: C.ROLE_REVIEW_OFFICER}
ROLE_CODES = DynamicRoleCodes()
MUST_CHANGE_PASSWORD_FIELD = account.MUST_CHANGE_PASSWORD_FIELD
# The auth service's custom User field holding the address an account signs in with. User.name
# is a synthetic id and core resets User.email to it on every save, so User.email is not a
# mailbox for an account the auth service registered. The field is absent on a site without it.
LOGIN_EMAIL_FIELD = "oan_login_email"
USER_FIELDS = [
	"name",
	"full_name",
	"email",
	"phone",
	MUST_CHANGE_PASSWORD_FIELD,
]


def create(
	*,
	full_name: str,
	email: str,
	role: str = DEFAULT_ROLE,
	designation: str | None = None,
	level: str | None = None,
	department: str | None = None,
	service_categories: list[str] | None = None,
	phone: str | None = None,
	region: str | None = None,
	status: str = DEFAULT_STATUS,
	reports_to: str | None = None,
	create_department: bool = False,
	desk_names: list[str] | None = None,
) -> str:
	"""Make `email` an officer, or a reviewer, on the category desks of `department`.

	Returns the user id.

	A department head is how a department is staffed first: with `create_department` the
	department is created for them, and any of their categories without a desk there gets one.

	Only an existing login can be staffed: an email with no User is refused. The login keeps
	its own password. A login that is already an officer or reviewer is rejected, and so is a
	System Manager or Administrator, who are never managed here.
	"""
	_assert_status(role, status)
	user_id = _login_user(email)
	existing = role_of(user_id) if user_id else None
	if existing:
		message = (
			_("{0} is already an officer.") if existing == DEFAULT_ROLE else _("{0} is already a reviewer.")
		)
		frappe.throw(message.format(email), frappe.ValidationError)
	if not user_id:
		frappe.throw(
			_("{0} has no login. Create the user first, then add them here.").format(email),
			frappe.DoesNotExistError,
		)
	account.assert_manageable(user_id)
	from oan_grievance_service.grievance_access_control.positions import is_officer_on_leave

	if is_officer_on_leave(user_id):
		frappe.throw(
			_("{0} is on leave and cannot be added to a desk until they return.").format(email),
			frappe.ValidationError,
		)
	placement = _placement(role, level, status)
	placement["designation"] = designation
	placement["administrative_area"] = _resolve_region(region)
	placement["reports_to"] = _resolve_supervisor(reports_to, level)

	is_head = _is_head(level)
	if desk_names is not None:
		desks, missing = [frappe._dict(name=name) for name in desk_names], set()
	else:
		department = (
			new_or_resolved_department(department, is_head and create_department) if department else None
		)
		desks, missing = _category_desks(department, service_categories, allow_missing=is_head)
		if is_head and role == DEFAULT_ROLE:
			_assert_head_free(department, desks, region=placement.get("administrative_area"))
	_assert_supervisor_covers(placement["reports_to"], level, [desk.name for desk in desks], missing)

	user = frappe.get_doc("User", user_id)
	account.write_user(user, {"full_name": full_name, "phone": phone}, FRAPPE_ROLES[role])
	for desk in desks:
		_save_desk(desk.name, user.name, placement)
	for category in sorted(missing):
		category_assignment.open_desk(category, department, {"user": user.name, **placement})
	if role == REVIEWER_ROLE and status == "Inactive":
		account.set_enabled(user.name, False)
	_sync_desk_head_role(user.name)
	return user.name


def reset_temporary_password(user_id: str, password: str) -> str:
	"""Issue a fresh temporary password to an existing officer or reviewer (the forgotten-password path).

	Only an officer or reviewer: this is how an admin recovers an account they manage, and it
	must not be a way to set a password on any other login. The account's current sessions end
	and it must replace the password before signing in again.

	Nobody resets their own password here, and an account that itself holds an admin role is
	never reset here, even if it is also an officer: this sets a password the caller knows, so
	it must not reach an account more powerful than the caller may manage.
	"""
	account.assert_manageable(user_id)
	if user_id == frappe.session.user:
		frappe.throw(
			_("You cannot issue a temporary password to your own account here."),
			frappe.PermissionError,
		)
	if account.user_roles(user_id) & set(C.ADMIN_ROLES):
		frappe.throw(_("A temporary password cannot be issued for this account."), frappe.PermissionError)
	if not role_of(user_id):
		_not_found(user_id)
	account.issue_temporary_password(user_id, password)
	return user_id


def role_of(user_id: str) -> str | None:
	"""Officer or Reviewer, from the levels of the user's desk rows. None for any other login."""
	for role in ROLE_CODES:
		if _rows([user_id], role):
			return role
	return None


def assert_may_read(role: str) -> None:
	"""Officers are read by every admin-read role. Reviewers only by admins.

	Whether a Reviewer may read Reviewer accounts is a product decision still to be confirmed;
	the answer for now is no, and it is one constant (`REVIEWER_READ_ROLES`) to change.
	"""
	allowed = C.ADMIN_READ_ROLES if role == DEFAULT_ROLE else C.REVIEWER_READ_ROLES
	if not set(frappe.get_roles()) & set(allowed):
		frappe.throw(_("You cannot view {0} accounts.").format(role), frappe.PermissionError)


def update(user_id: str, changes: dict) -> str:
	"""Apply a partial update. `department` and `service_categories` re-seat the account on desks.

	A Reviewer has no level or supervisor, so those are refused for them.
	"""
	account.assert_manageable(user_id)
	role = role_of(user_id)
	if not role:
		_not_found(user_id)
	current = _current(user_id, role)
	changes = dict(changes)
	if role == REVIEWER_ROLE:
		refused = sorted(set(changes) & {"level", "reports_to"})
		if refused:
			frappe.throw(
				_("{0} cannot be changed on a Reviewer.").format(", ".join(refused)),
				frappe.ValidationError,
			)
	level = changes.pop("level", current.level)
	status = changes.pop("status", None)
	if status:
		_assert_status(role, status)
	if status == "Inactive" and user_id == frappe.session.user:
		frappe.throw(_("You cannot deactivate your own account."), frappe.PermissionError)
	person = {key: changes.pop(key) for key in ("full_name", "phone") if key in changes}
	if person:
		account.write_user(frappe.get_doc("User", user_id), person, FRAPPE_ROLES[role])

	row_changes = _availability(status) if status else {}
	designation = changes.get("designation", current.designation)
	if "designation" in changes:
		row_changes["designation"] = designation
	if level != current.level:
		row_changes.update(_change_level(user_id, current.level, level))
	if "region" in changes:
		row_changes["administrative_area"] = _resolve_region(changes["region"])
	if "reports_to" in changes:
		row_changes["reports_to"] = _resolve_supervisor(changes["reports_to"], level)

	if "department" in changes:
		department = resolve_department(changes["department"]) if changes["department"] else None
	else:
		department = current.department
	if "desk_names" in changes:
		target, missing = set(changes["desk_names"]), set()
	else:
		categories = changes.get("service_categories")
		if categories is None:
			categories = current.categories
			if _is_head(level) and department != current.department:
				categories = _head_categories_in(department, categories)
		desks, missing = _category_desks(department, categories, allow_missing=_is_head(level))
		target = {desk.name for desk in desks}
		placing_head = level != current.level or {"department", "service_categories"} & changes.keys()
		# An inactive head's desk may have been taken since, so taking it back is a placement too.
		reactivating = bool(status) and status != "Inactive" and current.status == "Inactive"
		if role == DEFAULT_ROLE and _is_head(level) and (placing_head or reactivating):
			resolved_region = (
				_resolve_region(changes["region"])
				if "region" in changes
				else getattr(current, "region", None)
			)
			_assert_head_free(department, desks, user_id, region=resolved_region)
	if role == DEFAULT_ROLE:
		# A new supervisor, or new desks under the one they already have, must still match. A level
		# change clears the supervisor (`_change_level`), so row_changes is read first.
		supervisor = (
			row_changes.get("reports_to")
			if "reports_to" in row_changes
			else next((row.reports_to for row in _rows([user_id], role) if row.reports_to), None)
		)
		if "reports_to" in row_changes or target != current.desks or missing:
			_assert_supervisor_covers(supervisor, level, target, missing)
	on_new = {**_placement(role, level, status or current.status), "designation": designation}
	if role == REVIEWER_ROLE and status == "Active":
		# Before the rows: a desk refuses an active row whose login is disabled.
		account.set_enabled(user_id, True)
	# A position is a role shared by everyone who holds it, so a new level or designation
	# moves this officer's rows to the position that has them, made if there is none.
	# A Reviewer has no level, so no position.
	if level and ("designation" in changes or level != current.level):
		role_level = LEVEL_CODES.get(level, level)
		desig = designation or role_level.replace("_", " ").title()
		row_changes["position"] = frappe.db.get_value(
			"Grievance Officer Position", {"role_level": role_level, "designation": desig}, "name"
		) or (
			frappe.get_doc(
				{
					"doctype": "Grievance Officer Position",
					"position_title": desig,
					"designation": desig,
					"role_level": role_level,
					"active": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	for name in current.desks - target:
		_save_desk(name, user_id, None)
	for name in sorted(target):
		_save_desk(name, user_id, row_changes, on_new=on_new)
	for category in sorted(missing):
		category_assignment.open_desk(category, department, {"user": user_id, **on_new, **row_changes})
	if role == REVIEWER_ROLE and status == "Inactive":
		# After the rows, so they are retired before the login stops existing for the desk.
		account.set_enabled(user_id, False)
	_sync_desk_head_role(user_id)
	return user_id


def list_officers(
	*,
	role=DEFAULT_ROLE,
	level=None,
	department=None,
	status=None,
	service_category=None,
	region=None,
	q=None,
	position=None,
	start=0,
	page_size=20,
):
	"""One page of account ids of `role` and the total, filtered and counted in SQL."""
	row, user = DocType(ROW), DocType("User")
	query = _officer_query(
		role=role,
		level=level,
		department=department,
		service_category=service_category,
		region=region,
		q=q,
		position=position,
	)
	if status:
		query = query.where(_status_filter(row, status))

	total = query.select(Count(user.name).distinct()).run()[0][0]
	ids = (
		query.select(user.name, user.full_name)
		.distinct()
		.orderby(user.full_name)
		.orderby(user.name)
		.limit(page_size)
		.offset(start)
		.run()
	)
	return [name for name, _full_name in ids], total


def status_counts(
	*,
	role=DEFAULT_ROLE,
	level=None,
	department=None,
	service_category=None,
	region=None,
	q=None,
	position=None,
) -> dict[str, int]:
	"""Active, On Leave and Inactive people for the same filters as the list, without `status`.

	Each person is counted once, in the status `_status` gives them: the most available of
	their rows. The list's status filter matches a person with a row in that status, so an
	officer whose rows differ is in two of its pages but in one count here.
	"""
	row, user = DocType(ROW), DocType("User")
	query = _officer_query(
		role=role,
		level=level,
		department=department,
		service_category=service_category,
		region=region,
		q=q,
		position=position,
	)
	per_person = (
		query.select(
			Max(Case().when((row.active == 1) & (row.on_leave == 0), 1).else_(0)).as_("available"),
			Max(Case().when(row.active == 1, 1).else_(0)).as_("reachable"),
		)
		.groupby(user.name)
		.as_("per_person")
	)
	outcomes = (
		frappe.qb.from_(per_person)
		.select(per_person.available, per_person.reachable, Count("*"))
		.groupby(per_person.available, per_person.reachable)
		.run()
	)
	counts = {"active": 0, "on_leave": 0, "inactive": 0}
	for available, reachable, count in outcomes:
		counts["active" if available else "on_leave" if reachable else "inactive"] += count
	return {**counts, "total": sum(counts.values())}


def list_candidates(*, role=DEFAULT_ROLE, q=None, limit=50):
	"""Enabled logins that hold `role`'s Frappe role but sit on no desk, so hold no position yet.

	A delegated login gets the role from the identity provider, so this is how an officer
	onboarded there shows up before they are placed. System Manager and Administrator
	accounts are never listed.
	"""
	user, has_role, row = DocType("User"), DocType("Has Role"), DocType(ROW)
	placed = frappe.qb.from_(row).select(row.user).where(row.parenttype == DESK).where(row.user.notnull())
	privileged = (
		frappe.qb.from_(has_role)
		.select(has_role.parent)
		.where(has_role.parenttype == "User")
		.where(has_role.role.isin(list(C.PRIVILEGED_ROLES)))
	)
	query = (
		frappe.qb.from_(user)
		.join(has_role)
		.on((has_role.parent == user.name) & (has_role.parenttype == "User"))
		.where(has_role.role == FRAPPE_ROLES[role])
		.where(user.enabled == 1)
		.where(user.name.notin(["Administrator", "Guest"]))
		.where(user.name.notin(placed))
		.where(user.name.notin(privileged))
	)
	fields = [user.name, user.full_name, user.email, user.mobile_no]
	if _has_login_email():
		fields.append(getattr(user, LOGIN_EMAIL_FIELD))
	if q:
		like = f"%{q.strip()}%"
		match = user.full_name.like(like) | user.email.like(like)
		if _has_login_email():
			match = match | getattr(user, LOGIN_EMAIL_FIELD).like(like)
		query = query.where(match)
	rows = query.select(*fields).distinct().orderby(user.full_name).orderby(user.name).limit(limit)
	return [
		{
			"name": r.name,
			"full_name": r.full_name or r.name,
			"email": login_email(r) or r.name,
			"phone": r.mobile_no,
			"delegated": is_delegated_user(r.name),
		}
		for r in rows.run(as_dict=True)
	]


def _officer_query(*, role, level, department, service_category, region, q, position=None):
	"""Rows of `role` with an enabled login on category desks narrowed by every list filter except status."""
	row, desk, user = DocType(ROW), DocType(DESK), DocType("User")
	if level:
		if level not in LEVEL_CODES:
			frappe.throw(
				_("Invalid or unconfigured officer level '{0}'.").format(level), frappe.ValidationError
			)
		codes = [LEVEL_CODES[level]]
	else:
		codes = list(ROLE_CODES[role])
	query = (
		frappe.qb.from_(row)
		.join(desk)
		.on(desk.name == row.parent)
		.join(user)
		.on(user.name == row.user)
		.where(row.parenttype == DESK)
		.where(row.role_level.isin(codes))
	)
	if role != REVIEWER_ROLE:
		query = query.where(user.enabled == 1)
	if department:
		query = query.where(desk.department_scope == resolve_department(department))
	if service_category:
		query = query.where(desk.category_scope == resolve_service_category(service_category))
	if region:
		# The area and every area under it, so a woreda lists its kebele officers too.
		area = DocType("Grievance Administrative Area")
		lft, rgt = frappe.db.get_value(
			"Grievance Administrative Area", _resolve_region(region), ["lft", "rgt"]
		)
		query = query.where(
			row.administrative_area.isin(
				frappe.qb.from_(area).select(area.name).where((area.lft >= lft) & (area.rgt <= rgt))
			)
		)
	if position:
		query = query.where(row.position == position)
	if q:
		like = f"%{q.strip()}%"
		match = user.full_name.like(like) | user.email.like(like)
		if _has_login_email():
			match = match | getattr(user, LOGIN_EMAIL_FIELD).like(like)
		query = query.where(match)
	return query


def _login_user(email: str) -> str | None:
	"""The User id of the login that signs in with `email`: its login email, else its id."""
	email = email.strip()
	if _has_login_email():
		found = frappe.db.get_value("User", {LOGIN_EMAIL_FIELD: email}, "name")
		if found:
			return found
	return email if frappe.db.exists("User", email) else None


def _has_login_email() -> bool:
	return frappe.db.has_column("User", LOGIN_EMAIL_FIELD)


def login_email(person) -> str:
	"""The address `person` (a User row) signs in with: its login email, else User.email."""
	return (person.get(LOGIN_EMAIL_FIELD) or person.email or "").strip()


def records(user_ids: list[str]) -> list[dict]:
	"""API records for officers and reviewers, in the order given, with a fixed number of queries."""
	if not user_ids:
		return []
	rows = _rows(user_ids)
	by_user = defaultdict(list)
	for row in rows:
		by_user[row.user].append(row)

	supervisors = {row.reports_to for row in rows if row.reports_to}
	fields = [*USER_FIELDS, LOGIN_EMAIL_FIELD] if _has_login_email() else USER_FIELDS
	people = {
		person.name: person
		for person in frappe.get_all(
			"User", filters={"name": ["in", list(set(user_ids) | supervisors)]}, fields=fields
		)
	}
	# How many officers hold each rung of each desk, for `home_seat`, in one query.
	sharing = defaultdict(int)
	for seat in frappe.get_all(
		ROW,
		filters={"parent": ["in", list({row.parent for row in rows})], "parenttype": DESK, "active": 1},
		fields=["parent", "role_level"],
	):
		sharing[(seat.parent, seat.role_level)] += 1
	area_names = {
		area.name: area.area_name
		for area in frappe.get_all(
			"Grievance Administrative Area",
			filters={
				"name": [
					"in",
					list({row.administrative_area for row in rows if row.administrative_area}),
				]
			},
			fields=["name", "area_name"],
		)
	}

	records = []
	for user_id in user_ids:
		person, every = people.get(user_id), by_user[user_id]
		if not person or not every:
			continue
		# An account that is somehow both is an officer: reviewer rows only add read access.
		role = DEFAULT_ROLE if any(row.role_level in CODE_LEVELS for row in every) else REVIEWER_ROLE
		own = [row for row in every if row.role_level in ROLE_CODES[role]]
		top = max(own, key=lambda row: row.level_order)
		# Their own department, not that of a desk they co-handle (`home_seat`).
		home = home_seat([row for row in own if row.active] or own, sharing) or top
		region = next((row.administrative_area for row in own if row.administrative_area), None)
		supervisor = next((row.reports_to for row in own if row.reports_to), None)
		records.append(
			{
				"name": person.name,
				"full_name": person.full_name,
				"role": role,
				"designation": next((row.designation for row in own if row.designation), None),
				"level": CODE_LEVELS.get(top.role_level),
				"department": home.department_scope,
				"email": login_email(person),
				"phone": person.phone,
				"must_change_password": bool(person.get(MUST_CHANGE_PASSWORD_FIELD)),
				"region": region,
				"region_name": area_names.get(region),
				"status": _status(own),
				"service_categories": sorted({row.category_scope for row in own if row.category_scope}),
				"reports_to": supervisor,
				"reports_to_name": people[supervisor].full_name if supervisor in people else None,
				"assignments": [_assignment(row) for row in own],
			}
		)
	return records


def _assignment(row) -> dict:
	"""One desk the account sits on: the RBAC assignment the detail reports."""
	return {
		"assignment": row.parent,
		"service_category": row.category_scope,
		"department": row.department_scope,
		"level": CODE_LEVELS.get(row.role_level),
		"region": row.administrative_area,
		"position_id": row.position,
		"active": bool(row.active),
		"on_leave": bool(row.on_leave),
	}


def record(user_id: str) -> dict:
	"""One officer or reviewer as an API record, or DoesNotExistError."""
	found = records([user_id])
	if not found:
		_not_found(user_id)
	return found[0]


def _current(user_id: str, role: str) -> SimpleNamespace:
	"""What an account is today, read from its desk rows of `role`."""
	rows = _rows([user_id], role)
	if not rows:
		_not_found(user_id)
	return SimpleNamespace(
		level=CODE_LEVELS.get(max(rows, key=lambda row: row.level_order).role_level),
		department=rows[0].department_scope,
		categories=sorted({row.category_scope for row in rows if row.category_scope}),
		desks={row.parent for row in rows},
		status=_status(rows),
		designation=next((row.designation for row in rows if row.designation), None),
		region=next((row.administrative_area for row in rows if row.administrative_area), None),
	)


def _not_found(user_id: str):
	frappe.throw(_("Officer '{0}' was not found.").format(user_id), frappe.DoesNotExistError)


def _rows(user_ids: list[str], role: str | None = None) -> list:
	"""The desk rows of `user_ids`, of `role` or of either, with the desk's scope and level order."""
	codes = ROLE_CODES[role] if role else [*ROLE_CODES[DEFAULT_ROLE], *ROLE_CODES[REVIEWER_ROLE]]
	row, desk, level = DocType(ROW), DocType(DESK), DocType("Grievance Role Level")
	return (
		frappe.qb.from_(row)
		.join(desk)
		.on(desk.name == row.parent)
		.join(level)
		.on(level.name == row.role_level)
		.where(row.parenttype == DESK)
		.where(row.user.isin(user_ids))
		.where(row.role_level.isin(list(codes)))
		.select(
			row.user,
			row.parent,
			row.role_level,
			row.administrative_area,
			row.designation,
			row.reports_to,
			row.active,
			row.on_leave,
			desk.department_scope,
			desk.category_scope,
			level.level_order,
		)
		.orderby(row.parent)
		.run(as_dict=True)
	)


def _head_categories_in(department: str | None, own: list[str]) -> list[str]:
	"""A head moving to `department` keeps their categories and takes on the department's (a union)."""
	theirs = frappe.get_all(
		DESK,
		filters=category_assignment.desk_filters(department_scope=department or ["is", "not set"], active=1),
		pluck="category_scope",
	)
	return sorted(set(own) | set(theirs))


def is_head(level: str | None) -> bool:
	"""Whether `level` is the top rung (desk/department head)."""
	top = top_rung()
	return bool(top) and LEVEL_CODES.get(level) == top


def _is_head(level: str | None) -> bool:
	return is_head(level)


def _category_desks(
	department: str | None, categories: list[str] | None, allow_missing: bool = False
) -> tuple[list, set]:
	"""The category-only desks of `department` for `categories`, and the categories that have none.

	A missing desk is an error unless `allow_missing`.
	"""
	if not categories and not allow_missing:
		frappe.throw(_("An officer needs at least one service category."), frappe.ValidationError)
	names = list(dict.fromkeys(resolve_service_category(value) for value in categories or [])) or [None]
	filters = category_assignment.desk_filters(
		department_scope=department or ["is", "not set"],
		category_scope=["in", names] if names != [None] else ["is", "not set"],
	)
	desks = frappe.get_all(DESK, filters=filters, fields=["name", "category_scope", "department_scope"])
	missing = set(names) - {desk.category_scope or None for desk in desks}
	if missing and not allow_missing:
		frappe.throw(
			_(
				"{0} has no category assignment for: {1}. Create it first. "
				"An assignment scoped to a grievance type or service provider does not count."
			).format(department, ", ".join(sorted(missing))),
			frappe.ValidationError,
		)
	return desks, missing


# How many taken categories the duplicate-admin error spells out; the rest are counted.
MAX_CONFLICTS_SHOWN = 5


def _assert_head_free(department, desks, user_id=None, region=None) -> None:
	"""A desk has one category admin per region: refuse a second on a category, department and area.

	`desks` are the existing desks for the categories and department being staffed, a
	missing department meaning the desks that have none. A desk that already has an active
	category admin for this area is handled by them. `user_id` is the officer being edited, who does not
	count against themselves.

	Every category that is already taken is reported at once, so a caller who picked several
	does not have to retry one conflict at a time. An admin who is on leave still holds the desk.
	"""
	top = top_rung()
	if not top or not desks:
		return
	filters = {
		"parenttype": DESK,
		"parent": ["in", [desk.name for desk in desks]],
		"role_level": top,
		"active": 1,
		"user": ["!=", user_id or ""],
	}
	taken_all = frappe.get_all(
		ROW,
		filters=filters,
		fields=["parent", "user", "on_leave", "administrative_area"],
	)
	if not taken_all:
		return
	if region:
		taken = [r for r in taken_all if not r.administrative_area or r.administrative_area == region]
	else:
		taken = taken_all
	if not taken:
		return
	admins_by_desk = {}
	for row in taken:
		label = _("{0} (on leave)").format(row.user) if row.on_leave else row.user
		admins_by_desk.setdefault(row.parent, []).append(label)
	where = _("department {0}").format(department) if department else _("no department")
	conflicting = sorted(
		(desk for desk in desks if desk.name in admins_by_desk),
		key=lambda desk: desk.get("category_scope") or "",
	)
	conflicts = [
		_("{0} in {1}: {2}").format(
			desk.get("category_scope") or _("All categories"),
			where,
			", ".join(sorted(admins_by_desk[desk.name])),
		)
		for desk in conflicting[:MAX_CONFLICTS_SHOWN]
	]
	if len(conflicting) > MAX_CONFLICTS_SHOWN:
		conflicts.append(_("and {0} more").format(len(conflicting) - MAX_CONFLICTS_SHOWN))
	frappe.throw(
		_(
			"Only one active category admin is allowed for each category in a department. "
			"Already assigned: {0}. Choose a different category or department, "
			"or deactivate the current category admin first."
		).format("; ".join(conflicts)),
		frappe.ValidationError,
	)


def _assert_status(role: str, status: str) -> None:
	"""A Reviewer is never on leave: they take no cases, so there is nothing to skip."""
	if role == REVIEWER_ROLE and status == "On Leave":
		frappe.throw(_("A Reviewer is Active or Inactive, not On Leave."), frappe.ValidationError)


def _availability(status: str) -> dict:
	"""Officer-row flags for a status. On Leave stays active: the desk row and its permissions remain."""
	return {
		"active": 0 if status == "Inactive" else 1,
		"on_leave": 1 if status == "On Leave" else 0,
	}


def _status(rows: list) -> str:
	"""An officer's status from their desk rows: the most available one wins.

	Every write here keeps an officer's rows equal. They can differ only if an admin edits one
	desk directly, and then the officer is still reachable while any desk takes cases.
	"""
	if any(row.active and not row.on_leave for row in rows):
		return "Active"
	return "On Leave" if any(row.active for row in rows) else "Inactive"


def _status_filter(row, status: str):
	"""SQL for officers with a row in `status`, the same reading as `_status`."""
	if status == "Active":
		return (row.active == 1) & (row.on_leave == 0)
	if status == "On Leave":
		return (row.active == 1) & (row.on_leave == 1)
	return row.active == 0


def _placement(role: str, level: str | None, status: str) -> dict:
	"""Row fields that follow from the role, level and status."""
	if role == REVIEWER_ROLE:
		return {"role_level": reviewer_role_level(), "is_primary": 0, **_availability(status)}
	if level not in LEVEL_CODES:
		frappe.throw(_("Invalid or unconfigured officer level '{0}'.").format(level), frappe.ValidationError)
	code = LEVEL_CODES[level]
	chain = GrievanceRoleLevel.get_chain()
	lowest_code = chain[0].name if chain else None
	return {
		"role_level": code,
		"is_primary": 1 if code == lowest_code else 0,
		**_availability(status),
	}


def _change_level(user_id: str, old: str, new: str) -> dict:
	"""Row fields for moving an officer between rungs."""
	if new not in LEVEL_CODES:
		frappe.throw(_("Invalid or unconfigured officer level '{0}'.").format(new), frappe.ValidationError)
	chain = GrievanceRoleLevel.get_chain()
	lowest_code = chain[0].name if chain else None
	old_code = LEVEL_CODES.get(old, old)
	if (
		lowest_code
		and old_code != lowest_code
		and frappe.db.exists(ROW, {"reports_to": user_id, "parenttype": DESK})
	):
		frappe.throw(
			_("{0} cannot change level while other officers report to them.").format(user_id),
			frappe.ValidationError,
		)
	code = LEVEL_CODES[new]
	return {
		"role_level": code,
		"is_primary": 1 if code == lowest_code else 0,
		"reports_to": None,
	}


def _resolve_region(value: str | None) -> str | None:
	if not value:
		return None
	name = resolve_administrative_area(value)
	if not name:
		frappe.throw(_("Region '{0}' does not exist.").format(value), frappe.ValidationError)
	return name


def _resolve_supervisor(user_id: str | None, level: str | None) -> str | None:
	"""An officer reports to a higher active rung: escalation climbs to them."""
	if not user_id:
		return None
	chain = GrievanceRoleLevel.get_chain()
	order = {r.name: r.level_order for r in chain}
	my_code = LEVEL_CODES.get(level) if level in LEVEL_CODES else level
	if not my_code or my_code not in order:
		frappe.throw(_("Invalid or unknown officer level '{0}'.").format(level), frappe.ValidationError)
	my_order = order[my_code]
	top = top_rung()
	if my_code == top:
		frappe.throw(_("The department head cannot report to another officer."), frappe.ValidationError)

	higher_codes = [r.name for r in chain if r.level_order > my_order]
	if not higher_codes:
		frappe.throw(_("Only officers below the top rung report to someone."), frappe.ValidationError)

	if not frappe.db.exists(ROW, {"user": user_id, "parenttype": DESK, "role_level": ["in", higher_codes]}):
		higher_labels = [k for k, v in LEVEL_CODES.items() if v in higher_codes]
		frappe.throw(
			_("{0} is not an officer above {1} ({2}).").format(user_id, level, ", ".join(higher_labels)),
			frappe.ValidationError,
		)
	return user_id


def _assert_supervisor_covers(
	supervisor: str | None, level: str | None, desk_names, extra_categories=()
) -> None:
	"""The supervisor must sit, above the officer, on a desk of each of the officer's categories.

	`_resolve_supervisor` only asks that they hold a higher rung somewhere, which let an L1 of
	one category report to an L2 of another (STG-516). Escalation follows `reports_to` from any
	of the officer's desks, so a supervisor missing one of those categories would be handed
	cases they cannot open. `extra_categories` are categories whose desk is opened on save.
	"""
	if not supervisor or _is_head(level):
		return
	categories = set(extra_categories)
	if desk_names:
		categories |= set(
			frappe.get_all(DESK, filters={"name": ["in", list(desk_names)]}, pluck="category_scope")
		)
	categories.discard(None)
	categories.discard("")
	if not categories:
		return

	chain = GrievanceRoleLevel.get_chain()
	order = {r.name: r.level_order for r in chain}
	my_order = order.get(LEVEL_CODES.get(level) if level in LEVEL_CODES else level)
	higher = [r.name for r in chain if my_order is None or r.level_order > my_order]
	supervisor_desks = frappe.get_all(
		ROW,
		filters={"user": supervisor, "parenttype": DESK, "active": 1, "role_level": ["in", higher]},
		pluck="parent",
	)
	covered = (
		set(frappe.get_all(DESK, filters={"name": ["in", supervisor_desks]}, pluck="category_scope"))
		if supervisor_desks
		else set()
	)
	missing = sorted(categories - covered)
	if missing:
		frappe.throw(
			_(
				"{0} is not a senior officer for {1}. Choose a supervisor from the same service category."
			).format(supervisor, ", ".join(missing)),
			frappe.ValidationError,
		)


def _sync_desk_head_role(user_id: str) -> None:
	top = top_rung()
	holds = bool(top) and frappe.db.exists(
		ROW, {"user": user_id, "parenttype": DESK, "role_level": top, "active": 1}
	)
	user = frappe.get_doc("User", user_id)
	has = any(row.role == C.ROLE_DESK_HEAD for row in user.roles)
	if holds and not has:
		user.append_roles(C.ROLE_DESK_HEAD)
	elif has and not holds:
		user.set("roles", [row for row in user.roles if row.role != C.ROLE_DESK_HEAD])
	else:
		return
	user.save(ignore_permissions=True)


def _head_records(caller: str) -> list:
	top = top_rung()
	return [row for row in _rows([caller], DEFAULT_ROLE) if row.role_level == top and row.active]


def _assert_not_admin(user_id: str) -> None:
	"""A desk head never reaches a login that holds an admin role, even one that is also an officer.

	Like `reset_temporary_password`: such an account is more powerful than the head may manage.
	"""
	if account.user_roles(user_id) & set(C.ADMIN_ROLES):
		frappe.throw(
			_("{0} holds an admin role and cannot be managed by a desk head.").format(user_id),
			frappe.PermissionError,
		)


def scope_create(caller: str, fields: dict) -> dict:
	"""Seat a Desk Head's new officer on the head's own RBAC records, reporting to them by default.

	Admins pass through. `department` and `service_categories` narrow which of the head's
	records the officer joins; without them the officer joins all of them.
	"""
	if is_unrestricted(caller):
		return fields
	level = fields.get("level")
	if fields.get("role", DEFAULT_ROLE) != DEFAULT_ROLE or not level or _is_head(level):
		frappe.throw(_("A desk head can only create subordinate officers."), frappe.PermissionError)
	if fields.get("create_department"):
		frappe.throw(_("A desk head cannot create departments."), frappe.PermissionError)
	_assert_not_admin(_login_user(fields["email"]) or fields["email"])
	records = _narrow(_head_records(caller), fields.get("department"), fields.get("service_categories"))
	return {
		**fields,
		"desk_names": [row.parent for row in records],
		"reports_to": fields.get("reports_to") or caller,
	}


def scope_update(caller: str, user_id: str, changes: dict) -> dict:
	"""A Desk Head edits only subordinate officers who sit on nothing but the head's records."""
	if is_unrestricted(caller):
		return changes
	if role_of(user_id) != DEFAULT_ROLE:
		frappe.throw(_("A desk head can only manage officers."), frappe.PermissionError)
	_assert_not_admin(user_id)
	current = _current(user_id, DEFAULT_ROLE)
	if _is_head(current.level) or _is_head(changes.get("level", current.level)):
		frappe.throw(_("A desk head cannot manage desk head officers."), frappe.PermissionError)
	if "department" in changes:
		frappe.throw(_("A desk head cannot move officers between departments."), frappe.PermissionError)
	records = _head_records(caller)
	if not current.desks <= {row.parent for row in records}:
		frappe.throw(
			_("{0} also sits on RBAC records you are not the head of.").format(user_id),
			frappe.PermissionError,
		)
	if "service_categories" not in changes:
		return changes
	scoped = {key: value for key, value in changes.items() if key != "service_categories"}
	scoped["desk_names"] = [row.parent for row in _narrow(records, None, changes["service_categories"])]
	return scoped


def _narrow(records: list, department: str | None, categories: list[str] | None) -> list:
	"""The head's records in `department` and `categories`; refused when none match."""
	if department:
		dept = resolve_department(department)
		records = [row for row in records if row.department_scope == dept]
	if categories:
		names = {resolve_service_category(value) for value in categories}
		records = [row for row in records if row.category_scope in names]
	if not records:
		frappe.throw(_("None of your RBAC records match."), frappe.PermissionError)
	return records


def _save_desk(desk_name: str, user_id: str, values: dict | None, on_new: dict | None = None):
	"""Put `user_id` on a desk with `values`, or take them off when `values` is None.

	`on_new` fills the fields of a row that did not exist yet. The desk is locked so two
	admins editing the same desk are serialised, and its own validation runs on save.
	"""
	desk = frappe.get_doc(DESK, desk_name, for_update=True)
	mine = next((row for row in desk.officers if row.user == user_id), None)
	if values is None:
		if mine:
			if len(desk.officers) == 1:
				frappe.throw(
					_("{0} would be left without officers. Assign another officer first.").format(desk_name),
					frappe.ValidationError,
				)
			desk.remove(mine)
	elif mine:
		mine.update(values)
	else:
		row_dict = {"user": user_id, **(on_new or {}), **values}
		desk.append("officers", row_dict)
	desk.assigned_by = frappe.session.user
	desk.save()
