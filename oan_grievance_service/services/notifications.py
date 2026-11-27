"""Notifications and communication.

Configuration is a core Notification record per (event, channel). An administrator can
change the channel, recipient, wording or enable state from the desk without a release,
and core gives us real Jinja rendering and a
condition expression for free.

Delivery, however, is ours. Core renders a notification body exactly once, before it
knows who it is addressed to (notification.py: the render at send_an_email precedes
nothing that varies per recipient), and it never sets the language around that render.
Each citizen is written to in their own language, so the send loop
below does one render per recipient inside print_language(), which is the only point at
which the recipient's language is known. Everything else - the template, the enable
flag, the condition, the recipient role - stays in the admin-editable record.

Amharic wording lives in core Translation records rather than in a second Notification
per event, so both languages are editable from the desk. The consequence to remember:
the English source string is the translation key, so a stable context key is passed to
_() and admins must edit the Amharic alongside any English rewording.

To guard against redundant messaging, a queued
row is deduplicated on (grievance, event, recipient) and a disabled Notification, or one
whose condition is false, sends nothing.
"""

import copy
import re

import frappe
from frappe import _
from frappe.core.doctype.sms_settings.sms_settings import _send_sms
from frappe.email.doctype.notification.notification import get_context
from frappe.translate import get_user_lang, print_language
from frappe.utils import now_datetime, strip_html_tags, validate_email_address
from oan_auth_service.api import operational_log as oplog

from oan_grievance_service.services import classification
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import ticket_number as tn

RECIPIENT_SUBMITTER = "Submitter"
RECIPIENT_ASSIGNED_OFFICER = "Assigned Officer"
RECIPIENT_DEPARTMENT_OFFICER = "Department Officer"
RECIPIENT_ROLE_LEVEL = "Role Level"

RECIPIENT_TYPES = (
	RECIPIENT_SUBMITTER,
	RECIPIENT_ASSIGNED_OFFICER,
	RECIPIENT_DEPARTMENT_OFFICER,
	RECIPIENT_ROLE_LEVEL,
)

# Legacy aliases for backward compatibility with existing configs and tests
RECIPIENT_NODAL_OFFICER = "Nodal Officer"
RECIPIENT_DEPARTMENT_HEAD = "Department Head"
RECIPIENT_TOP_LEVEL = "Top Level Authority"

RECIPIENT_ROLES = (
	RECIPIENT_SUBMITTER,
	RECIPIENT_ASSIGNED_OFFICER,
	RECIPIENT_DEPARTMENT_OFFICER,
	RECIPIENT_NODAL_OFFICER,
	RECIPIENT_DEPARTMENT_HEAD,
	RECIPIENT_TOP_LEVEL,
)

ROLE_LEVEL_RECIPIENTS = {
	RECIPIENT_NODAL_OFFICER: "nodal_officer",
	RECIPIENT_TOP_LEVEL: "senior_nodal_officer",
	RECIPIENT_DEPARTMENT_HEAD: "department_head",
}

# Flat recipient ids for the case participants. Every other recipient id is a
# Grievance Role Level code.
PARTICIPANT_IDS = {
	RECIPIENT_SUBMITTER: "submitter",
	RECIPIENT_ASSIGNED_OFFICER: "assigned_officer",
	RECIPIENT_DEPARTMENT_OFFICER: "department_officer",
}
PARTICIPANTS_BY_ID = {recipient_id: recipient for recipient, recipient_id in PARTICIPANT_IDS.items()}

CHANNEL_SMS = "SMS"
CHANNEL_EMAIL = "Email"
# Core's create_system_notification writes per-user Notification Log rows carrying
# document_type, document_name, title and description, which a frontend reads over the
# API. That makes it a real in-app channel, not just the desk bell.
CHANNEL_SYSTEM = "System Notification"

SUPPORTED_CHANNELS = (CHANNEL_SMS, CHANNEL_EMAIL, CHANNEL_SYSTEM)

# Jinja expression, statement and comment blocks.
JINJA_BLOCK = re.compile(r"{{.*?}}|{%.*?%}|{#.*?#}", re.DOTALL)


