# API Development Standards & Best Practices

This document defines the architectural conventions, decorator pipeline, request validation rules, error handling, versioning policy, and test requirements for writing new REST API endpoints in `oan_grievance_service`.

---

## 1. Core Architecture & Design Principles

1. **Thin API Layer, Rich Service Layer:** API handlers must act solely as HTTP gateways. They perform authentication, request validation, invoke domain service methods (in `oan_grievance_service/services/`), and format the response envelope. **Do not write direct SQL or business logic inside API handlers.**
2. **Immutable Versioning:** All public endpoints live under a versioned package (`oan_grievance_service/api/v1/`).
   - Additive, backward-compatible fields remain in `v1`.
   - Breaking changes (field renaming, type changes, narrowing constraints) require opening a new version package (e.g. `v2/`).
   - Never modify or break an existing released version.
3. **Consistent Response Envelopes:** Every endpoint returns standard responses via `success_response()`, with version metadata (`meta`) automatically attached by `@handle_api_errors`.
4. **Auditable & Non-Bypassing:** All operations that mutate DocType state must route through standard Frappe document methods or service hooks so workflow guards, timeline logs, and status history are preserved.

---

## 2. Directory Structure & URL Mapping

Endpoints support two transports:

1. **REST Transport (Preferred):** Clean HTTP paths mounted into Frappe's URL Map via `@prefixed(...)` / `@rest(...)`.
2. **RPC Transport (Backward Compatibility):** Frappe's method dispatch convention (`/api/method/...`).

```
oan_grievance_service/api/
├── __init__.py                # Version registry, metadata helpers & router re-exports
├── router.py                  # REST route loader & URL map registration
├── middleware.py              # JWT validation & RPC path exemptions
└── v1/                        # Version 1 API package
    ├── __init__.py            # Module index & endpoint catalog
    ├── grievance.py           # Case submission, tracking & lifecycle actions
    ├── draft.py               # Save, resume and discard partial submissions
    ├── submitter.py           # Submitter profile & lookup options
    └── administrative_area.py # Cascading geo-hierarchy lookups
```

### REST Resource Naming Standards

All REST endpoints in `oan_grievance_service` follow industry-standard RESTful conventions:

1. **Plural Resource Nouns:** Top-level and nested collections must use plural nouns (`/api/v1/grievances`, `/api/v1/submitters`, `/api/v1/administrative-areas`).
2. **Kebab-Case URL Segments:** Compound resource names must use lowercase kebab-case (`/administrative-areas`, not snake_case `administrative_area`).
3. **No Frappe DocType Aliasing:** Do not create duplicate URL aliases to mirror internal Frappe DocType conventions (e.g. avoid creating duplicate singular `/grievance`, snake_case `/administrative_area`, or `/profile` routes). The REST API contract remains clean, consistent, and strictly decoupled from internal DocType names.
4. **Clean Root Collection Paths:** Use collection roots directly with query parameters (`GET /api/v1/administrative-areas?parent=...`) rather than nested RPC verb suffixes like `/areas` or `/get_areas`.
5. **State Transition Action Verbs:** For non-CRUD lifecycle state transitions, use clear POST action sub-paths on item resources (`/api/v1/grievances/<ticket_number>/confirm`, `/reopen`, `/escalate`).

**URL Mapping:**

