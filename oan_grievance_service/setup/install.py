"""Seed data created on install and refreshed on migrate.

A fresh site comes up usable rather than empty: the three capability roles, the
escalation levels, the service categories, the Ethiopian regions, and the full
notification matrix.
"""

import gzip
import json
import os

import frappe

from oan_grievance_service.grievance_sla.doctype.grievance_deferral_policy.grievance_deferral_policy import (
	seed_state_timers,
)
from oan_grievance_service.services import classification, ticket_number
from oan_grievance_service.services import constants as C
from oan_grievance_service.services.notifications import PLACEHOLDER_EXPRESSIONS
from oan_grievance_service.setup.service_users import ensure_ivr_service_user

# The three capability roles. The former L1 / L2 / Department Head roles were rungs of a
# hierarchy rather than distinct capabilities, and are replaced by position in the
# reporting chain - see .docs/sla_workflows_and_lifecycle_specification.md §10.1.
#
# No desk access for any role for now. Every actor - submitter, officer and admin -
# reaches the system through the portal and the v1 API, so Desk is not part of the
# surface being built or secured. Turning it on later is a one-line change per role,
# but it widens the attack surface to every doctype the role holds DocPerm on, so it
# should be a deliberate decision rather than a default.
ROLES = [
	("Grievance Submitter", 0),
	("Grievance Officer", 0),
	("Grievance Admin", 0),
	(C.ROLE_ASSISTED_SUBMITTER, 0),
	("Grievance Dashboard Reader", 0),
	(C.ROLE_REVIEW_OFFICER, 0),
	(C.ROLE_DESK_HEAD, 0),
]

# The Grievance Workflow: a native Frappe Workflow record, rebuilt from
# these tables on every migrate so they are the one place the lifecycle is written
# down. The engine decides whether a move is legal and who may take it; the app
# never keeps its own copy of that answer (tests/test_workflow_contracts.py).
#
# Grievance is not a submittable doctype: a case is filed the moment it is inserted,
# straight into Submitted, and every state is docstatus 0. Closed and Rejected have
# no outbound transition, which is what makes them terminal (the Grievance
# controller refuses edits there); a reopen happens from Resolved, and nowhere else.
WORKFLOW_NAME = "Grievance Workflow"

# (state, style, SLA category). Order matters: Frappe treats the first row
# as where a new document enters.
#
# The SLA category is what arriving in the state does to the clock (services.sla):
# Running keeps it going, Paused holds it while the case waits on the submitter, and
# Stopped ends it for good.
WORKFLOW_STATES = [
	(C.STATE_SUBMITTED, "Info", "Running"),
	(C.STATE_ASSIGNED, "Primary", "Running"),
	(C.STATE_REOPENED, "Warning", "Running"),
	(C.STATE_IN_PROGRESS, "Primary", "Running"),
	(C.STATE_MORE_INFO_NEEDED, "Warning", "Paused"),
	(C.STATE_RESOLVED, "Success", "Stopped"),
	(C.STATE_CLOSED, "Success", "Stopped"),
	(C.STATE_REJECTED, "Danger", "Stopped"),
	(C.STATE_DUPLICATE, "Inverse", "Stopped"),
]

OFFICER_ROLES = ("Grievance Officer", "Grievance Admin")
SUBMITTER_ROLES = ("Grievance Submitter", C.ROLE_ASSISTED_SUBMITTER)
SYSTEM_ROLES = ("Administrator", "System Manager")

# (from, action, to, roles that may take it)
WORKFLOW_TRANSITIONS = [
	(C.STATE_SUBMITTED, "Assign", C.STATE_ASSIGNED, SYSTEM_ROLES),
	(C.STATE_SUBMITTED, "Reject", C.STATE_REJECTED, OFFICER_ROLES),
	(C.STATE_ASSIGNED, "In Progress", C.STATE_IN_PROGRESS, OFFICER_ROLES),
	(C.STATE_ASSIGNED, "Request More Info", C.STATE_MORE_INFO_NEEDED, OFFICER_ROLES),
	(C.STATE_ASSIGNED, "Resolve", C.STATE_RESOLVED, OFFICER_ROLES),
	(C.STATE_ASSIGNED, "Reject", C.STATE_REJECTED, OFFICER_ROLES),
	# A reopened case waits for an officer exactly as an assigned one does.
	(C.STATE_REOPENED, "In Progress", C.STATE_IN_PROGRESS, OFFICER_ROLES),
	(C.STATE_REOPENED, "Request More Info", C.STATE_MORE_INFO_NEEDED, OFFICER_ROLES),
	(C.STATE_REOPENED, "Resolve", C.STATE_RESOLVED, OFFICER_ROLES),
	(C.STATE_REOPENED, "Reject", C.STATE_REJECTED, OFFICER_ROLES),
	(C.STATE_IN_PROGRESS, "Request More Info", C.STATE_MORE_INFO_NEEDED, OFFICER_ROLES),
	(C.STATE_IN_PROGRESS, "Resolve", C.STATE_RESOLVED, OFFICER_ROLES),
	(C.STATE_IN_PROGRESS, "Refer Onward", C.STATE_ASSIGNED, OFFICER_ROLES),
	(C.STATE_IN_PROGRESS, "Reject", C.STATE_REJECTED, OFFICER_ROLES),
	(C.STATE_MORE_INFO_NEEDED, "Submitter Reply", C.STATE_IN_PROGRESS, SUBMITTER_ROLES),
	(C.STATE_MORE_INFO_NEEDED, "Reject", C.STATE_REJECTED, OFFICER_ROLES),
	(C.STATE_MORE_INFO_NEEDED, "Auto Close", C.STATE_CLOSED, SYSTEM_ROLES),
	# Taken only through the duplicate endpoints, which name the main ticket; the generic
	# action endpoint refuses it.
	(C.STATE_SUBMITTED, "Confirm Duplicate", C.STATE_DUPLICATE, OFFICER_ROLES),
	(C.STATE_ASSIGNED, "Confirm Duplicate", C.STATE_DUPLICATE, OFFICER_ROLES),
	(C.STATE_REOPENED, "Confirm Duplicate", C.STATE_DUPLICATE, OFFICER_ROLES),
	(C.STATE_IN_PROGRESS, "Confirm Duplicate", C.STATE_DUPLICATE, OFFICER_ROLES),
	(C.STATE_MORE_INFO_NEEDED, "Confirm Duplicate", C.STATE_DUPLICATE, OFFICER_ROLES),
	(C.STATE_RESOLVED, "Reopen", C.STATE_REOPENED, SUBMITTER_ROLES),
	(C.STATE_RESOLVED, "Close Case", C.STATE_CLOSED, SUBMITTER_ROLES),
	(C.STATE_RESOLVED, "Auto Close", C.STATE_CLOSED, SYSTEM_ROLES),
]