ALLOWED_TEMPLATE_PLACEHOLDERS: dict[str, dict[str, str]] = {
	"ticket_number": {
		"name": "ticket_number",
		"label": "Ticket Number",
		"description": "Canonical 9-character ticket reference",
		"example": "3001002A0",
	},
	"service_category": {
		"name": "service_category",
		"label": "Service Category",
		"description": "Name of the service category",
		"example": "Fertilizer Distribution",
	},
	"grievance_type": {
		"name": "grievance_type",
		"label": "Grievance Type",
		"description": "Specific grievance type",
		"example": "Delayed Input Supply",
	},
	"assigned_dept": {
		"name": "assigned_dept",
		"label": "Assigned Department",
		"description": "Responsible department handling the case",
		"example": "Agricultural Inputs Bureau",
	},
	"department": {
		"name": "department",
		"label": "Department",
		"description": "Alias for assigned department",
		"example": "Agricultural Inputs Bureau",
	},
	"assigned_to": {
		"name": "assigned_to",
		"label": "Assigned Officer",
		"description": "Full name of the assigned officer",
		"example": "Abebe Kebede",
	},
	"sla_due_date": {
		"name": "sla_due_date",
		"label": "SLA Due Date",
		"description": "Resolution deadline datetime",
		"example": "2026-10-15 17:00:00",
	},
	"state_deadline": {
		"name": "state_deadline",
		"label": "State Deadline",
		"description": "Confirmation window or state timer deadline",
		"example": "2026-10-22 17:00:00",
	},
	"response_days": {
		"name": "response_days",
		"label": "Days to Respond",
		"description": "Whole days left until the state timer runs out, for example the farmer's time to reply",
		"example": "7",
	},
	"status": {
		"name": "status",
		"label": "Status",
		"description": "Current lifecycle status of the grievance",
		"example": "In Progress",
	},
	"submitter_name": {
		"name": "submitter_name",
		"label": "Submitter Name",
		"description": "Name of citizen or organization",
		"example": "Abebe Bikila",
	},
	"closure_reason": {
		"name": "closure_reason",
		"label": "Closure Reason",
		"description": "Explanation provided upon case closure",
		"example": "Resolved following input delivery",
	},
}


# `assigned_to` renders the name: the User id is an email, which must not reach a submitter.
PLACEHOLDER_EXPRESSIONS = {
	"department": "doc.assigned_dept",
	"assigned_to": "frappe.db.get_value('User', doc.assigned_to, 'full_name')",
	"response_days": "frappe.utils.date_diff(doc.state_deadline, frappe.utils.today())",
	"sla_due_date": "frappe.utils.format_datetime(doc.sla_due_date, 'yyyy-MM-dd HH:mm')",
}


def _placeholder_of(arg) -> str:
	"""The placeholder a compiled `.format()` argument stands for."""
	import ast

	code = ast.unparse(arg)
	for name, expression in PLACEHOLDER_EXPRESSIONS.items():
		if name != "department" and code == ast.unparse(ast.parse(expression, mode="eval").body):
			return name
	if isinstance(arg, ast.Attribute) and getattr(arg.value, "id", None) == "doc":
		return arg.attr
	if isinstance(arg, ast.Name):
		return arg.id
	return code


def get_allowed_template_placeholders() -> set[str]:
	"""Allowed fieldnames that can be used as placeholders in notification templates."""
	return set(ALLOWED_TEMPLATE_PLACEHOLDERS.keys())


def get_template_placeholders_metadata() -> list[dict[str, str]]:
	"""Return curated placeholder metadata for API responses."""
	return list(ALLOWED_TEMPLATE_PLACEHOLDERS.values())


def simplify_jinja_expression(code: str) -> str:
	"""Parse an AST inside {{ ... }} and return simplified text with {fieldname} placeholders."""
	import ast

	try:
		node = ast.parse(code.strip(), mode="eval").body
	except Exception:
		return code

	# Pattern 1: _('...', context='...').format(doc.a, doc.b)
	if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
		inner = node.func.value
		if isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "_" and inner.args:
			source_text = getattr(inner.args[0], "value", None)
			if isinstance(source_text, str):
				args = [_placeholder_of(arg) for arg in node.args]
				for i, arg_name in enumerate(args):
					source_text = source_text.replace(f"{{{i}}}", f"{{{arg_name}}}")
				return source_text

	# Pattern 2: _('...', context='...')
	if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_" and node.args:
		source_text = getattr(node.args[0], "value", None)
		if isinstance(source_text, str):
			return source_text

	# Pattern 3: doc.fieldname
	if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == "doc":
		return f"{{{node.attr}}}"

	return code


def unpack_template(template_str: str) -> str:
	"""Convert a compiled Jinja template into a clean user-facing template with {field} placeholders."""
	if not template_str:
		return ""

	def _repl(m):
		return simplify_jinja_expression(m.group(1))

	res = re.sub(r"\{\{\s*(.*?)\s*\}\}", _repl, template_str)
	return res.strip()


def extract_placeholders(template_str: str) -> list[str]:
	"""Extract placeholder names from a simplified or compiled template string."""
	simplified = unpack_template(template_str)
	raw_matches = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}", simplified)
	unique_vars = []
	for p in raw_matches:
		clean = p.removeprefix("doc.")
		if clean not in unique_vars:
			unique_vars.append(clean)
	return unique_vars