| Endpoint Purpose          | REST Route (Standard)                                    | RPC Route (Legacy)                                                                    | Method |
| ------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------ |
| Health Check              | `GET /api/v1/grievances/health`                          | `GET /api/method/oan_grievance_service.api.router.get_health`                         | GET    |
| Ping                      | `GET /api/v1/grievances/ping`                            | `GET /api/method/oan_grievance_service.api.router.get_ping`                           | GET    |
| Submitter Options         | `GET /api/v1/submitters/options`                         | `GET /api/method/oan_grievance_service.api.v1.submitter.options`                      | GET    |
| User Profile & Claims     | `GET /api/v1/auth/me`                                    | `GET /api/method/oan_auth_service.api.v1.auth.get_me`                                 | GET    |
| Submitter Profile (Dep.)  | `GET /api/v1/submitters/me`                              | `GET /api/method/oan_grievance_service.api.v1.submitter.me`                           | GET    |
| Administrative Areas      | `GET /api/v1/administrative-areas`                       | `GET /api/method/oan_grievance_service.api.v1.administrative_area.get_areas`          | GET    |
| Area Ancestors            | `GET /api/v1/administrative-areas/<path:area>/ancestors` | `GET /api/method/oan_grievance_service.api.v1.administrative_area.get_area_ancestors` | GET    |
| List Grievances           | `GET /api/v1/grievances`                                 | `GET /api/method/oan_grievance_service.api.v1.grievance.list_grievances`              | GET    |
| Save Draft                | `POST /api/v1/drafts`                                    | `POST /api/method/oan_grievance_service.api.v1.draft.save`                            | POST   |
| Get Draft                 | `GET /api/v1/drafts`                                     | `GET /api/method/oan_grievance_service.api.v1.draft.load`                             | GET    |
| Grievance Options         | `GET /api/v1/grievances/options`                         | `GET /api/method/oan_grievance_service.api.v1.grievance.options`                      | GET    |
| Submit Case               | `POST /api/v1/grievances`                                | `POST /api/method/oan_grievance_service.api.v1.grievance.submit`                      | POST   |
| Track / Case Detail       | `GET /api/v1/grievances/<ticket_number>`                 | `GET /api/method/oan_grievance_service.api.v1.grievance.track`                        | GET    |
| Timeline & Thread Summary | `GET /api/v1/grievances/<ticket_number>/timeline`        | `GET /api/method/oan_grievance_service.api.v1.grievance.timeline`                     | GET    |
| Add Note                  | `POST /api/v1/grievances/<ticket_number>/note`           | `POST /api/method/oan_grievance_service.api.v1.grievance.add_note`                    | POST   |
| Post Message              | `POST /api/v1/grievances/<ticket_number>/message`        | `POST /api/method/oan_grievance_service.api.v1.grievance.message`                     | POST   |
| Confirm Case              | `POST /api/v1/grievances/<ticket_number>/confirm`        | `POST /api/method/oan_grievance_service.api.v1.grievance.confirm`                     | POST   |
| Reopen Case               | `POST /api/v1/grievances/<ticket_number>/reopen`         | `POST /api/method/oan_grievance_service.api.v1.grievance.reopen`                      | POST   |
| Escalate Case             | `POST /api/v1/grievances/<ticket_number>/escalate`       | `POST /api/method/oan_grievance_service.api.v1.grievance.escalate`                    | POST   |
| Assign Case               | `POST /api/v1/grievances/<ticket_number>/assign`         | `POST /api/method/oan_grievance_service.api.v1.grievance.assign`                      | POST   |
| Reassign Case             | `POST /api/v1/grievances/<ticket_number>/reassign`       | `POST /api/method/oan_grievance_service.api.v1.grievance.reassign`                    | POST   |
| Reply to Info Request     | `POST /api/v1/grievances/<ticket_number>/reply`          | `POST /api/method/oan_grievance_service.api.v1.grievance.reply`                       | POST   |

---

## 3. Decorator Pipeline & Execution Order

Every API endpoint must apply decorators in the exact order shown below:

```python
from oan_auth_service.api.router import prefixed
from oan_auth_service.api.utils import handle_api_errors, require_role, success_response, validate_request

route = prefixed("/api/v1/grievances")

@route("/your-action", methods=("POST",), summary="Action description")
@frappe.whitelist()                               # Exposes method via HTTP RPC
@handle_api_errors                                # Catches exceptions and formats error JSON
@require_role(ALLOWED_ROLES)                      # Enforces RBAC permissions
@validate_request(YourRequestModel)               # Validates payload schema via Pydantic
def your_endpoint(**kwargs):
    ...
```

### Decorator Responsibilities

| Decorator                    | Source                        | Purpose                                                                                                                                                                                                  |
| ---------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `@route(...)` / `@rest(...)` | `oan_auth_service.api.router` | Exposes clean RESTful URL endpoint on Frappe's `API_URL_MAP` and registers guest exemptions.                                                                                                             |
| `@frappe.whitelist()`        | `frappe`                      | Whitelists the Python function for HTTP invocation. Use `allow_guest=True` only for public, unauthenticated routes.                                                                                      |
| `@handle_api_errors`         | `oan_auth_service.api.utils`  | Intercepts `frappe.ValidationError`, `frappe.PermissionError`, etc., and returns standard JSON error responses with appropriate HTTP status codes. Dynamically resolves service-specific `version_meta`. |
| `@require_role(...)`         | `oan_auth_service.api.utils`  | Blocks requests if the authenticated user lacks one of the specified roles (e.g., `Grievance Submitter`, `Grievance Officer`).                                                                           |
| `@validate_request(Model)`   | `oan_auth_service.api.utils`  | Validates input against a Pydantic schema before executing the handler.                                                                                                                                  |