ORG_WORKFLOW_NAME = "Organization Workflow"
ORG_ADMIN_ROLES = (C.ROLE_ADMIN, "System Manager")

# (state, style). The first row is where a new organization enters.
ORG_WORKFLOW_STATES = [
	(C.ORG_STATUS_IN_REVIEW, "Warning"),
	(C.ORG_STATUS_ACTIVE, "Success"),
	(C.ORG_STATUS_SUSPENDED, "Danger"),
]

# (from, action, to, roles that may take it). Request Re-verification is the system's: it is
# made when a representative changes what an administrator verified.
ORG_WORKFLOW_TRANSITIONS = [
	(C.ORG_STATUS_IN_REVIEW, "Verify", C.ORG_STATUS_ACTIVE, ORG_ADMIN_ROLES),
	(C.ORG_STATUS_IN_REVIEW, "Reject", C.ORG_STATUS_SUSPENDED, ORG_ADMIN_ROLES),
	(C.ORG_STATUS_ACTIVE, "Suspend", C.ORG_STATUS_SUSPENDED, ORG_ADMIN_ROLES),
	(C.ORG_STATUS_SUSPENDED, "Reinstate", C.ORG_STATUS_ACTIVE, ORG_ADMIN_ROLES),
	(C.ORG_STATUS_SUSPENDED, "Reopen Review", C.ORG_STATUS_IN_REVIEW, ORG_ADMIN_ROLES),
	(C.ORG_STATUS_ACTIVE, "Request Re-verification", C.ORG_STATUS_IN_REVIEW, SYSTEM_ROLES),
]

# The escalation rungs of §10.1, as Grievance Role Level master records.
#
# §10.1 retired `L1 Nodal Officer`, `L2 Senior Nodal Officer` and `Department Head` as
# *roles* - seniority is not a capability - but the rungs themselves remain, because
# escalation and remand still have to name where a case is going. They are three.
#
# The older lists in database-schema.md §57 / §245 and verifier_management_schema_and_flow.md
# §93 also carried `l1_case_officer` and `l2_supervisor`. Those are not rungs of this
# organisation - in §10.1 the L1 / L2 prefixes qualify Nodal Officer, they do not name
# separate case-officer and supervisor tiers. They are dropped here.
#
# Orders run in tens so a rung can be inserted between two others without renumbering.
#
# `escalation_hours` is how long a breached case may rest at that rung before it is
# handed upward. It is not the SLA: `sla_days` gets a case to its first escalation, and
# these govern the climb after that, which is why they shorten going up. The department
# head has none, which is what makes that rung terminal.
ROLE_LEVELS = [
	("nodal_officer", "Kebele Nodal Officer", 10, 48, "L1. Kebele-level coordination point for grievances."),
	(
		"senior_nodal_officer",
		"Woreda Nodal Officer",
		20,
		24,
		"L2. Woreda-level coordination; first rung above the Kebele nodal officer.",
	),
	(
		"zonal_head",
		"Zonal Head",
		25,
		24,
		"L3. Zonal-level coordination and escalation point.",
	),
	(
		"department_head",
		"Regional Bureau Head",
		30,
		0,
		"L4. Final internal escalation rung for the region/department.",
	),
]

# The level a Reviewer's rows carry on a department's desks. A desk row needs a level, but a
# Reviewer is read-only oversight and not a step of the chain, so it is inactive: `get_chain`
# and `top_rung` read active levels only. Routing and the officer picker skip these rows.
REVIEWER_ROLE_LEVEL = (
	"review_officer",
	"Review Officer",
	910,
	"Read-only oversight of a department. Not a step of the escalation chain.",
)

# The service category the app owns: the catch-all that routing falls back to, and the one the unrouted desk
# is scoped to. Every other category comes from the seed data (openg2p-data), not from the app. The code is the
# 3-character CATEGORY segment of the ticket number (services.ticket_number). Three characters allow 32,768
# categories, so a service can later be split into much finer groups without the codes running out. A retired
# code is never reused. The catch-all is `SYS`, as in the Routing Matrix.
SERVICE_CATEGORIES = [
	(C.FALLBACK_SERVICE_CATEGORY, C.FALLBACK_CATEGORY_CODE, 20),
]

# Baseline SLA days for standard service categories, as their base SLA rules.
# Seeded on install and migrate so categories have operational SLA windows out of the box.
DEFAULT_BASE_SLA_DAYS = [
	("Inputs", 14),
	("Schemes", 14),
	("Payments", 7),
	("Credit", 14),
	("Markets", 10),
	(C.FALLBACK_SERVICE_CATEGORY, 7),
]

# The 1-character REGION segment of the ticket number, keyed by the official
# administrative code carried in the seeded registry data. Assigned in
# ascending order of that code so the mapping is derived rather than chosen.
# Eighteen characters remain spare for future regions; a retired character is
# never reassigned, or tickets issued years earlier would start pointing at the
# wrong region.
REGION_TICKET_CODES = [
	("ET01", "0"),  # Tigray
	("ET02", "1"),  # Afar
	("ET03", "2"),  # Amhara
	("ET04", "3"),  # Oromia
	("ET05", "4"),  # Somali
	("ET06", "5"),  # Benishangul-Gumuz
	("ET07", "6"),  # Central Ethiopia
	("ET08", "7"),  # South Ethiopian
	("ET11", "8"),  # South West Ethiopia
	("ET12", "9"),  # Gambela
	("ET13", "A"),  # Harari
	("ET14", "B"),  # Addis Ababa
	("ET15", "C"),  # Dire Dawa
	("ET16", "D"),  # Sidama
]

