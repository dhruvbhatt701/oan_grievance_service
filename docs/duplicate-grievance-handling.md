# Duplicate grievance handling

The system flags a likely duplicate; an officer rules on it. Only a confirmed duplicate changes
the ticket: it moves to the terminal `Duplicate` status, points at the main ticket and becomes
read-only. Code: `services/duplicates.py`. Algorithm background: `GRIEVANCE_SIMILARITY_ALGORITHMS.md`.

## Flow

0. **Ask first (web portal only, off by default).** When `Ask Before Filing a Similar Grievance`
   is ticked in Grievance Settings, before a grievance is filed, `POST /v1/grievances` checks the
   submitter's grievances of the last 24 hours with the same score (below). If one is similar it
   files nothing and answers `409 POSSIBLE_DUPLICATE` with `details.possible_duplicates`. The client
   shows them and asks whether this is a new grievance; sending the same request again with
   `confirm_new: 1` files it. IVR and SMS are one way, so nobody could answer: they are never asked
   and are only flagged in the next steps. The setting stays unticked until the web client handles
   the 409 and sends `confirm_new`; until then a farmer is never refused, and the next steps still
   flag the repeat for an officer.
1. A grievance is filed. Once the filing commits, a queued job (`duplicates.enqueue_flagging`) runs
   `flag_possible_duplicates`, which compares it with the same submitter's earlier grievances.
   Flagging only advises an officer, so a fault in it is logged (Error Log) and undone to a
   savepoint; it never loses the farmer's grievance.
2. Each earlier ticket that scores at or above the threshold becomes a `Grievance Duplicate` row
   awaiting review (best three at most). The ticket itself is untouched and the submitter is told
   nothing.
3. The officer sees `possible_duplicate` (the main ticket, match level and what matched) when
   opening the case (`GET /v1/grievances/{ticket}/timeline`) and in the `current_state` that
   actions return, and a `possible_duplicate_of` chip in the list, then rules:
   - `POST /v1/grievances/{ticket}/duplicate/confirm` sets `duplicate_of`, takes the workflow
     action `Confirm Duplicate`, adds the public timeline message and marks the row confirmed.
     The ticket's other pending flags are dismissed as superseded.
   - `POST /v1/grievances/{ticket}/duplicate/dismiss` marks the rows dismissed and adds an internal
     note. The ticket stays a normal one: one still Submitted with nobody assigned is routed then,
     as a new filing is.

The main ticket must be the same submitter's and still open or resolved (Submitted, Assigned,
Reopened, In Progress, More Info Needed or Resolved): never Closed, Rejected or itself a duplicate,
and never another farmer's, because the farmer is shown its number. The Grievance controller checks
the same rule on every save. `dismiss` is refused on a ticket that is already a duplicate.

A confirmed duplicate cannot be undone: `Duplicate` has no outgoing workflow transition, so it is
terminal and the Grievance controller refuses every save.

## Candidates

| Rule      | Value                                                                                  |
| --------- | -------------------------------------------------------------------------------------- |
| Submitter | Same `submitter` profile. Anonymous grievances have none, so they are never flagged    |
| Age       | Earlier tickets only, so the main ticket is always the older one                       |
| Status    | Submitted, Assigned, Reopened, In Progress, More Info Needed, Resolved                 |
| Window    | The prompt looks back 24 hours (`PROMPT_WINDOW_HOURS`). The officer flag has no window |
| Pool      | The submitter's 200 most recent tickets                                                |
| Sub-type  | Another sub-type of the same category is a new grievance and is not compared           |

Submitted, Assigned and Reopened are candidates on purpose. A double submit is compared with a
ticket that has barely been routed, and the specification's three statuses alone would miss it.

## Score

```
score = 0.70 * description + 0.10 * same category + 0.20 * same type
```

These are the defaults. The category weight, the type weight and both thresholds are set in
Grievance Settings (Duplicate Detection); the description weight is whatever the other two leave
of 1.

- `description` is the cosine of character 3-5-grams of the normalised text: lower-cased,
  punctuation removed, Ge'ez homophones folded (ሐ/ኀ to ሀ, ሠ to ሰ, ዐ to አ, ጸ to ፀ). Folding is applied to
  the copy used for matching only; the stored description is never changed.
- Category and type are weights, not filters, because submitters sometimes pick the wrong category.
  The exception is the Sub-type rule above: within one category a different sub-type is a different
  grievance (staging test report, 26 Nov 2026).
- A flag needs `score >= 0.65` (the flag threshold); `High` is `>= 0.85`, otherwise `Medium`.
- Category plus type is 0.30, below the threshold, so matching labels alone never flag. Saving
  Grievance Settings is refused if the category weight plus the type weight reaches the flag
  threshold, if a weight is negative, or unless `0 < flag threshold <= high threshold <= 1`. If the
  stored values are ever unusable (for example before the migrate that adds them), the defaults
  above apply.
- Identical text filed under a different category and type scores 0.70 and is still flagged.

The score ranks; it is not a probability, so the API returns the `High` or `Medium` label and
never a percentage. The defaults are starting points: they are unmeasured, and the confirm and
dismiss rulings officers record are the labelled pairs to tune them against (section 8a of the
algorithms document). Watch the dismiss rate: complaints that share a category, a type and place or
cooperative names can cross the threshold on a description similarity of about 0.5.

## Status, read-only and the dashboard

- `Duplicate` is a Workflow State (style Inverse, SLA category Stopped). The `Confirm Duplicate`
  action is offered from every state except Resolved, Closed, Rejected and Duplicate, to
  `Grievance Officer` and `Grievance Admin`.
- The generic `/action` endpoint refuses `Confirm Duplicate` and does not list it, because only the
  duplicate endpoints name the main ticket.
- Comments (`/message`), attachment uploads and change requests (defer SLA, reassign, reclassify)
  are refused on a duplicate; removing evidence was already refused on a stopped case. The farmer sees the `Duplicate` status and the timeline message. Opening a confirmed duplicate (`/timeline`) returns `duplicate_of`, the main ticket, for the banner that links to it.
- Status cards: `Duplicate` (labelled Duplicates) is new, `In Progress` counts In Progress and
  More Info Needed together, and `All` includes duplicates. A farmer still sees the status `More Info Needed` on the case itself, because that is the cue to reply.
- Ticket export: the Closure section treats the move into Duplicate as the closure, with the
  remarks starting "Duplicate of" the main ticket.
- Dashboard: a `duplicates` KPI, `Duplicate` in the status distribution, and the duplicate count
  inside Total and every other count. `dup_pending` (awaiting duplicate review) no longer counts a
  dismissed flag.

## Migration

`add_duplicate_status` re-seeds the workflow. `dismiss_legacy_duplicate_flags` dismisses the
unreviewed rows the earlier same-type-within-7-days detector left (method `Identity + Type + Time Proximity`, score 1.0, no match level), so they do not appear as pending reviews under the new rule.
Confirmed legacy rows are kept.

## Not done yet

- A prompt on channels other than the web portal, which needs its own two-way flow.
- Notifying the farmer when a duplicate is confirmed. The `duplicate_detected` template still
  describes the old submitter-decides flow and is no longer sent.
- Embeddings (T5) and Tigrinya handling, which the algorithms document schedules for phase 2.
