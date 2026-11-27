# The catch-all category and type an intake falls back to when the submitter does
# not pick one. Seeded by setup/install.py; intake defaults to them by name.
FALLBACK_SERVICE_CATEGORY = "Other"
FALLBACK_GRIEVANCE_TYPE = "Other"
# Codes of the catch-all in the Routing Matrix (STG-512). The category code is the CATEGORY segment of
# the ticket number; the type code is the key the seed matches the type on. Both are SYS by convention.
FALLBACK_CATEGORY_CODE = "SYS"
FALLBACK_TYPE_CODE = "SYS"
UNROUTED_DEPARTMENT = "Unrouted"
UNROUTED_OFFICER = "unrouted@grievance.local"


# Dedicated RQ queues, so a ClamAV backlog or a long SLA sweep cannot starve the
# others. Each must be declared under "workers" in common_site_config.json, or
# frappe.enqueue rejects it.
QUEUE_ROUTING = "grievance_routing"
# Frappe's built-in short queue: flagging runs in seconds and needs no worker of its own.
QUEUE_DUPLICATES = "short"
QUEUE_SLA = "grievance_sla"
QUEUE_SCAN = "grievance_scan"
QUEUE_NOTIFICATIONS = "grievance_notifications"


# Grievance Workflow states, seeded by setup/install.py (WORKFLOW_STATES). Code that
# branches on where a case is compares against these, never a bare string.
STATE_SUBMITTED = "Submitted"
STATE_ASSIGNED = "Assigned"
STATE_REOPENED = "Reopened"
STATE_IN_PROGRESS = "In Progress"
STATE_MORE_INFO_NEEDED = "More Info Needed"
STATE_RESOLVED = "Resolved"
STATE_CLOSED = "Closed"
STATE_REJECTED = "Rejected"
# An officer-confirmed copy of another ticket. Terminal and read-only: the main ticket is where
# the case is worked (services/duplicates.py).
STATE_DUPLICATE = "Duplicate"


# Organisation (NGO / FPO / Cooperative) verification lifecycle. Held on Grievance
# Organization.verification_status; services/organization.py owns the legal moves.
ORG_STATUS_IN_REVIEW = "In Review"
ORG_STATUS_ACTIVE = "Active"
ORG_STATUS_SUSPENDED = "Suspended"
ORG_TYPES = ("NGO", "FPO", "Cooperative")


# The group the dashboards count as resolved: Resolved or Closed. The open and
# awaiting-action groups are not listed here; sla.open_states() and
# sla.awaiting_action_states() read them from the Grievance Workflow.
RESOLVED_STATES = (STATE_RESOLVED, STATE_CLOSED)

# How long a client may reuse an answer from a reference-data endpoint.
REFERENCE_CACHE_SECONDS = 300

# `level_name` of the Grievance Administrative Area levels the dashboards count by.
AREA_LEVEL_COUNTRY = "Country"
AREA_LEVEL_REGION = "Region"
AREA_LEVEL_WOREDA = "Woreda"