def get_source_text(compiled: str) -> str:
	"""Extract the source string from a compiled translatable Jinja expression."""
	import ast

	m = re.search(r"\{\{\s*(.*?)\s*\}\}", compiled)
	if not m:
		return compiled
	expr = m.group(1)
	try:
		node = ast.parse(expr, mode="eval").body
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
			node = node.func.value
		if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_" and node.args:
			val = getattr(node.args[0], "value", None)
			if isinstance(val, str):
				return val
	except Exception:
		pass
	return compiled


PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


def _placeholder_field(name: str, validate_fields: bool) -> str:
	field = name.removeprefix("doc.")
	allowed = get_allowed_template_placeholders()
	if validate_fields and field not in allowed:
		frappe.throw(
			_("Unknown placeholder '{{{0}}}'. Use one of: {1}.").format(field, ", ".join(sorted(allowed))),
			frappe.ValidationError,
		)
	return field


def compile_simple_segment(text: str, context_key: str, validate_fields: bool = True) -> str:
	"""Compile a plain text segment into {{ _('...', context='...').format(...) }}."""
	text = text.strip()
	if not text:
		return ""

	# Only {placeholder} may carry braces: a stray brace breaks .format() and a stray
	# }} or %} breaks the Jinja the text is wrapped in.
	if re.search(r"[{}]", PLACEHOLDER.sub("", text)):
		frappe.throw(
			_("Template text may only use braces for placeholders such as {ticket_number}."),
			frappe.ValidationError,
		)

	unique_vars = list(
		dict.fromkeys(_placeholder_field(p, validate_fields) for p in PLACEHOLDER.findall(text))
	)

	formatted_text = text
	for i, var_name in enumerate(unique_vars):
		formatted_text = re.sub(rf"\{{(?:doc\.)?{re.escape(var_name)}\}}", f"{{{i}}}", formatted_text)

	call = f"_({formatted_text!r}, context={context_key!r})"
	if unique_vars:
		args_str = ", ".join(PLACEHOLDER_EXPRESSIONS.get(v, f"doc.{v}") for v in unique_vars)
		call += f".format({args_str})"

	return f"{{{{ {call} }}}}"


def compile_template(text: str, context_key: str, validate_fields: bool = True) -> str:
	"""Compile user-facing simplified text into translatable Jinja format.

	Accepts `{field}` placeholders and one optional `{% if field %}...{% else %}...{% endif %}`
	tail, whose condition must itself be an allowed placeholder field.
	"""
	if not text:
		return ""
	text = text.strip()

	m = re.match(
		r"^(.*?)\s*\{%\s*if\s+(.+?)\s*%\}([\s\S]*?)\{%\s*else\s*%\}([\s\S]*?)\{%\s*endif\s*%\}$",
		text,
		re.DOTALL,
	)
	if m:
		base_part, cond, then_part, else_part = m.groups()
		if not re.fullmatch(r"(?:doc\.)?[a-zA-Z_][a-zA-Z0-9_]*", cond.strip()):
			frappe.throw(
				_("Condition '{0}' must be a single placeholder field.").format(cond.strip()),
				frappe.ValidationError,
			)
		cond_field = _placeholder_field(cond.strip(), validate_fields)
		if cond_field == "department":
			cond_field = "assigned_dept"
		base_compiled = compile_simple_segment(base_part, context_key, validate_fields)
		then_compiled = compile_simple_segment(then_part, f"{context_key}.then", validate_fields)
		else_compiled = compile_simple_segment(else_part, f"{context_key}.else", validate_fields)
		return f"{base_compiled} {{% if doc.{cond_field} %}}{then_compiled}{{% else %}}{else_compiled}{{% endif %}}"

	return compile_simple_segment(text, context_key, validate_fields)


def template_context(event_code: str, channel: str) -> str:
	"""Per-channel translation context, so one channel's edits never touch another's Translation rows."""
	return f"grievance.{event_code}.{frappe.scrub(channel)}"


def get_translations_for_event(event_code: str, channel: str) -> dict[str, dict[str, str]]:
	"""Return existing translations mapped by language for one event and channel."""
	if not event_code or not channel or not getattr(frappe, "db", None):
		return {}

	body_ctx = template_context(event_code, channel)
	subj_ctx = f"{body_ctx}.subject"
	rows = frappe.get_all(
		"Translation",
		filters={"context": ["in", [body_ctx, subj_ctx]]},
		fields=["language", "context", "translated_text"],
	)
	res: dict[str, dict[str, str]] = {}
	for row in rows:
		lang = row["language"]
		if lang not in res:
			res[lang] = {}
		if row["context"] == body_ctx:
			res[lang]["body"] = row["translated_text"]
		elif row["context"] == subj_ctx:
			res[lang]["subject"] = row["translated_text"]
	return res