# Standard Grievance Types mapped to Service Categories
GRIEVANCE_TYPES = [
	("Fertilizer Shortage", "Inputs"),
	("Seed Quality Issue", "Inputs"),
	("Pesticide Availability", "Inputs"),
	("Direct Benefit Transfer Delay", "Schemes"),
	("Subsidy Allocation Issue", "Schemes"),
	("Payment Failure", "Payments"),
	("Delayed Payment Settlement", "Payments"),
	("Loan Disbursement Delay", "Credit"),
	("Interest Rate Discrepancy", "Credit"),
	("Price Reporting Dispute", "Markets"),
	("Market Access Obstruction", "Markets"),
	(C.FALLBACK_GRIEVANCE_TYPE, C.FALLBACK_SERVICE_CATEGORY),
]

# Submitter Types master
SUBMITTER_TYPES = [
	("Individual Farmer", "IND"),
	("Development Agent", "DA"),
	("Cooperative", "COOP"),
	("FPO", "FPO"),
	("NGO", "NGO"),
	("Woreda/Kebele Body", "BODY"),
]

# Submission Types / Channels master
SUBMISSION_TYPES = [
	("Mobile App", "APP"),
	("Web Portal", "WEB"),
	("Mobile Call", "CALL"),
	("IVR Helpline", "IVR"),
	("Development Agent Assisted", "DA"),
]

# Starting wording for each officer action, global (no department or category scope).
# Seeded once; administrators edit, scope or deactivate them from there. The body is a
# Jinja template rendered into the action's reason; see grievance_response_template.py
# for the variables in scope.
#
# (title, workflow_action, body)
RESPONSE_TEMPLATES = [
	(
		"Resolved - standard",
		"Resolve",
		(
			"Action Taken:\n"
			+ "{{ department or 'The department' }} reviewed your grievance {{ ticket_number }} "
			+ "on {{ service_category }} and took the following action:\n\n"
			+ "Resolution Summary:\n"
			+ "Dear {{ submitter_name }}, your grievance has been resolved. If you are not satisfied "
			+ "with the outcome, you can reopen it from the grievance page.\n\n"
			+ "{{ officer_name }}, {{ today }}"
		),
	),
	(
		"Request more information - standard",
		"Request More Info",
		(
			"Dear {{ submitter_name }}, to continue working on your grievance {{ ticket_number }} "
			+ "we need the following information:\n\n"
			+ "Please reply from the grievance page. Your case will wait for your reply.\n\n"
			+ "{{ officer_name }}, {{ today }}"
		),
	),
	(
		"Referred - standard",
		"Refer Onward",
		(
			"Dear {{ submitter_name }}, your grievance {{ ticket_number }} has been referred to the "
			+ "department responsible for it, because:\n\n"
			+ "You will be informed once an officer there takes it up.\n\n"
			+ "{{ officer_name }}, {{ today }}"
		),
	),
]