---

## 4. Request Validation with Pydantic

All `POST` / mutation endpoints must define an explicit `pydantic.BaseModel` schema.

```python
from pydantic import BaseModel, Field
from oan_auth_service.api.utils import SafeEmail

class SubmitGrievanceRequest(BaseModel):
	model_config = {"extra": "allow"}

	# Soft at the edge: draft (`client_uuid`) and profile may supply values.
	# Presence + format are enforced after merge via identity.validate_submission_payload.
	submitter_type: str | None = None
	submitter_name: str | None = None
	contact_mobile: str | None = None
	submission_channel: str | None = None
	administrative_area: str | None = None
	service_category: str | None = None
	grievance_type: str | None = None
	description: str | None = None
	contact_email: SafeEmail | None = None
	consent_given: int | None = Field(None, ge=0, le=1)
	client_uuid: str | None = None
	client_submission_uuid: str | None = None
	is_anonymous: int | None = Field(0, ge=0, le=1)
```

### Guidelines for Schemas

- Use `RequiredPhone` and `SafeEmail` utility types from `oan_auth_service.api.utils`.
- Enforce sensible length and boundary constraints using `Field(..., min_length=...)` or `ge`/`le`.
- Set `model_config = {"extra": "allow"}` if forward compatibility with client parameters is required, or `"forbid"` if strict parameter policing is desired.

---

## 4.1 Submit Grievance API (STG-328)

Finalizes a case: validates input, allocates a ticket ID (STG-327), moves status out of
`Draft`, and returns the ticket to the caller. Draft wizard state (STG-322 / STG-325)
carries over when `client_uuid` is provided.

|      |                                                                  |
| ---- | ---------------------------------------------------------------- |
| REST | `POST /api/v1/grievances`                                        |
| RPC  | `POST /api/method/oan_grievance_service.api.v1.grievance.submit` |
| Auth | Required (`Grievance Submitter` and staff roles)                 |

**Full-body example**

```json
{
  "submission_channel": "Mobile App",
  "administrative_area": "<area id or path_code>",
  "service_category": "Inputs",
  "grievance_type": "Fertilizer Shortage",
  "description": "At least twenty characters describing the grievance.",
  "desired_outcome": "Optional outcome text",
  "consent_given": 1,
  "client_uuid": "<optional draft key>",
  "client_submission_uuid": "<optional idempotency key>"
}
```

Authenticated submitters omit identity fields (`submitter_type`, `submitter_name`,
`contact_mobile`, `contact_email`); the server snapshots them from the session profile.
`woreda` / `kebele` may be sent instead of `administrative_area`.

**Shared field names (draft `payload` ↔ submit body)**

Keys inside draft `payload` are the **same names** as the submit body. They are
never renamed on save or on merge (`submission.SHARED_SUBMISSION_FIELD_KEYS`):

`submitter_type`, `submitter_name`, `contact_mobile`, `contact_email`,
`submission_channel`, `administrative_area`, `administrative_unit`, `woreda`,
`kebele`, `service_category`, `grievance_type`, `description`, `desired_outcome`,
`consent_given`, `is_anonymous`, `assisted_by_officer`, `client_submission_uuid`.

Only the envelope differs: draft wraps those keys in `payload` (+ `client_uuid`,
`step_reached`); submit sends them at the top level (+ optional `client_uuid`).

**Draft-only submit** (payload already saved via `POST /api/v1/drafts`):

```json
{ "client_uuid": "<draft key>", "consent_given": 1 }
```

Request fields overlay the draft. A re-submit of an already-claimed draft returns the
original `ticket_number` with `duplicate_submission: true`.

**Success `data`:** `ticket_number`, `status` (`Submitted`), routing/SLA summary,
`attachments`, `duplicate_submission`.