def sync_translation(
	language: str,
	source_text: str,
	context: str,
	translated_text: str | None = None,
):
	"""Keep a Translation record in sync when notification wording changes."""
	if not source_text or not context:
		return

	existing = frappe.db.get_value(
		"Translation",
		{"language": language, "context": context},
		"name",
	)
	if existing:
		trans_doc = frappe.get_doc("Translation", existing)
		trans_doc.source_text = source_text
		if translated_text is not None:
			trans_doc.translated_text = translated_text
		trans_doc.save(ignore_permissions=True)
	elif translated_text:
		frappe.get_doc(
			{
				"doctype": "Translation",
				"language": language,
				"source_text": source_text,
				"context": context,
				"translated_text": translated_text,
			}
		).insert(ignore_permissions=True)


def sync_translations(context: str, source_text: str, provided: dict[str, str], source_changed: bool):
	"""Write the translations given for one template segment, in any language.

	When the English changed, a translation not given here was written for the old
	wording, so it is removed rather than left to render stale text.
	"""
	for language, translated_text in provided.items():
		sync_translation(language, source_text, context, translated_text)
	if not source_changed:
		return
	for row in frappe.get_all("Translation", filters={"context": context}, fields=["name", "language"]):
		if row.language not in provided:
			frappe.delete_doc("Translation", row.name, ignore_permissions=True, force=True)


def validate_notification(doc, method=None):
	"""Keep Grievance notification wording translatable. Registered on Notification.

	Citizen messages go out in Amharic and English. The send path renders once
	per recipient inside print_language(), which only moves strings marked with _();
	literal text typed into a template renders identically in every language. So a
	Grievance notification carrying bare literal text would quietly send English to
	Amharic speakers, with nothing in the log to show it had happened.

	This is a heuristic, not a proof. It strips Jinja blocks and rejects readable text in
	what remains, which catches the realistic mistake - someone pastes a sentence in -
	but does not verify that the strings inside {{ }} are themselves wrapped in _().
	"""
	if doc.get("document_type") != "Grievance":
		return

	if doc.get("channel") and doc.channel not in SUPPORTED_CHANNELS:
		frappe.throw(
			_("Grievance notifications support {0}. {1} has no delivery path here.").format(
				", ".join(SUPPORTED_CHANNELS), doc.channel
			),
			title=_("Unsupported Channel"),
		)

	# Subject is checked alongside the body: it is rendered for email and reused as the
	# in-app title, so literal text there reaches the recipient just as untranslated.
	for fieldname in ("subject", "message"):
		value = doc.get(fieldname)
		if not value:
			continue
		remainder = JINJA_BLOCK.sub("", value)
		if any(char.isalnum() for char in remainder):
			frappe.throw(
				_(
					"Grievance notification {0} must be wrapped in _() so it can be sent in the "
					"recipient's language. Found untranslatable text: {1}"
				).format(_(fieldname), frappe.utils.cstr(remainder).strip()[:120]),
				title=_("Untranslatable Message"),
			)


def resolve_recipient(grievance, recipient_role, override=None, role_level=None):
	"""Turn a recipient role or (recipient_type, role_level) pair into a User, or into a bare address.

	Supports:
	- Direct case participants: Submitter, Assigned Officer, Department Officer
	- Dynamic role level: recipient_role == "Role Level" with role_level
	- Legacy aliases: "Nodal Officer", "Top Level Authority", "Department Head"
	- Direct role level names from Grievance Role Level (e.g. "nodal_officer", "zonal_officer")
	"""
	if override:
		return override

	# Normalize legacy role names or direct role levels
	if recipient_role in ROLE_LEVEL_RECIPIENTS:
		role_level = role_level or ROLE_LEVEL_RECIPIENTS[recipient_role]
		recipient_role = RECIPIENT_ROLE_LEVEL
	elif (
		not role_level
		and recipient_role not in PARTICIPANT_IDS
		and recipient_role != RECIPIENT_ROLE_LEVEL
		and frappe.db.exists("Grievance Role Level", recipient_role)
	):
		role_level = recipient_role
		recipient_role = RECIPIENT_ROLE_LEVEL

	if recipient_role == RECIPIENT_SUBMITTER:
		# If the case has no contact details on file (e.g. submitter opted out via can_request_more_info=False),
		# do not notify the submitter.
		if not grievance.contact_mobile and not grievance.contact_email:
			return None

		# submitter is a Link to Submitter Profile, so the User is one hop away. Fall
		# back to the contact snapshot for submitters who never registered a User.
		if grievance.submitter:
			user = frappe.db.get_value("Grievance Submitter Profile", grievance.submitter, "user")
			if user:
				return user
		return grievance.contact_mobile or grievance.contact_email

	if recipient_role == RECIPIENT_ASSIGNED_OFFICER:
		return grievance.assigned_to

	if not (grievance.assigned_dept or grievance.service_category):
		return None

	if recipient_role == RECIPIENT_ROLE_LEVEL and role_level:
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			find_officer_by_role_level,
		)

		officer = find_officer_by_role_level(
			role_level,
			department=grievance.assigned_dept,
			administrative_area=grievance.administrative_area,
			category=grievance.service_category,
		)
		if officer:
			return officer

	if recipient_role == RECIPIENT_DEPARTMENT_OFFICER:
		from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
			find_officer_by_role_level,
		)
		from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
			GrievanceRoleLevel,
		)

		chain = GrievanceRoleLevel.get_chain()
		rungs = dict.fromkeys(rung.name for rung in (chain[-1:] + chain[:1]))
		officer = next(
			(
				found
				for rung in rungs
				if (
					found := find_officer_by_role_level(
						rung,
						department=grievance.assigned_dept,
						administrative_area=grievance.administrative_area,
						category=grievance.service_category,
					)
				)
			),
			None,
		) or (grievance.assigned_to if getattr(grievance, "assigned_to", None) else None)
		if officer:
			return officer

	# Officers come only from Grievance RBAC Assignment now, so an unresolved
	# recipient means no assignment covers this case's department and area. The
	# notification is dropped either way; without this it is dropped invisibly.
	target_label = role_level or recipient_role
	oplog.log_error(
		title=f"Grievance notification recipient unresolved: {target_label}",
		message=(
			f"No recipient could be resolved for role '{target_label}' on grievance "
			f"{grievance.name} (department '{grievance.assigned_dept}', administrative area "
			f"'{grievance.administrative_area}'). Check the Grievance RBAC Assignment "
			f"covering this department and area."
		),
	)
	return None