# Notification event codes. Each is the "method" on one core Notification record per
# channel, seeded by setup/install.py and editable from the desk thereafter.
EVENT_SUBMISSION_RECEIVED = "submission_received"
EVENT_DUPLICATE_DETECTED = "duplicate_detected"
EVENT_ASSIGNED_AUTO = "grievance_assigned_auto"
EVENT_ASSIGNED_MANUAL = "grievance_assigned_manual"
EVENT_STATUS_IN_PROGRESS = "status_in_progress"
EVENT_MORE_INFO_REQUESTED = "more_info_requested"
EVENT_SUBMITTER_RESPONDED = "submitter_responds_to_info"
EVENT_RESPONSE_SENT = "structured_response_sent"
EVENT_CONFIRMATION_WINDOW = "confirmation_window_open"
EVENT_CONFIRMED = "grievance_confirmed"
EVENT_REOPENED = "grievance_reopened"
EVENT_AUTO_CLOSED = "auto_closed_no_response"
EVENT_CLOSED = "grievance_closed"
EVENT_STATUS_REJECTED = "status_rejected"
EVENT_SLA_REMINDER_20 = "sla_reminder_20"
EVENT_SLA_REMINDER_50 = "sla_reminder_50"
EVENT_SLA_REMINDER_80 = "sla_reminder_80"
EVENT_SLA_AT_RISK = "sla_at_risk_report"
EVENT_SLA_BREACH = "sla_breached"
EVENT_MANUAL_ESCALATION = "manual_escalation"
EVENT_REASSIGNMENT_REQUESTED = "reassignment_requested"
EVENT_ANONYMITY_DISCLOSURE_REQUESTED = "anonymity_disclosure_requested"
EVENT_DEADLINE_EXTENDED = "deadline_extended"
# The link and PIN a sensitive anonymous case's submitter opens it with (STG-518), sent once at
# filing, or once by the backfill for a case filed before the feature. The text is built in
# services/guest_access.py, never from the Notification's template.
EVENT_GUEST_ACCESS_ISSUED = "guest_access_issued"
EVENT_GUEST_ACCESS_BACKFILL = "guest_access_backfill"
GUEST_ACCESS_EVENTS = frozenset({EVENT_GUEST_ACCESS_ISSUED, EVENT_GUEST_ACCESS_BACKFILL})

# FSD Appendix C event ids (EC-001 to EC-019), keyed by event code. EC-017 (SLA breach,
# L2) has no code of its own; the escalation ladder sends a single EVENT_SLA_BREACH for
# every level, which is EC-016's. EVENT_CLOSED, EVENT_STATUS_REJECTED,
# EVENT_ANONYMITY_DISCLOSURE_REQUESTED, EVENT_DEADLINE_EXTENDED and EVENT_SLA_REMINDER_20
# are not in Appendix C.
EVENT_EC_ID = {
	EVENT_SUBMISSION_RECEIVED: "EC-001",
	EVENT_DUPLICATE_DETECTED: "EC-002",
	EVENT_ASSIGNED_AUTO: "EC-003",
	EVENT_ASSIGNED_MANUAL: "EC-004",
	EVENT_STATUS_IN_PROGRESS: "EC-005",
	EVENT_MORE_INFO_REQUESTED: "EC-006",
	EVENT_SUBMITTER_RESPONDED: "EC-007",
	EVENT_RESPONSE_SENT: "EC-008",
	EVENT_CONFIRMATION_WINDOW: "EC-009",
	EVENT_CONFIRMED: "EC-010",
	EVENT_REOPENED: "EC-011",
	EVENT_AUTO_CLOSED: "EC-012",
	EVENT_SLA_REMINDER_50: "EC-013",
	EVENT_SLA_REMINDER_80: "EC-014",
	EVENT_SLA_AT_RISK: "EC-015",
	EVENT_SLA_BREACH: "EC-016",
	EVENT_MANUAL_ESCALATION: "EC-018",
	EVENT_REASSIGNMENT_REQUESTED: "EC-019",
}

# SLA window, in days, for a category created without one.
DEFAULT_SLA_DAYS = 7
# State timers the Grievance Deferral Policy starts with: (state, hours, on_expiry).
DEFAULT_STATE_TIMERS = (
	# The confirmation window: the submitter may reopen (appeal) a decision for 30 days (Routing
	# Matrix, "Appeal and reinvestigation"). Patch `lengthen_confirmation_window` moves sites that
	# kept the old 7-day default.
	(STATE_RESOLVED, 30 * 24, "Auto Close"),
	(STATE_MORE_INFO_NEEDED, 7 * 24, "Auto Close"),
)
# Reminder thresholds as a percentage of the SLA window, and the event each one sends.
SLA_REMINDER_EVENTS = {
	20: EVENT_SLA_REMINDER_20,
	50: EVENT_SLA_REMINDER_50,
	80: EVENT_SLA_REMINDER_80,
}
SLA_REMINDER_THRESHOLDS = tuple(sorted(SLA_REMINDER_EVENTS))
# The last reminder before the deadline also sends the at-risk report.
SLA_AT_RISK_THRESHOLD = max(SLA_REMINDER_THRESHOLDS)
# Grievance field holding the highest reminder threshold already sent in the current window
# (0 for none), so a new threshold needs no new column.
REMINDER_SENT_FIELD = "last_reminder_percent"
# Default ceiling on a single deferral, for a Grievance Deferral Policy never saved.
DEFAULT_MAX_DEFERRAL_DAYS = 30