# The complete notification matrix.
#
# Each row becomes one core Notification record per channel, so an administrator can
# change the wording, channel, recipient or enable state from the desk without a
# release. An "SMS + Email" event is two records here, because core's
# channel is a single Select.
#
# Message bodies are wrapped in _() so the same record serves both languages: the
# send path renders once per recipient inside print_language(), and the Amharic comes
# from core Translation records, which are themselves desk-editable. The context key
# pins the lookup, so rewording the English does not silently orphan the Amharic.
#
# (event_code, title, recipient_role, channels, trigger, source_text, format_args)
NOTIFICATION_EVENTS = [
	(
		C.EVENT_SUBMISSION_RECEIVED,
		"Submission Received",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Immediately on save",
		# The SLA clock starts at assignment, so a case that no routing rule matched
		# has no due date yet. Without the fallback the message read "Expected
		# response by None" -- seen in a real send on the dev bench.
		{
			"text": "Your grievance {0} has been received under {1}.",
			"args": ("doc.ticket_number", "doc.service_category"),
			"if": "doc.sla_due_date",
			"then": ("Expected response by {0}.", (PLACEHOLDER_EXPRESSIONS["sla_due_date"],)),
			"else": ("You will be informed once an officer is assigned.", ()),
		},
		(),
	),
	# The link and PIN of a sensitive anonymous case (STG-518). The record decides only whether a
	# channel is on: the text, which carries the link and PIN, is built by services/guest_access.py
	# and the template below is never sent, as no template is for a sensitive case's submitter.
	(
		C.EVENT_GUEST_ACCESS_ISSUED,
		"Grievance Access Link",
		"Submitter",
		("SMS", "Email"),
		"Sensitive anonymous case filed",
		"Grievance {0}: link and PIN to view it.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_GUEST_ACCESS_BACKFILL,
		"Grievance Access Link (Existing Case)",
		"Submitter",
		("SMS", "Email"),
		"One-time backfill for cases filed before the access link",
		"Grievance {0}: link and PIN to view it.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_DUPLICATE_DETECTED,
		"Duplicate Detected",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"On validation",
		"A similar grievance already exists. Reference {0}. You may link to it or proceed with justification.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_ASSIGNED_AUTO,
		"Grievance Assigned (Auto)",
		"Department Officer",
		("Email", "System Notification"),
		"On auto-routing match",
		"Grievance {0} ({1} / {2}) has been assigned to {3}. SLA deadline {4}.",
		(
			"doc.ticket_number",
			"doc.service_category",
			"doc.grievance_type",
			PLACEHOLDER_EXPRESSIONS["assigned_to"],
			PLACEHOLDER_EXPRESSIONS["sla_due_date"],
		),
	),
	(
		C.EVENT_ASSIGNED_MANUAL,
		"Grievance Assigned (Manual)",
		"Department Officer",
		("Email", "System Notification"),
		"On nodal officer assignment",
		"Grievance {0} has been assigned to {1} by the nodal officer. SLA deadline {2}.",
		(
			"doc.ticket_number",
			PLACEHOLDER_EXPRESSIONS["assigned_to"],
			PLACEHOLDER_EXPRESSIONS["sla_due_date"],
		),
	),
	(
		C.EVENT_STATUS_IN_PROGRESS,
		"Grievance Under Investigation",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Officer starts work",
		"Your grievance {0} is now under investigation.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_CONFIRMED,
		"Grievance Confirmed",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Submitter confirms resolution",
		"Grievance {0} confirmed resolved. Thank you for your feedback. Please rate your satisfaction from 1 to 5.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_AUTO_CLOSED,
		"Grievance Auto-Closed",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Confirmation window expires",
		"Grievance {0} has been closed as no response was received within the confirmation window. Please rate your satisfaction from 1 to 5.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_CLOSED,
		"Grievance Closed",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Grievance closed",
		"Grievance {0} is now closed. Thank you for your feedback. Please rate your satisfaction from 1 to 5.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_STATUS_REJECTED,
		"Grievance Rejected",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Officer rejects grievance",
		"Your grievance {0} was not accepted.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_MORE_INFO_REQUESTED,
		"More Information Requested",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Officer sets More Info Needed",
		# Without a state timer on the category there is no deadline to name.
		{
			"text": "Additional information is needed for grievance {0}. Please respond via the portal.",
			"args": ("doc.ticket_number",),
			"if": "doc.state_deadline",
			"then": (
				"Please reply within {0} days, or the case will be closed.",
				(PLACEHOLDER_EXPRESSIONS["response_days"],),
			),
			"else": ("", ()),
		},
		(),
	),
	(
		C.EVENT_ANONYMITY_DISCLOSURE_REQUESTED,
		"Anonymity Not Approved",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Officer declines an anonymity request",
		"Your request to stay anonymous on grievance {0} was not approved. Your identity has not been shared. Please choose in the portal whether to continue with your identity disclosed or withdraw the grievance.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_DEADLINE_EXTENDED,
		"Deadline Extended",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Approved SLA deferral",
		"The expected response date for your grievance {0} has been moved to {1}.",
		("doc.ticket_number", PLACEHOLDER_EXPRESSIONS["sla_due_date"]),
	),
	(
		C.EVENT_SUBMITTER_RESPONDED,
		"Submitter Responded",
		"Department Officer",
		("Email", "System Notification"),
		"Submitter provides info",
		"The submitter has responded on grievance {0}.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_RESPONSE_SENT,
		"Structured Response Sent",
		"Submitter",
		("SMS", "Email", "System Notification"),
		"Officer submits response",
		"A response has been issued on grievance {0}. Please confirm or reopen within the confirmation window.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_CONFIRMATION_WINDOW,
		"Confirmation Window Open",
		"Submitter",
		("SMS", "System Notification"),
		"Response submitted",
		"Grievance {0} is awaiting your confirmation. Confirm or reopen before the window closes.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_REOPENED,
		"Grievance Reopened",
		"Department Officer",
		("Email", "System Notification"),
		"Submitter reopens",
		# A reopen starts a fresh SLA window, so the officer is told the new deadline.
		{
			"text": "Grievance {0} has been reopened by the submitter and needs attention.",
			"args": ("doc.ticket_number",),
			"if": "doc.sla_due_date",
			"then": ("The new deadline is {0}.", (PLACEHOLDER_EXPRESSIONS["sla_due_date"],)),
			"else": ("", ()),
		},
		(),
	),
	(
		# N8 of the Routing Matrix: who looks at the case again, and the new expected date. The
		# office, never the officer. A sensitive case gets only its reference and PIN
		# (`notifications.sensitive_submitter_text`).
		C.EVENT_REOPENED,
		"Grievance Reopened for Submitter",
		"Submitter",
		("SMS",),
		"Submitter reopens",
		{
			"text": "Your grievance {0} has been reopened and will be examined again by {1}.",
			"args": ("doc.ticket_number", PLACEHOLDER_EXPRESSIONS["department"]),
			"if": "doc.sla_due_date",
			"then": ("Expected response by {0}.", (PLACEHOLDER_EXPRESSIONS["sla_due_date"],)),
			"else": ("", ()),
		},
		(),
	),
	(
		C.EVENT_SLA_REMINDER_20,
		"SLA Reminder 20%",
		"Assigned Officer",
		("Email", "System Notification"),
		"Scheduled job at 20% SLA elapsed",
		"Grievance {0} has reached 20% of its SLA window. Deadline {1}.",
		("doc.ticket_number", PLACEHOLDER_EXPRESSIONS["sla_due_date"]),
	),
	(
		C.EVENT_SLA_REMINDER_50,
		"SLA Reminder 50%",
		"Assigned Officer",
		("Email", "System Notification"),
		"Scheduled job at 50% SLA elapsed",
		"Grievance {0} has reached 50% of its SLA window. Deadline {1}.",
		("doc.ticket_number", PLACEHOLDER_EXPRESSIONS["sla_due_date"]),
	),
	(
		C.EVENT_SLA_REMINDER_80,
		"SLA Reminder 80%",
		"Assigned Officer",
		("Email", "System Notification"),
		"Scheduled job at 80% SLA elapsed",
		"Grievance {0} has reached 80% of its SLA window. Urgent action required.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_SLA_AT_RISK,
		"SLA At-risk Report",
		"Nodal Officer",
		("Email", "System Notification"),
		"Scheduled job at 80%",
		"Grievance {0} is approaching its SLA deadline {1}.",
		("doc.ticket_number", PLACEHOLDER_EXPRESSIONS["sla_due_date"]),
	),
	(
		C.EVENT_SLA_BREACH,
		"SLA Breached",
		"Department Head",
		("Email", "System Notification"),
		"SLA deadline passed",
		"Grievance {0} has breached its SLA and has been escalated. Immediate action is required.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_MANUAL_ESCALATION,
		"Manual Escalation",
		"Department Head",
		("Email", "System Notification"),
		# Sent to the officer the case is handed to (`sla.escalate` names them).
		"Officer escalates to the next level",
		"Grievance {0} has been escalated to you and needs attention.",
		("doc.ticket_number",),
	),
	(
		C.EVENT_REASSIGNMENT_REQUESTED,
		"Reassignment Requested",
		"Nodal Officer",
		("Email", "System Notification"),
		"Officer requests reassign",
		"A reassignment has been requested on grievance {0} and needs approval.",
		("doc.ticket_number",),
	),
]