def _recipient_language(recipient):
	"""The language to render in.

	get_user_lang carries core's own fallback chain (User.language, then System
	Settings, then en), but it assumes the argument is a User. Bare addresses - the
	department mailbox, or an unregistered submitter's mobile - have no User record and
	fall back to the site default.
	"""
	if recipient and frappe.db.exists("User", recipient):
		return get_user_lang(recipient)
	return frappe.db.get_single_value("System Settings", "language") or "en"


# Events that can legitimately happen more than once on a case, so each occurrence is sent.
# The SLA reminders come once per SLA window, not once per case: the case's reminder flags
# stop a repeat within a window and are cleared when the deadline moves or the window restarts
# (deferral, reopen, category change). Deduplicating them here as well dropped every reminder
# of the new window.
REPEATABLE_EVENTS = frozenset(
	{
		C.EVENT_DEADLINE_EXTENDED,
		*C.SLA_REMINDER_EVENTS.values(),
		C.EVENT_SLA_AT_RISK,
	}
)


def _already_queued(grievance_name, event_code, recipient, channel):
	"""Guard against redundant messaging.

	The key includes the channel. "SMS + Email" events are two Notification
	records here, because core's channel is a single Select, and both are meant to go
	out - so deduplicating on (grievance, event, recipient) alone would silently drop
	whichever of the pair was queued second.
	"""
	if event_code in REPEATABLE_EVENTS:
		return False
	return bool(
		frappe.db.exists(
			"Grievance Notification Log",
			{
				"grievance": grievance_name,
				"event": event_code,
				"recipient": recipient,
				"channel": channel,
			},
		)
	)


def _shown_ticket(grievance) -> str:
	"""The ticket as people read it, grouped 3-001-002A-0; stored flat."""
	return tn.display(grievance.ticket_number) or grievance.name


# Stands in for a sensitive case's PIN in everything stored here. The PIN itself is put back only
# into the text that leaves the system (`_outbound_message`).
PIN_MASK = "******"


def sensitive_submitter_text(grievance, reveal_pin=False) -> tuple[str, str]:
	"""(message, subject) a sensitive case sends its submitter: the reference number and PIN only.

	No officer, no office, no category, no date. The Routing Matrix allows 'Woreda office' at
	most where an office must be named; nothing here needs one. The PIN is masked unless
	`reveal_pin`: the Grievance Notification Log, which officers can read, never holds it.
	"""
	ref = _shown_ticket(grievance)
	pin = classification.pin_of(grievance)
	subject = _("Grievance {0}").format(ref)
	if not pin:
		return subject, subject
	return _("Grievance {0}. PIN: {1}").format(ref, pin if reveal_pin else PIN_MASK), subject


def _outbound_message(row, grievance) -> str:
	"""The text a queued row sends: its logged message, with a sensitive case's PIN put back.

	Only the exact masked message `queue` wrote for the case's submitter is unmasked, so no other
	row, and no template text, ever gets the PIN. A link row (STG-518) gets its link and PIN from
	services/guest_access.py, which raises once they are gone, so it is never sent masked.
	"""
	if row.get("event") in C.GUEST_ACCESS_EVENTS:
		from oan_grievance_service.services import guest_access

		return guest_access.outbound_text(row, grievance)
	if not grievance.get("is_sensitive"):
		return row.message
	with print_language(row.language):
		masked, _subject = sensitive_submitter_text(grievance)
		if row.message != masked:
			return row.message
		return sensitive_submitter_text(grievance, reveal_pin=True)[0]


