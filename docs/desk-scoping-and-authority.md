# Desk Scoping, Authority Lookups & Routing Fallback

How a desk (`Grievance RBAC Assignment`) is scoped, how the system decides who holds a rung
for a case, how a case is always given an owner, and the privacy rules around what a
submitter sees. This complements [`routing-architecture.md`](routing-architecture.md), which
covers desk matching and the L1 → L2 → L3 officer cascade in detail.

## 1. Goals

1. **Every case gets an owner.** A submitted grievance is never left unassigned because no
   desk matched it exactly.
2. **Category, type and provider matter more than region.** When nothing matches exactly, the
   system gives up the least important criterion first.
3. **Flexible staffing without fake data.** A Category Admin (L3) can be created before they
   belong to a department, and a desk can exist before it has officers.
4. **Most specific wins.** A wildcard desk (empty department or category) is a last resort,
   never a shortcut past the department's own head.
5. **No internal identifiers reach a submitter.** Officer emails, department IDs and internal
   reasons stay on staff-only records.
6. **Bounded query cost.** Authority lookups cost the same no matter how many desks exist.

## 2. The desk model

A desk is a `Grievance RBAC Assignment` with officer rows (`Grievance RBAC Assignment Officer`).

| Field                         | Required | Empty means                              |
| :---------------------------- | :------- | :--------------------------------------- |
| `department_scope`            | No       | Every department                         |
| `category_scope`              | No       | Every category                           |
| `grievance_type_scope`        | No       | Every type (legacy narrowing scope)      |
| `service_provider_scope`      | No       | Every provider (legacy narrowing scope)  |
| `officers`                    | No       | Nobody yet; officers can be seated later |
| officer `administrative_area` | No       | Every area                               |

Validation (`GrievanceRBACAssignment.validate`) applies to every desk that is not narrowed by
type or provider (`is_unnarrowed`):

- one desk per (category, department) pair, where an empty value is its own key, so there is at
  most one "every department" desk per category and one desk with neither scope;
- the category row is locked during the duplicate check only when a category is set;
- linked officers must exist, be enabled and hold an officer role; a set category must be active.

Type- and provider-scoped desks predate these rules and are left alone.

### What an empty scope does in each subsystem

| Subsystem                      | Empty department                           | Empty category                        |
| :----------------------------- | :----------------------------------------- | :------------------------------------ |
| Read access (`permissions.py`) | Not filtered on department                 | Not filtered on category              |
| Auto-routing (`routing.py`)    | Wildcard match; `assigned_dept` left empty | Wildcard match                        |
| Escalation / approver lookups  | Wildcard, ranked below specific desks      | Wildcard, ranked below specific desks |

A desk with **no field set at all** grants no read access (`_scope_covers`: a scope with no
field set covers nothing). Exception: if the officer row has an `administrative_area`, the area
is the only clause and the officer reads every case in that area.

### Who reads a case below the top rung (`desk_teams`)

An officer below the category admin reads their own cases and, on each desk they sit on, the
cases that desk covers (department, category, and their row's area) held by an officer on a
lower rung of that same desk. `reports_to` plays no part: it names one supervisor for the whole
person, so following it leaked one desk's cases to a supervisor on another (a Kebele head on seven
desks) and hid a case from a second specialist at the same rung. The same rule decides who may act
on the case, the case list's `can_act` and the dashboard scope.

## 3. Authority lookups: `covering_rows`

Every "who holds this rung for this case" question goes through
`grievance_rbac_assignment.covering_rows(rows, department, category, administrative_area)`:

- `find_officer_by_role_level` — escalation targets, notification recipients
- `holds_rung` → `permissions.is_department_head` — may this user approve directly?
- `get_officer_supervisor` — the officer's `reports_to` for this case
- `department_head_of` — the reassignment approver

### Ranking

1. Drop rows whose department, category or area excludes the case (empty = wildcard).
2. Rank the rest by **specificity**: names both the case's department and category (2) >
   names one (1) > names neither (0).
3. Then by the **narrowest covering area** (smallest `rgt - lft`; no area is widest).
4. Then by the rows' own order (`is_primary DESC, modified DESC`).

The case's **category is always passed** (`grievance.service_category`, or the request's
target category for a reassignment via `reassignment.target_category`). Before this change the
lookups ignored category, so the head of category A was also the approver for category B.

### Query cost

- `find_officer_by_role_level` and `holds_rung` push the department and category filters into
  SQL (`query_active_officer_assignments`, wildcard-aware `= x OR IS NULL OR = ''`), so only
  covering desks are loaded.