**Validation failure:** HTTP 400, `code: VALIDATION_ERROR`, per-field `details` map
(STG-321). Missing consent, short description, wrong mobile country code, and
type↔category mismatches are included.

---

## 4.3 Assignment and Reassignment API (STG-337)

Initial assignment and a later move to another department both use the active
`Grievance Routing Rule` for the case's service category and administrative area.
The officer is taken from the RBAC desk scoped to that department. Reassignment
does not wait for approval, and a clock that is already running is not restarted.

|      |                                                                    |
| ---- | ------------------------------------------------------------------ |
| REST | `POST /api/v1/grievances/<ticket_number>/assign`                   |
| REST | `POST /api/v1/grievances/<ticket_number>/reassign`                 |
| RPC  | `POST /api/method/oan_grievance_service.api.v1.grievance.assign`   |
| RPC  | `POST /api/method/oan_grievance_service.api.v1.grievance.reassign` |
| Auth | `Grievance Officer`, `Grievance Admin`, System Manager, Administrator |

**Assign** takes no body fields beyond the ticket. The case must be `Submitted`
and not yet assigned. The winning routing rule sets the department; the desk
strategy sets the officer; the workflow action `Assign` moves the case to
`Assigned` and starts the SLA clock.

**Reassign body**

```json
{
  "department": "<Grievance Department name>",
  "reason": "Why the case is moving. At least five characters.",
  "officer": "<optional User on the target desk>"
}
```

The department must be a different department, and a routing rule for this
category and area must name it. `officer`, when sent, must be an active member
of that department's desk; otherwise the desk strategy picks. Only the current
`assigned_to` user, or an administrator, may call it. Allowed while the case is
`Assigned` or `In Progress`. From `In Progress` the workflow action is
`Refer Onward`, which returns the case to `Assigned`.

`sla_start_at`, `sla_due_date`, and the reminder flags are left as they were.
The move is stored as an auto-routed `Grievance Reassignment Request`
(`sla_treatment: Continue`, no approver) and as an append-only timeline
`assignment` entry. Both refuse later edits. The approval hook does not run for
`auto_routed` rows.

**Success `data`:** `ticket_number`, `status`, `assignment` (`department`,
`assigned_to`, `routing_rule`, `routed_automatically`), `sla`. Reassignment also
returns `audit.name`, `audit.auto_routed`, and `audit.sla_treatment`.

---

## 5. Response Format & Standard Envelopes

All successful responses **MUST** use the `success_response()` helper from `oan_auth_service.api.utils`. `@handle_api_errors` automatically resolves and attaches the `meta` block directly from `api/__init__.py`.

```python
from oan_auth_service.api.utils import handle_api_errors, success_response

@frappe.whitelist(allow_guest=True)  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@handle_api_errors
def options():
	# ...
	return success_response(
		data=data,
		message=_("Options fetched successfully"),
	)
```

### Standard Success Response Payload

```json
{
  "status": "success",
  "message": "Options fetched successfully",
  "data": {
    "submitter_types": [ ... ],
    "submission_types": [ ... ]
  },
  "meta": {
    "api_version": "v1",
    "status": "current"
  },
  "request_id": "8fa1e19d-b4ef-4bbd-9866-9dc7bc5fec1b"
}
```

---

## 6. Authentication & Public Route Exemption

1. **Authenticated by Default:** Requests hitting `/api/method/oan_grievance_service.*` are validated via JWT tokens handled by `oan_auth_service`.
2. **Public Routes (Unauthenticated):** If an endpoint must be accessible without a login or token (e.g. dropdown lookups, public search):
   - Add `@frappe.whitelist(allow_guest=True)` with the `# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method` annotation.
   - Ensure all parameters on whitelisted functions have **explicit type hints** (e.g., `parent: str | None = None, limit: int = 100`).
   - **Explicitly register the endpoint path** in `oan_grievance_service/api/middleware.py`:

```python
EXEMPT_PATHS: list[str] = [
	"/api/method/oan_grievance_service.api.v1.submitter.options",
	"/api/method/oan_grievance_service.api.v1.administrative_area.get_areas",
	"/api/method/oan_grievance_service.api.v1.your_module.public_endpoint",
]
```

---

## 7. Error Handling & Validation Failures