def _is_kebele_level_user(recipient) -> bool:
	"""Whether `recipient` is a user seated only at Kebele level. An address is not."""
	from oan_grievance_service import permissions

	return bool(recipient) and permissions.is_kebele_level_officer(recipient)


def _render_template(template: str | None, context: dict) -> str:
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-ssti
	return frappe.render_template(template, context) if template else ""


def _has_address(recipient, grievance, channel) -> bool:
	"""Whether a row on `channel` would have somewhere to go."""
	try:
		if channel == CHANNEL_SMS:
			_deliverable_mobile(recipient, grievance)
		elif channel == CHANNEL_EMAIL:
			_deliverable_email(recipient, grievance)
	except ValueError:
		return False
	return True


def queue(grievance, event_code, recipient_override=None, dispatch_now=True):
	"""Write a Grievance Notification Log row for an event.

	Returns the log rows created, which is an empty list when every matching
	Notification is disabled, fails its condition, or has already been sent for this
	grievance. `dispatch_now=False` leaves the rows to the sweep, which paces a large batch.
	"""
	rows = []
	guest_link = event_code in C.GUEST_ACCESS_EVENTS

	# Events use core's "Method" trigger rather than Save/Value Change: the lifecycle
	# fires them explicitly, so there is one Notification per (event, channel).
	notification_names = frappe.get_all(
		"Notification",
		filters={"document_type": "Grievance", "event": "Method", "method": event_code, "enabled": 1},
		pluck="name",
	)
	for notification_name in notification_names:
		notification = frappe.get_doc("Notification", notification_name)
		context = get_context(grievance)
		# Templates print `doc.ticket_number`; a copy carries the grouped form so the stored
		# value on the case itself is never touched.
		shown = copy.copy(grievance)
		shown.ticket_number = _shown_ticket(grievance)
		shown_context = {**context, "doc": shown}

		# Core evaluates this inside evaluate_alert; we are not going through that path,
		# so the condition is honoured here or not at all.
		if notification.condition and not frappe.safe_eval(notification.condition, None, context):
			continue

		recipient_role = (
			notification.get("grievance_recipient_type")
			or notification.get("grievance_recipient")
			or RECIPIENT_SUBMITTER
		)
		role_level = notification.get("grievance_role_level")
		to_submitter = recipient_role == RECIPIENT_SUBMITTER
		# An override names the officer for every officer-facing template of the event (the one a
		# case is escalated or reassigned to). A template for the submitter still goes to the
		# submitter: the reopen's officer notice and its N8 SMS are one event.
		recipient = resolve_recipient(
			grievance,
			recipient_role,
			role_level=role_level,
			override=None if to_submitter else recipient_override,
		)
		if not recipient:
			continue

		sensitive = bool(grievance.get("is_sensitive"))
		if sensitive and not to_submitter and _is_kebele_level_user(recipient):
			# A sensitive case is never mentioned to a Kebele-level officer.
			continue

		if notification.channel == CHANNEL_SYSTEM:
			if not frappe.db.exists("User", recipient):
				user = frappe.db.get_value("User", {"email": recipient}, "name")
				if user:
					recipient = user
				else:
					continue

		if _already_queued(grievance.name, event_code, recipient, notification.channel):
			continue

		# The link goes only where it can arrive: a case with an email address and no mobile
		# number is not left with a Failed SMS row.
		if guest_link and not _has_address(recipient, grievance, notification.channel):
			continue

		language = _recipient_language(recipient)

		# The only reason delivery is ours rather than core's: one render per recipient,
		# with the recipient's language in scope. print_language also clears the cached
		# jenv, so _() inside the template resolves against the right dictionary.
		with print_language(language):
			if guest_link:
				# Never the template: the text carries the link and PIN, masked here and put
				# in only on the way out (`_outbound_message`).
				from oan_grievance_service.services import guest_access

				message, subject = guest_access.message_text(grievance, event_code)
			elif sensitive and to_submitter:
				# Never the template: it may name the officer, the office, the category or the
				# deadline. A sensitive case tells its submitter the reference and PIN, nothing else.
				message, subject = sensitive_submitter_text(grievance)
			else:
				message = _render_template(notification.message, shown_context)
				subject = _render_template(notification.subject, shown_context)

		rows.append(
			frappe.get_doc(
				{
					"doctype": "Grievance Notification Log",
					"grievance": grievance.name,
					"event": event_code,
					"recipient": recipient,
					"channel": notification.channel,
					"language": language,
					"status": "Queued",
					"message": message,
					"subject": subject,
					"notification": notification_name,
				}
			).insert(ignore_permissions=True)
		)

	if rows and dispatch_now:
		# Send as soon as the event commits instead of waiting for the sweep, which stays
		# on as the backstop for rows this job skips or loses.
		try:
			from frappe.utils.background_jobs import get_queues_timeout

			queue_name = C.QUEUE_NOTIFICATIONS if C.QUEUE_NOTIFICATIONS in get_queues_timeout() else "default"
			frappe.enqueue(
				"oan_grievance_service.services.notifications.dispatch_queued",
				queue=queue_name,
				grievance=grievance.name,
				enqueue_after_commit=True,
			)
		except Exception:
			oplog.log_error(
				title=f"Failed to enqueue notifications for grievance: {grievance.name}",
				message=frappe.get_traceback(),
			)

	return rows