# Canonical Frappe user roles used for API authentication and authorization.
ROLE_SUBMITTER = "Grievance Submitter"
ROLE_OFFICER = "Grievance Officer"
ROLE_ADMIN = "Grievance Admin"
# Granted and revoked by services/officer.py with the top rung; RBAC records decide the scope.
ROLE_DESK_HEAD = "Grievance Desk Head"
# An assisted submitter (e.g. Development Agent, call-centre operator, front-desk intake staff)
# who files grievances on behalf of citizens, recorded as assisted_by_officer.
# Granted via Keycloak.
ROLE_ASSISTED_SUBMITTER = "Grievance Assisted Submitter"
# The OAN dashboards: a machine user that authenticates with its Frappe API key and
# secret and may read the public charts only.
ROLE_DASHBOARD_READER = "Grievance Dashboard Reader"
# Oversight: reads grievances, officer statistics and the administration data, and
# changes nothing. It holds read-only DocPerms and is never listed on a mutating
# endpoint's require_role.
ROLE_REVIEW_OFFICER = "Grievance Review Officer"

# The submission channel (Grievance Submission Type) of the IVR helpline, and what the webhook
# files a call under when the platform sends no more than the caller's number.
CHANNEL_IVR = "IVR Helpline"
IVR_CALLER_NAME = "IVR Caller"
IVR_DEFAULT_DESCRIPTION = (
	"Grievance registered through the IVR helpline. The caller gave no details; "
	"an officer will contact them on the phone number they called from."
)

# Accounts the officer API never manages, whatever their other roles.
PRIVILEGED_ROLES = frozenset({"System Manager", "Administrator"})

# Who may file on someone else's behalf: assisted submitters (Development Agents, call centre,
# walk-in intake) and administrators. Officers do not file. The named submitter stays the
# owner of the case and the filing user is recorded as the assisting officer.
ASSISTED_FILER_ROLES = frozenset({ROLE_ASSISTED_SUBMITTER, ROLE_ADMIN, "System Manager", "Administrator"})
STAFF_ROLES = frozenset({ROLE_OFFICER, ROLE_ADMIN, "System Manager", "Administrator"})
ADMIN_ROLES = [ROLE_ADMIN, "System Manager", "Administrator"]
# Admin endpoints that only read: the Review Officer joins the admins here and nowhere
# on a POST, PATCH or DELETE.
ADMIN_READ_ROLES = [*ADMIN_ROLES, ROLE_REVIEW_OFFICER]
# Who may read Reviewer accounts through the officer API. Admins only for now: whether a Review
# Officer may see them is a product decision still to be confirmed. Adding ROLE_REVIEW_OFFICER
# here is the whole change if the answer is yes.
REVIEWER_READ_ROLES = ADMIN_ROLES


# Sensitive and urgent classification (STG-514). The handling class and priority a case carries are
# decided once, at filing, by services/classification.py from the type and the category's SLA
# rules; these are the values it writes.
HANDLING_STANDARD = "Standard"
HANDLING_URGENT = "Urgent"
HANDLING_CLASSES = (HANDLING_STANDARD, HANDLING_URGENT)
# Priority has one value: an urgent case is Urgent, any other case has no priority (blank). It
# follows the rule's `is_urgent`; Normal and High were removed.
PRIORITY_URGENT = "Urgent"
PRIORITIES = (PRIORITY_URGENT,)

# `level_name` of the areas a Kebele-level officer is seated at: the Kebele and anything below it.
# A sensitive case never reaches an officer whose every seat is one of these.
KEBELE_AND_BELOW_LEVELS = frozenset({"Kebele", "Village", "Ward"})