- `covering_rows` fetches the area bounds of the case and every candidate row in **one** query.
- `get_officer_supervisor` loads the user's own rows unfiltered (its non-strict mode falls back
  to the first assignment), which is a handful of rows.

Each lookup is therefore one officer query plus at most one area query. Escalation may still
call it once per rung while climbing the chain.

## 4. Routing fallback: every case gets an owner

`routing.ranked_assignments(grievance)` yields desks best first; `apply_routing` takes the
first desk that yields an officer.

1. Desks whose category, type and provider scopes each equal the case's or are empty (an empty
   scope is a wildcard). A desk set to another category, type or provider never takes the case.
2. The **Unrouted** desk.

Within step 1, the desk whose officers cover the case's area most tightly ranks first, then
desks with more matching scopes (the order `find_matching_assignment` always used). Region is
the last thing given up: a desk qualifies even
when none of its officers cover the case's area, because the officer pick climbs rungs to one
with no area (usually the Category Admin). If no rung covers the area, any officer on the desk
is used. A desk with no officer is skipped. A desk with neither a department nor a category is
skipped: it is a Category Admin's last-resort desk and must not take cases ahead of the
Unrouted triage desk. A case routed to a desk with no department keeps `assigned_dept` empty;
work on it needs only an assigned officer (`api/v1/grievance.py::_ensure_owner`).

### The Unrouted desk

Seeded idempotently on every migrate by `setup/install.py::seed_unrouted_desk`:

- department `Unrouted` (`C.UNROUTED_DEPARTMENT`);
- user `unrouted@grievance.local` (`C.UNROUTED_OFFICER`), role `Grievance Officer`, no
  password, no welcome email;
- a desk on the default category (`Other`) held by that user at `nodal_officer`, with
  `reassignment_requires_approval` off.

It is excluded from passes 1–3 and used only last. Cases are reassigned from there without
waiting for the receiving department head. A
case stays unassigned only if the Unrouted desk is deactivated.

Separately, a case in the `Other` category moved to another category skips the receiving head's
ruling only when the person moving it is an officer on a desk for `Other` itself
(`reassignment._sits_on_fallback_desk`). Anyone else, a department head included, still goes through the
receiving head.

`find_matching_assignment` (used by reassignment side effects) still uses the original strict
match.

### Reassigning with only a department or a category