# The auth service names every User it registers with a synthetic address in this
# domain and keeps the real one in the custom `oan_login_email` field, because core's
# User.validate copies `name` back into `email` on every save (user/user.py:222).
# So `User.email` is never a mailbox for a registered submitter or officer.
SYNTHETIC_ADDRESS_DOMAIN = "@id.openagrinet.internal"


def _submitter_profile_for(recipient, grievance):
	"""The submitter's profile name when the queued row is addressed to a registered
	submitter, else None.

	`resolve_recipient` hands back the submitter's User when they registered one.
	When they did not, it hands back the bare contact snapshot from the case, and
	this returns None: there is no registered contact to prefer.
	"""
	if not grievance.submitter:
		return None
	user = frappe.db.get_value("Grievance Submitter Profile", grievance.submitter, "user")
	return grievance.submitter if user and user == recipient else None


def _address_candidates(recipient, grievance, profile_field, user_fields, case_field):
	"""Where to look for a recipient's contact, in order.

	Precedence, agreed on PR #26 review: for a registered submitter the registered
	contact always wins, first the profile (which the farmer can update) and then
	the User record (from registration). The contact snapshot on the case is used
	only for a submitter with no account, a walk-in or IVR caller that staff filed,
	where the snapshot is the only contact there is. For staff recipients only the
	User record applies. The snapshot is taken at filing time, so for a registered
	farmer it can only ever equal or lag the profile, never lead it.
	"""
	candidates = []
	profile = _submitter_profile_for(recipient, grievance)
	if profile:
		candidates.append(frappe.db.get_value("Grievance Submitter Profile", profile, profile_field))

	if frappe.db.exists("User", recipient):
		for fieldname in user_fields:
			if frappe.db.has_column("User", fieldname):
				candidates.append(frappe.db.get_value("User", recipient, fieldname))
	else:
		# No account: the row carries the contact snapshot itself.
		candidates += [getattr(grievance, case_field, None), recipient]
	return candidates


def _deliverable_email(recipient, grievance):
	"""The address a queued email row actually goes to.

	Registered contact first (profile, then the User's login email), the case's
	snapshot only for a submitter without an account. `User.email` is the last
	resort and is skipped when it is the synthetic registration address. Found on
	the dev bench: every acknowledgement email went to xxxx@id.openagrinet.internal,
	which no mail server delivers.
	"""
	candidates = _address_candidates(
		recipient, grievance, "contact_email", ("oan_login_email", "email"), "contact_email"
	)
	for address in candidates:
		if not address:
			continue
		address = address.strip()
		if address.lower().endswith(SYNTHETIC_ADDRESS_DOMAIN):
			continue
		if validate_email_address(address):
			return address
	raise ValueError(f"No deliverable email address for {recipient}")


def _deliverable_mobile(recipient, grievance):
	"""The number a queued SMS row actually goes to, same precedence as email."""
	candidates = _address_candidates(recipient, grievance, "contact_mobile", ("mobile_no",), "contact_mobile")
	for mobile in candidates:
		# A submitter with no account and only an email address is addressed by that address,
		# which is a candidate here too; it is no number to text.
		if mobile and mobile.strip() and "@" not in mobile:
			return mobile.strip()
	raise ValueError(f"No mobile number for {recipient}")


def _send_email_row(row, grievance):
	"""Deliver one queued email row and return the Communication it threaded onto."""
	from frappe.core.doctype.communication.email import _make as make_communication

	address = _deliverable_email(row.recipient, grievance)

	communication = make_communication(
		doctype="Grievance",
		name=grievance.name,
		content=row.message,
		subject=row.subject or f"Grievance {_shown_ticket(grievance)}",
		recipients=address,
		communication_medium="Email",
		send_email=False,
		communication_type="Automated Message",
	).get("name")

	# The Communication shows on the case, so it keeps the masked text; only the mail carries the PIN.
	frappe.sendmail(
		recipients=[address],
		subject=row.subject or f"Grievance {_shown_ticket(grievance)}",
		message=_outbound_message(row, grievance),
		reference_doctype="Grievance",
		reference_name=grievance.name,
		communication=communication,
		delayed=True,
	)
	return {"communication": communication}