def after_install():
	seed_all()
	# Install only: on migrate an administrator may have removed the defaults on purpose.
	seed_state_timers()
	ensure_ivr_service_user()


def after_migrate():
	seed_all()
	ensure_ivr_service_user()


def seed_all():
	created = {
		"roles": seed_roles(),
		"sla_category_field": seed_sla_category_field(),
		"workflow": seed_workflow(),
		"organization_workflow": seed_organization_workflow(),
		"role_levels": seed_role_levels(),
		"categories": seed_categories(),
		"unrouted_desk": seed_unrouted_desk(),
		"grievance_types": seed_grievance_types(),
		"submitter_types": seed_submitter_types(),
		"submission_types": seed_submission_types(),
		"service_provider_types": seed_service_provider_types(),
		"service_provider_sources": seed_service_provider_sources(),
		"response_templates": seed_response_templates(),
		"notification_recipient_field": seed_recipient_custom_field(),
		"notifications": seed_notifications(),
		"administrative_areas": seed_administrative_areas(),
		"region_ticket_codes": seed_region_ticket_codes(),
		"unlocated_area": seed_unlocated_area(),
		"holiday_list": seed_holiday_list(),
		"base_sla_rules": seed_base_sla_rules(),
	}
	# Explicit commit after running setup seed data in after_install/after_migrate hook
	frappe.db.commit()  # nosemgrep
	return created


def seed_holiday_list():
	"""Seed standard Ethiopian public holiday list if absent."""
	name = "Ethiopian Public Holidays"
	if frappe.db.exists("Grievance Holiday List", name):
		return []

	year = frappe.utils.now_datetime().year
	from_date = f"{year}-01-01"
	to_date = f"{year + 1}-12-31"

	doc = frappe.get_doc(
		{
			"doctype": "Grievance Holiday List",
			"holiday_list_name": name,
			"from_date": from_date,
			"to_date": to_date,
			"weekly_off": "Sunday",
			"is_default": 1,
		}
	)
	holidays = [
		(f"{year}-01-07", "Ethiopian Christmas (Genna)"),
		(f"{year}-01-19", "Timkat (Epiphany)"),
		(f"{year}-03-02", "Victory of Adwa"),
		(f"{year}-05-01", "International Labour Day"),
		(f"{year}-05-05", "Patriots' Victory Day"),
		(f"{year}-05-28", "Downfall of Derg"),
		(f"{year}-09-11", "Ethiopian New Year (Enkutatash)"),
		(f"{year}-09-27", "Meskel (Finding of True Cross)"),
		(f"{year + 1}-01-07", "Ethiopian Christmas (Genna)"),
		(f"{year + 1}-01-19", "Timkat (Epiphany)"),
		(f"{year + 1}-03-02", "Victory of Adwa"),
		(f"{year + 1}-05-01", "International Labour Day"),
		(f"{year + 1}-05-05", "Patriots' Victory Day"),
		(f"{year + 1}-05-28", "Downfall of Derg"),
		(f"{year + 1}-09-11", "Ethiopian New Year (Enkutatash)"),
		(f"{year + 1}-09-27", "Meskel (Finding of True Cross)"),
	]
	for h_date, desc in holidays:
		doc.append("holidays", {"holiday_date": h_date, "description": desc, "weekly_off": 0})
	doc.populate_weekly_offs()
	doc.insert(ignore_permissions=True)
	return [name]


def seed_base_sla_rules():
	"""Seed the base SLA rule of each standard service category that has none."""
	made = []
	for category, sla_days in DEFAULT_BASE_SLA_DAYS:
		if not frappe.db.exists("Grievance Service Category", category):
			continue
		if frappe.db.exists("Grievance SLA Rule", {"service_category": category, "is_base": 1}):
			continue
		classification.set_base_days(category, sla_days)
		made.append(category)
	return made


def seed_response_templates():
	made = []
	for title, workflow_action, body in RESPONSE_TEMPLATES:
		if frappe.db.exists("Grievance Response Template", {"title": title}):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Grievance Response Template",
				"title": title,
				"workflow_action": workflow_action,
				"body": body,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(doc.name)
	return made


def seed_grievance_types():
	made = []
	for name, category in GRIEVANCE_TYPES:
		if not frappe.db.exists("Grievance Service Category", category):
			continue
		is_catch_all = name == C.FALLBACK_GRIEVANCE_TYPE and category == C.FALLBACK_SERVICE_CATEGORY
		if is_catch_all:
			if frappe.db.exists("Grievance Type", C.FALLBACK_TYPE_CODE):
				if not frappe.db.get_value("Grievance Type", C.FALLBACK_TYPE_CODE, "type_code"):
					frappe.db.set_value(
						"Grievance Type", C.FALLBACK_TYPE_CODE, "type_code", C.FALLBACK_TYPE_CODE
					)
				continue
			existing_by_tuple = frappe.db.get_value(
				"Grievance Type",
				{"service_category": category, "type_name": name},
				["name", "type_code"],
				as_dict=True,
			)
			if existing_by_tuple:
				if existing_by_tuple.name != C.FALLBACK_TYPE_CODE:
					frappe.rename_doc(
						"Grievance Type", existing_by_tuple.name, C.FALLBACK_TYPE_CODE, force=True
					)
				if existing_by_tuple.type_code != C.FALLBACK_TYPE_CODE:
					frappe.db.set_value(
						"Grievance Type", C.FALLBACK_TYPE_CODE, "type_code", C.FALLBACK_TYPE_CODE
					)
				continue
			row = {
				"doctype": "Grievance Type",
				"name": C.FALLBACK_TYPE_CODE,
				"type_name": name,
				"service_category": category,
				"type_code": C.FALLBACK_TYPE_CODE,
				"is_active": 1,
			}
			frappe.get_doc(row).insert(ignore_permissions=True, set_name=C.FALLBACK_TYPE_CODE)
			made.append(name)
			continue

		if frappe.db.exists("Grievance Type", {"type_name": name, "service_category": category}):
			continue
		row = {"doctype": "Grievance Type", "type_name": name, "service_category": category, "is_active": 1}
		frappe.get_doc(row).insert(ignore_permissions=True, set_name=row.get("type_code"))
		made.append(name)
	return made