`target_department` and `target_category` on `POST /grievances/<ticket>/reassign` are each
optional. The one left out is kept when a desk still matches it: a category-only move first looks
for desks in the case's current department (a case with no department matches desks with none),
and a department-only move first looks for desks on the case's current category. Only when none
matches are the other desks considered (every desk except the Unrouted desk, or the target
officer's desks). `target_grievance_type` narrows them as before.

The case then takes the department of the desk its officer sits on: the current officer when
they are on a surviving desk, otherwise the officer picked on the first one. A desk with no
department clears `assigned_dept`. A desk with no category keeps the case's category.

### Category assignment API (`api/v1/category_assignment.py`)

- `service_category` required; `department`, `l1_officer`, `l2_officer` optional.
- No department: the desk serves the category in every department.
- No officers: the desk is created empty; L1/L2 seats are filled later.

### Officer API (`api/v1/officer.py`)

| Field                | L1 / L2  | L3 (Category Admin)                  | Reviewer |
| :------------------- | :------- | :----------------------------------- | :------- |
| `level`              | Required | Required                             | Refused  |
| `designation`        | Optional | Optional                             | Optional |
| `department`         | Optional | Optional (every department)          | Optional |
| `service_categories` | Required | Optional (the desk with no category) | Required |

An officer has no record of their own: they are a User plus desk rows, and `role_of()` finds
them by those rows. That is why an L3 without a department or categories is still placed on a
desk (one with those scopes empty) instead of being a bare login. `designation` is a label on
the rows and is not read by routing, permissions or escalation.

### Moving a Category Admin to another department

`PATCH /officers/<id>` with a new `department` and no `service_categories` seats an L3 on the
**union** of their own categories and the categories the new department already has desks for
(`services/officer.py::_head_categories_in`). Missing desks are created in the new department, and the officer leaves the old department's desks.
Sending `service_categories` uses exactly those. L1 and L2 still need existing desks. To switch
policy later (only the department's categories, or only the overlap), change the set operation
in `_head_categories_in`.

### Grievance Desk Head

The top rung (labelled Category Admin) is backed by the Frappe role **Grievance Desk Head**
(`C.ROLE_DESK_HEAD`). The role is only a gate: a holder with no active top-rung RBAC record can
do nothing, and the records decide the scope.

- **Granted with the rung.** `services/officer.py::_sync_desk_head_role` adds the role when an
  officer holds an active top-rung row and removes it when they no longer do (level change,
  deactivation). It is the only writer. Existing heads get it from
  `patches/grant_desk_head_role.py`.
- **No power over the structure.** A Desk Head never edits departments, categories, SLA or
  RBAC records themselves. They manage the **people on their records** and the **grievance
  types of their categories**.
- **People** (`POST` / `PATCH /officers`, `services/officer.py::scope_create` / `scope_update`):
  - The head's records are the RBAC records where they hold an active top-rung row
    (`_head_records`).
  - A new L1 or L2 joins the head's records, narrowed by `department` and `service_categories`
    when given, otherwise all of them, and reports to the head unless `reports_to` is given.
  - An existing L1 or L2 can be edited only when every record they sit on is one of the head's.
    Changing `service_categories` re-seats them among the head's records only.
  - A Desk Head cannot create L3s or Reviewers, create departments, or move officers between
    departments.
  - A Desk Head never creates, enrols or edits a login that holds an admin role, even one that
    is also an officer (`_assert_not_admin`), as `reset_temporary_password` already refuses.
- **Scope for types** (`permissions.head_scopes`): the (department, category) of each top-rung
  record, where an empty field is every department or every category.
- **Supervisors**: an L1 reports to an L2 or L3, an L2 to an L3 (`SUPERVISOR_LEVELS`).
- **Grievance types** (`assert_heads_category`): create, update and deactivate on a category the
  head's record names, or, for a record with no category, on the categories its department has
  an active RBAC record for (every category when it has no department either). Listing and
  reading types is open to Desk Heads.
- **Category detail** (`GET /service-categories/<category>`): returns the departments with a
  record on the category (`null` is every department), the SLA (`sla_days`, `auto_escalate`) and
  the grievance types. Desk Heads read only categories they head.
- **DocPerm**: the role has read, write and create on `Grievance Type` and
  `Grievance RBAC Assignment` so the API's saves pass Frappe's permission check. The
  `has_permission` hooks (`permissions.has_rbac_assignment_permission`,
  `permissions.has_grievance_type_permission`) narrow every write by a Desk Head to the records
  they head and the categories they head, so Frappe's generic `/api/resource` cannot reach
  further than the API does. A Desk Head cannot create an RBAC record, and cannot move a type
  to a category they do not head. Admins and other roles are left to the DocPerm rows.

### Role level label

The top rung keeps its record id `department_head` (officer rows link to it) and is labelled
**Category Admin** (`level_name`). New sites get the label from `ROLE_LEVELS`; existing sites
from `patches/rename_department_head_level_to_category_admin.py`. Only the label is shown, so
it can change without touching any row.

The Department Officer notification recipient resolves the top rung of
`GrievanceRoleLevel.get_chain()`, then the bottom rung, instead of hardcoded rung ids. The API
level codes (`L1`/`L2`/`L3` → rung id in `services/officer.py::LEVEL_CODES`) stay a fixed
mapping on purpose: deriving them by chain position would shift their meaning if a rung were
inserted.

## 6. Submitter privacy

### Reassignment timeline (`GrievanceChangeRequest.record_on_timeline`)

- An **internal** `note` always carries the full record: field changes with officer emails and
  department ids, the reason and the decision note. `req.flags.timeline_entry` points at it.
- When an approved request changes `assigned_dept` or `assigned_to`, a **public**
  `assignment` entry is added: "Your grievance has been reassigned to {department name}."
  (`Grievance Department.dept_name`), or a generic line when no name is found.

Entries written before this change still hold the raw body.

### Notification templates (`services/notifications.py`)

`PLACEHOLDER_EXPRESSIONS` maps placeholders that do not render the case field of the same name:

- `{department}` → `doc.assigned_dept`
- `{assigned_to}` → `frappe.db.get_value('User', doc.assigned_to, 'full_name')`, the officer's
  name instead of their email

`unpack_template` maps the compiled expression back to `{assigned_to}` for the editor.
`patches/render_assigned_officer_name_in_templates.py` rewrites already saved Grievance
notification templates.

## 7. Known limits and follow-ups

- An officer row with an area on a desk with no department and no category reads every case in
  that area (§2). Block area-only access if that is not wanted.
- Attachments are scanned asynchronously; a grievance is never rejected for a pending or
  infected file. Infected files lose their object and are withheld from download.
- Legacy notification recipient aliases (`Nodal Officer`, `Department Head`,
  `Top Level Authority`) and request-field aliases (`client_uuid`, `category`, the
  `submitter.py` aliases) are still accepted.
- Older reassignment timeline entries keep their raw body (§6).