def _send_sms_row(row, grievance):
	"""Deliver one queued SMS row.

	Core writes an SMS Log only on gateway success, and not at all once a send_sms hook
	is registered, so the Grievance Notification Log row is the durable evidence and the
	sms_log link is opportunistic.

	With no gateway configured, core's `_send_sms` only msgprints and returns, so a
	row would be marked Sent for a message that never left. Found on the dev bench:
	every SMS row read Sent with SMS Settings empty. Fail closed instead, so a
	gateway that is missing or not answering shows up as Failed in the log.
	"""
	mobile = _deliverable_mobile(row.recipient, grievance)
	hooked = bool(frappe.get_hooks("send_sms"))

	if not hooked and not frappe.db.get_single_value("SMS Settings", "sms_gateway_url"):
		raise ValueError("No SMS gateway configured in SMS Settings; message not sent.")

	before = frappe.db.get_value("SMS Log", {}, "name", order_by="creation desc")
	_send_sms([mobile], strip_html_tags(_outbound_message(row, grievance)), success_msg=False)
	after = frappe.db.get_value("SMS Log", {}, "name", order_by="creation desc")

	delivered = bool(after and after != before)
	if not hooked and not delivered:
		raise ValueError("SMS gateway did not confirm delivery: no SMS Log was written.")
	return {"sms_log": after} if delivered else {}


def _send_system_row(row, grievance):
	"""Deliver one queued in-app row.

	Core's own path resolves recipients through get_list_of_recipients, which we bypass,
	so the Notification Log row is created directly for the recipient we already
	resolved. Only a User can receive one - for_user is a User link - so a bare address
	such as the department mailbox has nowhere to go.
	"""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	if not frappe.db.exists("User", row.recipient):
		raise ValueError(f"{row.recipient} is not a User and cannot receive an in-app notification")

	user_email = frappe.db.get_value("User", row.recipient, "email") or row.recipient
	from_user = grievance.modified_by or grievance.owner
	if from_user == row.recipient:
		from_user = None

	enqueue_create_notification(
		[u for u in {user_email, row.recipient} if u],
		{
			"type": "Alert",
			"document_type": "Grievance",
			"document_name": grievance.name,
			"subject": row.subject or _shown_ticket(grievance),
			"title": row.subject or _shown_ticket(grievance),
			# Core's Notification Log is read by its recipient only, the submitter themselves.
			"description": _outbound_message(row, grievance),
			"from_user": from_user,
		},
		# Our own log dedupes on (grievance, event, recipient, channel); this stops a
		# retry of a partially-failed dispatch from stacking rows in the bell feed too.
		dedupe_on=["document_type", "document_name", "subject"],
	)
	return {}


def _next_queued_row(grievance=None):
	"""Lock and return the oldest Queued row no other job holds, or None.

	The immediate jobs and the sweep run side by side. Each takes a row lock and skips rows
	another job holds, so no row is sent twice. The lock lasts until the commit after the
	send, and a worker that dies loses it, which leaves the row Queued for the next sweep.
	"""
	rows = frappe.db.sql(
		"""
		select name, grievance, event, channel, recipient, language, message, subject
		from `tabGrievance Notification Log`
		where status = 'Queued' and (%(grievance)s is null or grievance = %(grievance)s)
		order by creation asc, name asc
		limit 1
		for update skip locked
		""",
		{"grievance": grievance},
		as_dict=True,
	)
	return rows[0] if rows else None


def dispatch_queued(limit=100, grievance=None):
	"""Send whatever is queued. Called by the scheduler."""
	grievance_cache = {}

	def _get_cached_grievance(name):
		if name not in grievance_cache:
			grievance_cache[name] = frappe.get_doc("Grievance", name)
		return grievance_cache[name]

	handled = 0
	while handled < limit and (row := _next_queued_row(grievance)):
		update = {"status": "Sent", "sent_at": now_datetime()}
		try:
			if row.channel == CHANNEL_EMAIL:
				update.update(_send_email_row(row, _get_cached_grievance(row.grievance)))
			elif row.channel == CHANNEL_SMS:
				update.update(_send_sms_row(row, _get_cached_grievance(row.grievance)))
			elif row.channel == CHANNEL_SYSTEM:
				update.update(_send_system_row(row, _get_cached_grievance(row.grievance)))
			else:
				# Never silently mark an undelivered row as Sent.
				raise ValueError(f"No delivery path for channel {row.channel}")
		except Exception:
			oplog.log_error(
				title="Grievance notification dispatch failed",
				message=frappe.get_traceback(),
			)
			update = {"status": "Failed", "error": frappe.get_traceback(with_context=False)}

		frappe.db.set_value("Grievance Notification Log", row.name, update, update_modified=False)
		if row.event in C.GUEST_ACCESS_EVENTS:
			# The plain link and PIN are kept only until nothing is left to carry them.
			from oan_grievance_service.services import guest_access

			guest_access.clear_pending_secret_when_sent(row.grievance)
		# Commit per row: it releases this row's lock and records the send before the next.
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
		handled += 1

	return handled