def seed_administrative_areas():
	"""Seed pre-calculated Ethiopian administrative area tree (21,028 nodes) from the JSON seed if empty."""
	if frappe.db.count("Grievance Administrative Area") > 0:
		return 0

	seed_path = os.path.join(os.path.dirname(__file__), "data", "ethiopia_administrative_areas.json.gz")
	if not os.path.exists(seed_path):
		return 0

	with gzip.open(seed_path, "rt", encoding="utf-8") as f:
		seed = json.load(f)

	frappe.db.bulk_insert(
		"Grievance Administrative Area", seed["columns"], seed["rows"], ignore_duplicates=True
	)

	return frappe.db.count("Grievance Administrative Area")


def seed_region_ticket_codes():
	"""Give each region its REGION character for the ticket number.

	Runs after the area tree so the rows exist. Matched on the official
	administrative code rather than the area name, because names are translated
	and occasionally renamed while the codes are stable. Only regions carry a
	ticket code; no other level appears in a ticket number.
	"""
	made = []
	for official_code, ticket_code in REGION_TICKET_CODES:
		name = frappe.db.get_value(
			"Grievance Administrative Area",
			{"code": official_code, "level_name": "Region"},
			"name",
		)
		if not name:
			continue
		if frappe.db.get_value("Grievance Administrative Area", name, "ticket_code") == ticket_code:
			continue
		frappe.db.set_value("Grievance Administrative Area", name, "ticket_code", ticket_code)
		made.append(f"{official_code}={ticket_code}")
	return made


def seed_unlocated_area():
	"""The row that stands for the ticket character of a case filed with no area.

	A top-level "Other" node, so it never appears among the Regions an area picker lists.
	Runs after the tree seed, which is skipped once any area exists.
	"""
	if frappe.db.exists("Grievance Administrative Area", {"code": ticket_number.UNLOCATED_AREA_CODE}):
		return []
	frappe.get_doc(
		{
			"doctype": "Grievance Administrative Area",
			"area_name": "Unlocated",
			"code": ticket_number.UNLOCATED_AREA_CODE,
			"level_name": "Other",
			"ticket_code": ticket_number.UNLOCATED_REGION,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)
	return [ticket_number.UNLOCATED_AREA_CODE]


def seed_roles():
	made = []
	for role, desk_access in ROLES:
		if frappe.db.exists("Role", role):
			continue
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": desk_access}).insert(
			ignore_permissions=True
		)
		made.append(role)
	return made


def seed_workflow():
	"""The Grievance Workflow, rebuilt from WORKFLOW_STATES and WORKFLOW_TRANSITIONS.

	Rebuilt rather than created-if-missing because the tables are the source of
	truth: a row removed from them must disappear from the engine too.
	"""
	seed_roles()
	for state, style, _sla in WORKFLOW_STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state, "style": style}
			).insert(ignore_permissions=True)

	for _from_state, action, _to_state, _roles in WORKFLOW_TRANSITIONS:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
				ignore_permissions=True
			)

	# One workflow on Grievance, this one. Anything else -- an earlier name, a
	# desk experiment -- would sit inactive beside it and confuse the next reader.
	for other in frappe.get_all(
		"Workflow", filters={"document_type": "Grievance", "name": ["!=", WORKFLOW_NAME]}, pluck="name"
	):
		frappe.delete_doc("Workflow", other, force=True, ignore_permissions=True)

	if frappe.db.exists("Workflow", WORKFLOW_NAME):
		workflow = frappe.get_doc("Workflow", WORKFLOW_NAME)
		workflow.set("states", [])
		workflow.set("transitions", [])
	else:
		workflow = frappe.new_doc("Workflow")
		workflow.workflow_name = WORKFLOW_NAME

	workflow.update(
		{
			"document_type": "Grievance",
			"workflow_state_field": "workflow_state",
			"is_active": 1,
			"send_email_alert": 0,
		}
	)

	# One state row per editing role: the desk makes the form read-only for anyone
	# whose roles match none of a state's allow_edit rows.
	for state, _style, sla_category in WORKFLOW_STATES:
		for role in OFFICER_ROLES:
			workflow.append(
				"states",
				{"state": state, "doc_status": "0", "allow_edit": role, "sla_category": sla_category},
			)

	for from_state, action, to_state, roles in WORKFLOW_TRANSITIONS:
		for role in roles:
			workflow.append(
				"transitions",
				{
					"state": from_state,
					"action": action,
					"next_state": to_state,
					"allowed": role,
					"allow_self_approval": 1,
				},
			)

	workflow.save(ignore_permissions=True)
	return workflow.name


def seed_organization_workflow():
	"""The Organization Workflow, rebuilt from ORG_WORKFLOW_STATES and ORG_WORKFLOW_TRANSITIONS."""
	for state, style in ORG_WORKFLOW_STATES:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state, "style": style}
			).insert(ignore_permissions=True)

	for _from_state, action, _to_state, _roles in ORG_WORKFLOW_TRANSITIONS:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({"doctype": "Workflow Action Master", "workflow_action_name": action}).insert(
				ignore_permissions=True
			)

	if frappe.db.exists("Workflow", ORG_WORKFLOW_NAME):
		workflow = frappe.get_doc("Workflow", ORG_WORKFLOW_NAME)
	else:
		workflow = frappe.new_doc("Workflow")
		workflow.workflow_name = ORG_WORKFLOW_NAME
	workflow.update(
		{
			"document_type": "Grievance Organization",
			"workflow_state_field": "workflow_state",
			"is_active": 1,
			"send_email_alert": 0,
		}
	)
	workflow.set("states", [])
	workflow.set("transitions", [])

	for state, _style in ORG_WORKFLOW_STATES:
		for role in ORG_ADMIN_ROLES:
			workflow.append("states", {"state": state, "doc_status": "0", "allow_edit": role})
	for from_state, action, to_state, roles in ORG_WORKFLOW_TRANSITIONS:
		for role in roles:
			workflow.append(
				"transitions",
				{
					"state": from_state,
					"action": action,
					"next_state": to_state,
					"allowed": role,
					"allow_self_approval": 1,
				},
			)

	workflow.save(ignore_permissions=True)
	return workflow.name