Always raise standard Frappe exceptions with clear, localized messages and titles. `@handle_api_errors` handles the conversion to JSON.

```python
# Validation / Bad Input -> Returns HTTP 400
if not frappe.db.exists("Service Category", kwargs["service_category"]):
	frappe.throw(
		_("The specified service category does not exist."),
		exc=frappe.ValidationError,
		title=_("Invalid Category"),
	)

# Record Not Found -> Returns HTTP 404
if not frappe.db.exists("Grievance", {"ticket_number": ticket_number}):
	frappe.throw(
		_("Ticket #{0} was not found.").format(ticket_number),
		exc=frappe.DoesNotExistError,
		title=_("Grievance Not Found"),
	)

# Permission / Authorization Error -> Returns HTTP 403
if not user_has_scope_access(user, grievance):
	frappe.throw(
		_("You do not have permission to access this grievance."),
		exc=frappe.PermissionError,
		title=_("Forbidden"),
	)
```

---

## 8. Complete Boilerplate Template for a New API

Here is a full, production-ready template to use when creating a new API file:

```python
"""<Module description and FSD reference>."""

import frappe
from frappe import _
from pydantic import BaseModel, Field
from oan_auth_service.api.utils import handle_api_errors, require_role, success_response, validate_request

from oan_grievance_service.services import your_service_module

ALLOWED_ROLES = [
	"Grievance Submitter",
	"Grievance Officer",
	"Grievance Admin",
	"System Manager",
	"Administrator",
]


class ExampleActionRequest(BaseModel):
	model_config = {"extra": "allow"}

	ticket_number: str = Field(..., min_length=1, description="Ticket number of the grievance")
	reason: str = Field(..., min_length=5, description="Reason for the action")


@frappe.whitelist()
@handle_api_errors
@require_role(ALLOWED_ROLES)
@validate_request(ExampleActionRequest)
def perform_action(**kwargs):
	"""Execute the domain action and return the standard response."""
	ticket_number = kwargs["ticket_number"]
	reason = kwargs["reason"]

	# 1. Validation & Record Lookup
	doc = frappe.db.get_value(
		"Grievance",
		{"ticket_number": ticket_number},
		["name", "status", "assigned_officer"],
		as_dict=True,
	)
	if not doc:
		frappe.throw(
			_("Ticket #{0} not found.").format(ticket_number),
			exc=frappe.DoesNotExistError,
			title=_("Not Found"),
		)

	# 2. Invoke Service Layer
	result = your_service_module.process_action(doc.name, reason=reason)

	# 3. Return Standard Response
	return success_response(
		data={
			"ticket_number": ticket_number,
			"status": result.status,
			"updated_at": frappe.utils.now_datetime(),
		},
		message=_("Action performed successfully"),
	)
```

---

## 9. Automated Testing for APIs

Every new API endpoint must have automated tests validating:

1. **Happy Path:** Correct parameters return status 200 with matching `meta` and `data` structures.
2. **Invalid Input:** Missing mandatory fields or malformed data trigger validation errors.
3. **Role Guards:** Requests from unauthorized users fail with permission errors.
4. **Public vs. Protected Checks:** Guest access is permitted on exempt paths and blocked on protected paths.

### Example API Test Case

```python
import frappe
from frappe.tests.utils import FrappeTestCase

class TestAPIEndpoints(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_endpoint_returns_valid_envelope(self):
		from oan_grievance_service.api.v1.submitter import options

		res = options()
		self.assertIn("meta", res)
		self.assertEqual(res["meta"]["api_version"], "v1")
		self.assertIn("data", res)
		self.assertIn("submitter_types", res["data"])
```

---

## 10. Postman Collection Synchronization

When introducing a new API or modifying parameters:

1. Open [`postman/oan_grievance_collection.json`](file:///Users/arnav/Code/frappe_local/frappe-bench/apps/oan_grievance_service/postman/oan_grievance_collection.json).
2. Add the request definition under the appropriate folder with:
   - Method (`POST` / `GET`).
   - URL: `{{base_url}}/api/method/oan_grievance_service.api.v1.<module>.<endpoint>`.
   - Headers: `Authorization: Bearer {{auth_token}}`, `Content-Type: application/json`.
   - Sample request payload and example response body.
