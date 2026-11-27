# Operational logging

The grievance service logs through the shared OAN logger in `oan_auth_service`: one JSON object per
line, a correlation id on everything written during a request or a job, and no credentials or personal
data. The format, the correlation id, the request log (`http.request`, `security.*`), masking and
scrubbing, `emit` and `log_error` are all described in
`oan_auth_service/docs/OPERATIONAL_LOGGING.md`; this page covers only what the grievance service adds.

The code here is [`services/grievance_log.py`](../oan_grievance_service/services/grievance_log.py): the
fields that identify a case, and the framing of the scheduled SLA sweeps. The events themselves are
`emit` calls where the SLA and escalation logic runs (`services/sla.py`, `tasks.py`).

## Events

| Event                                                      | Level   | When                                                                                                                                          |
| ---------------------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `sla.clock_started` / `clock_recalculated` / `clock_reset` | INFO    | The SLA window was stamped, moved by a category change, or restarted.                                                                         |
| `sla.level_window_started`                                 | INFO    | A case reached a level with its own SLA days (escalation, reopen, handover) and got a fresh window of them: `rung`, `sla_days`.               |
| `sla.clock_paused` / `clock_resumed` / `clock_stopped`     | INFO    | A hold began or ended; the case _entered_ a stopped state (`breached` says whether it was late). Once: Resolved to Closed does not repeat it. |
| `sla.reminder_sent`                                        | INFO    | The 50% or 80% reminder was queued.                                                                                                           |
| `sla.state_timer_expired`                                  | INFO    | A case sat in a state longer than its timer allows (`action` is Escalate or Auto Close).                                                      |
| `escalation.escalated`                                     | INFO    | A case moved up a rung: `rung`, `from_user`, `to_user`, `escalated_at`, `next_escalation_at`, `reason`.                                       |
| `escalation.no_higher_authority`                           | WARNING | A case breached and nobody sits above its assignee.                                                                                           |
| `security.temporary_password_reissued`                     | WARNING | An admin issued an officer a temporary password. No password is logged. WARNING so a raised level keeps it.                                   |
| `job.started` / `job.finished` / `job.failed`              | INFO    | The hourly SLA sweeps, with `duration_ms` and `processed`.                                                                                    |

Every SLA and escalation event carries `grievance_fields`: `grievance`, `ticket_number`, `category`,
`department`, `status`, `assigned_to` (masked) and `sla_due_at`. A case's own `submitter_name`,
`contact_mobile` and `contact_email` are never passed to a log call. The users in `from_user`, `to_user` and
`assigned_to` are masked (`a***@example.com`), so two officers who share an initial and a domain look
alike; a stable keyed handle for them is left for a follow-up.

A rung with nobody on it, a failed escalation, a failed sweep item, a failed notification and the other
existing failure paths are `error` events with the title they always had, and also Error Log rows
(`oplog.log_error`, in place of `frappe.log_error`).

A sweep (`send_sla_reminders`, `escalate_breached`, `forward_stale_change_requests`,
`expire_state_timers`) is wrapped with `@logged_job("name")`. It gets a correlation id from the shared
helper (a plain UUID, or Frappe's monitor trace id for the job when `monitor: 1` is set), so every line
the sweep writes, including errors, can be pulled with one filter; the `job` field on `job.started` says
which sweep it was.

## Adding a line

```python
import logging

from oan_auth_service.api import operational_log as oplog

from oan_grievance_service.services.grievance_log import grievance_fields

oplog.emit("sla.something", **grievance_fields(grievance), detail="...")
oplog.emit("escalation.something", logging.WARNING, **grievance_fields(grievance))
```

Pick the identifying fields; do not log a document, a body or `frappe.form_dict`. The shared doc has the
rules.

## Configuration

None of its own. `oan_slow_request_ms`, the level and rotation are the shared logger's (see the shared
doc): the log goes to stderr with `FRAPPE_STREAM_LOGGING=1`, where rotation and retention belong to the
container runtime and log collector.