def seed_sla_category_field():
	"""What each state of the Grievance Workflow does to the SLA clock.

	On Workflow Document State - the workflow's own state rows - rather than on
	Workflow State, which is a global master: "Rejected" there is shared
	with every other workflow on the site, and this is a fact about this one.
	"""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	if frappe.db.exists("Custom Field", "Workflow Document State-sla_category"):
		return []

	create_custom_field(
		"Workflow Document State",
		{
			"fieldname": "sla_category",
			"label": "SLA Category",
			"fieldtype": "Select",
			"options": "\nRunning\nPaused\nStopped",
			"insert_after": "doc_status",
			"in_list_view": 1,
			"description": "Running keeps the SLA clock going, Paused holds it, Stopped ends it.",
		},
	)
	return ["Workflow Document State-sla_category"]


def seed_role_levels():
	made = []
	for code, name, order, escalation_hours, description in ROLE_LEVELS:
		if frappe.db.exists("Grievance Role Level", code):
			continue
		frappe.get_doc(
			{
				"doctype": "Grievance Role Level",
				"level_code": code,
				"level_name": name,
				"level_order": order,
				"escalation_hours": escalation_hours,
				"description": description,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(code)
	code, name, order, description = REVIEWER_ROLE_LEVEL
	if not frappe.db.exists("Grievance Role Level", code):
		frappe.get_doc(
			{
				"doctype": "Grievance Role Level",
				"level_code": code,
				"level_name": name,
				"level_order": order,
				"description": description,
				"is_active": 0,
			}
		).insert(ignore_permissions=True)
		made.append(code)
	return made


def seed_categories():
	"""Seed the service categories if they are absent."""
	made = []
	for name, code, order in SERVICE_CATEGORIES:
		# An admin may have renamed a seeded category; its code still marks it as seeded.
		if frappe.db.exists("Grievance Service Category", name) or frappe.db.exists(
			"Grievance Service Category", {"code": code}
		):
			if name == C.FALLBACK_SERVICE_CATEGORY and frappe.db.exists("Grievance Service Category", name):
				current_code = frappe.db.get_value("Grievance Service Category", name, "code")
				if current_code != code and not frappe.db.exists(
					"Grievance Service Category", {"code": code}
				):
					frappe.db.set_value(
						"Grievance Service Category", name, "code", code, update_modified=False
					)
			continue
		frappe.get_doc(
			{
				"doctype": "Grievance Service Category",
				"category_name": name,
				"code": code,
				"sort_order": order,
				"is_active": 1,
				# The catch-all is the default until an administrator names another.
				"is_default": 1 if name == C.FALLBACK_SERVICE_CATEGORY else 0,
			}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


def seed_unrouted_desk():
	"""Seed the desk that takes a case no other desk can."""
	made = []
	if not frappe.db.exists("Grievance Department", C.UNROUTED_DEPARTMENT):
		frappe.get_doc({"doctype": "Grievance Department", "dept_name": C.UNROUTED_DEPARTMENT}).insert(
			ignore_permissions=True
		)
		made.append("department")
	if not frappe.db.exists("User", C.UNROUTED_OFFICER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": C.UNROUTED_OFFICER,
				"first_name": "Unrouted",
				"last_name": "Officer",
				"send_welcome_email": 0,
				"enabled": 1,
				"roles": [{"role": "Grievance Officer"}, {"role": C.ROLE_DESK_HEAD}],
			}
		).insert(ignore_permissions=True)
		made.append("officer")

	if not frappe.db.exists("Grievance RBAC Assignment", {"department_scope": C.UNROUTED_DEPARTMENT}):
		frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": C.UNROUTED_DEPARTMENT,
				"category_scope": C.FALLBACK_SERVICE_CATEGORY,
				"reassignment_requires_approval": 0,
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": C.UNROUTED_OFFICER,
						"role_level": "department_head",
						"designation": "other officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)
		made.append("desk")
	return made


def seed_submitter_types():
	made = []
	for name, code in SUBMITTER_TYPES:
		if frappe.db.exists("Grievance Submitter Type", name) or frappe.db.exists(
			"Grievance Submitter Type", {"code": code}
		):
			continue
		frappe.get_doc(
			{
				"doctype": "Grievance Submitter Type",
				"type_name": name,
				"code": code,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


def seed_submission_types():
	made = []
	for name, code in SUBMISSION_TYPES:
		if frappe.db.exists("Grievance Submission Type", name) or frappe.db.exists(
			"Grievance Submission Type", {"code": code}
		):
			continue
		frappe.get_doc(
			{
				"doctype": "Grievance Submission Type",
				"submission_type_name": name,
				"code": code,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


SERVICE_PROVIDER_TYPES = [
	"Bank",
	"Microfinance",
	"Cooperative",
	"Cooperative Union",
	"Input Store",
	"Market",
	"Government Office",
	"Other",
]

A2C_BANKS_SOURCE = "A2C Banks"


def seed_service_provider_types():
	"""The first set of provider types. Administrators can add more; existing ones are left alone."""
	made = []
	for name in SERVICE_PROVIDER_TYPES:
		if frappe.db.exists("Grievance Service Provider Type", name):
			continue
		frappe.get_doc(
			{"doctype": "Grievance Service Provider Type", "type_name": name, "is_active": 1}
		).insert(ignore_permissions=True)
		made.append(name)
	return made


def seed_service_provider_sources():
	"""The A2C bank catalog as a source, disabled and blank until an operator completes it.

	It carries the shape of the A2C catalog (login path, list path, field names). A source that
	already exists is left alone: it holds the site's own choice.
	"""
	if frappe.db.exists("Grievance Service Provider Source", A2C_BANKS_SOURCE):
		return []
	frappe.get_doc(
		{
			"doctype": "Grievance Service Provider Source",
			"source_name": A2C_BANKS_SOURCE,
			"source_type": "API",
			"is_enabled": 0,
			"auth_mode": "Login",
			"login_path": "/v1/auth/login",
			"list_path": "/v1/catalog/facets",
			"list_key": "banks",
			"map_external_id": "name",
			"map_name": "bank_name",
			"map_area": "registered_region",
			"default_provider_type": "Bank",
		}
	).insert(ignore_permissions=True)
	return [A2C_BANKS_SOURCE]


def seed_recipient_custom_field():
	"""The recipient type and dynamic role level, as fields on core Notification."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	from oan_grievance_service.services.notifications import RECIPIENT_ROLES, RECIPIENT_TYPES

	created = []
	if not frappe.db.exists("Custom Field", "Notification-grievance_recipient_type"):
		create_custom_field(
			"Notification",
			{
				"fieldname": "grievance_recipient_type",
				"label": "Grievance Recipient Type",
				"fieldtype": "Select",
				"options": "\n".join(("", *RECIPIENT_TYPES)),
				"insert_after": "document_type",
				"depends_on": 'eval:doc.document_type=="Grievance"',
				"description": "Recipient classification (citizen, assigned officer, department mailbox, or organizational role level).",
			},
		)
		created.append("Notification-grievance_recipient_type")

	if not frappe.db.exists("Custom Field", "Notification-grievance_role_level"):
		create_custom_field(
			"Notification",
			{
				"fieldname": "grievance_role_level",
				"label": "Grievance Role Level",
				"fieldtype": "Link",
				"options": "Grievance Role Level",
				"insert_after": "grievance_recipient_type",
				"depends_on": 'eval:doc.document_type=="Grievance" && doc.grievance_recipient_type=="Role Level"',
				"description": "Dynamic escalation tier from Grievance Role Level.",
			},
		)
		created.append("Notification-grievance_role_level")

	if not frappe.db.exists("Custom Field", "Notification-grievance_recipient"):
		create_custom_field(
			"Notification",
			{
				"fieldname": "grievance_recipient",
				"label": "Grievance Recipient (Legacy)",
				"fieldtype": "Select",
				"options": "\n".join(("", *RECIPIENT_ROLES)),
				"insert_after": "grievance_role_level",
				"depends_on": 'eval:doc.document_type=="Grievance"',
				"description": "Legacy recipient role.",
			},
		)
		created.append("Notification-grievance_recipient")

	return created


def _translatable(source, args, context_key):
	"""Wrap an English source string as a translatable Jinja expression.

	The source string is the translation key, which is the one sharp edge of keeping
	both languages desk-editable from a single record: reword the English and the
	Amharic Translation stops matching. The context key is passed to _() so the lookup
	keys on something stable instead.
	"""
	if isinstance(source, dict):
		return _translatable_with_fallback(source, context_key)
	return _translatable_expr(source, args, context_key)


def _translatable_expr(source, args, context_key):
	call = f"_({source!r}, context={context_key!r})"
	if args:
		call += ".format(" + ", ".join(args) + ")"
	return "{{ " + call + " }}"


def _translatable_with_fallback(spec, context_key):
	"""A base sentence followed by one of two translatable sentences, chosen by a
	Jinja condition on the document.

	    {"text": ..., "args": (...), "if": "doc.field",
	     "then": (text, args), "else": (text, args)}

	Each sentence is its own _() call with its own context key, so each has its own
	Translation record. Only Jinja blocks and whitespace appear outside _(), which is
	what `validate_notification` requires.
	"""
	base = _translatable_expr(spec["text"], spec.get("args", ()), context_key)
	then_text, then_args = spec["then"]
	else_text, else_args = spec["else"]
	then_expr = _translatable_expr(then_text, then_args, f"{context_key}.then")
	else_expr = _translatable_expr(else_text, else_args, f"{context_key}.else")
	return f"{base} {{% if {spec['if']} %}}{then_expr}{{% else %}}{else_expr}{{% endif %}}"


def seed_notifications():
	"""One core Notification per (event, channel).

	Carried on core's "Method" trigger: the lifecycle fires these explicitly from the
	service layer rather than on Save or Value Change, so the event code is the method
	name and notifications.queue() looks them up by it.
	"""
	from oan_grievance_service.services.notifications import (
		RECIPIENT_ROLE_LEVEL,
		RECIPIENT_TYPES,
		ROLE_LEVEL_RECIPIENTS,
		template_context,
	)

	made = []
	for code, title, recipient, channels, _trigger, source, args in NOTIFICATION_EVENTS:
		if recipient in ROLE_LEVEL_RECIPIENTS:
			rec_type = RECIPIENT_ROLE_LEVEL
			rec_role_level = ROLE_LEVEL_RECIPIENTS[recipient]
		elif recipient in RECIPIENT_TYPES:
			rec_type = recipient
			rec_role_level = None
		else:
			rec_type = RECIPIENT_ROLE_LEVEL
			rec_role_level = recipient

		for channel in channels:
			name = f"Grievance: {title} ({channel})"
			if frappe.db.exists("Notification", name):
				doc = frappe.get_doc("Notification", name)
				needs_save = False
				if not doc.get("grievance_recipient_type"):
					doc.db_set("grievance_recipient_type", rec_type, update_modified=False)
					needs_save = True
				if rec_role_level and not doc.get("grievance_role_level"):
					doc.db_set("grievance_role_level", rec_role_level, update_modified=False)
					needs_save = True
				if needs_save:
					made.append(name)
				continue
			context_key = template_context(code, channel)
			frappe.get_doc(
				{
					"doctype": "Notification",
					"name": name,
					# Translatable like the body: the subject is rendered for email and
					# reused as the in-app title, so a literal here reaches the
					# recipient just as untranslated as a literal in the message.
					"subject": _translatable(title, (), f"{context_key}.subject"),
					"document_type": "Grievance",
					"event": "Method",
					"method": code,
					"channel": channel,
					"grievance_recipient": recipient,
					"grievance_recipient_type": rec_type,
					"grievance_role_level": rec_role_level,
					"message": _translatable(source, args, context_key),
					"message_type": "Plain Text",
					# Deliberately not is_standard. A standard Notification loads its
					# template from a file in the module folder and validate_standard()
					# refuses edits outside developer mode, which is the opposite of the
					# desk-editable wording we need.
					"is_standard": 0,
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
			made.append(name)
	return made
