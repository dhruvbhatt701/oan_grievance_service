"""SLA and escalation management.

Reminders at 20%, 50% and 80% of the window, and escalation up the role-level chain on
breach.

The clock starts from the creation date/time of the grievance.

What each workflow state does to the clock is its SLA category, set per state in
setup/install.py (WORKFLOW_STATES): Running, Paused or Stopped. A state can also carry
a timer - how long a case may sit there - set once for every case on the Grievance
Deferral Policy's State Timers.

What belongs here: the SLA clock (start, pause, resume, stop, consumed percentage, working days calculation),
state timers, and walking a case up the escalation chain.

What does not belong here: looking up who holds which rung or who reports to whom.
Those are RBAC assignment queries and live in the Grievance RBAC Assignment module.
Scheduling lives in tasks.py; this module only answers what a run should do.
"""

import logging
import math
from dataclasses import dataclass
from datetime import timedelta

import frappe
from frappe import _
from frappe.model.workflow import get_workflow
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime
from oan_auth_service.api import operational_log as oplog

from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	covering_rows,
	current_level_of,
	find_officer_by_role_level,
	get_officer_supervisor,
	query_active_officer_assignments,
)
from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
	GrievanceRoleLevel,
)
from oan_grievance_service.services import audit
from oan_grievance_service.services import constants as C
from oan_grievance_service.services.grievance_log import grievance_fields

CLOCK_START_ASSIGNMENT = "assignment"
CLOCK_START_CREATION = "creation"

RUNNING = "Running"
PAUSED = "Paused"
STOPPED = "Stopped"

CACHE_KEY_DEFAULT_HOLIDAY_LIST = "grievance_default_holiday_list"
CACHE_KEY_HOLIDAYS_PREFIX = "grievance_holidays_"


@dataclass(frozen=True, slots=True)
class SLAPolicy:
	"""The SLA a case runs on: its category's rule days and the flags of the desk that routed it."""

	sla_days: int
	auto_escalate: bool = True
	notify_on_breach: bool = True
	holiday_list: str | None = None


def get_default_holiday_list() -> str | None:
	"""Returns the default Grievance Holiday List name if one is marked default."""
	cached = frappe.cache.get_value(CACHE_KEY_DEFAULT_HOLIDAY_LIST)
	if cached is not None:
		return cached or None
	default_list = frappe.db.get_value("Grievance Holiday List", {"is_default": 1}, "name")
	frappe.cache.set_value(CACHE_KEY_DEFAULT_HOLIDAY_LIST, default_list or "")
	return default_list


def get_holiday_dates(holiday_list_name: str | None) -> set:
	"""Return set of datetime.date objects for all holidays in the given holiday list."""
	if not holiday_list_name:
		return set()
	cache_key = f"{CACHE_KEY_HOLIDAYS_PREFIX}{holiday_list_name}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return {getdate(d) for d in cached}

	if not frappe.db.exists("Grievance Holiday List", holiday_list_name):
		return set()

	rows = frappe.get_all(
		"Grievance Holiday",
		filters={"parent": holiday_list_name, "is_half_day": 0},
		pluck="holiday_date",
	)
	holidays_set = {getdate(d) for d in rows}
	frappe.cache.set_value(cache_key, [str(d) for d in holidays_set])
	return holidays_set


def is_holiday(date, holiday_list_name: str | None = None) -> bool:
	"""Check if a date is a holiday or weekly off."""
	h_list = holiday_list_name or get_default_holiday_list()
	if not h_list:
		return False
	holidays = get_holiday_dates(h_list)
	return getdate(date) in holidays


def calculate_working_deadline(start_datetime, sla_days: int, holiday_list_name: str | None = None):
	"""Calculate target due date adding working days, skipping holidays and weekly offs.

	Days allocated count from start_datetime (which defaults to creation date/time).
	Preserves the time component of start_datetime.
	"""
	if not start_datetime:
		start_datetime = now_datetime()
	start_dt = get_datetime(start_datetime)
	if not sla_days or sla_days <= 0:
		return start_dt

	h_list = holiday_list_name or get_default_holiday_list()
	holidays = get_holiday_dates(h_list) if h_list else set()

	if not holidays:
		return add_days(start_dt, sla_days)

	cur_dt = start_dt
	days_added = 0
	while days_added < sla_days:
		cur_dt = add_to_date(cur_dt, days=1)
		if cur_dt.date() not in holidays:
			days_added += 1

	return cur_dt


def resolve_policy(service_category, grievance=None, level=None) -> SLAPolicy:
	"""The SLA days of the category's rules, with the flags of the desk that routed `grievance`.

	The days come only from the category's Grievance SLA Rules. A case's window is the days of
	the rung it sits on (`level`, else its assignee's rung on the case) when that rung has a rule
	for it (`classification.level_sla_days`: the Routing Matrix's SLA L1 and SLA L2), so a case
	climbs one level when that rung's window runs out. Otherwise the rule that matches the case
	(its type, area and filing date, `classification.sla_days_for`), for example 3 days in season
	against 15 out of it; without a `grievance`, the base rule's. A category with no rule has
	0 days: no deadline.

	Auto-escalate and notify-on-breach are the desk's (`grievance.routing_rule`). A case no desk
	has routed yet, or no `grievance`, has both on. Working days skip the default holiday list.
	"""
	from oan_grievance_service.services import classification

	if grievance is not None and level is None and grievance.get("assigned_to"):
		level = rung_on_case(grievance.get("assigned_to"), grievance)
	level_days = classification.level_sla_days(service_category, grievance, level)

	desk = grievance.get("routing_rule") if grievance else None
	flags = (
		frappe.db.get_value(
			"Grievance RBAC Assignment", desk, ["auto_escalate", "notify_on_breach"], as_dict=True
		)
		if desk
		else None
	)
	return SLAPolicy(
		sla_days=level_days or classification.sla_days_for(service_category, grievance) or 0,
		auto_escalate=bool(flags.auto_escalate) if flags else True,
		notify_on_breach=bool(flags.notify_on_breach) if flags else True,
		holiday_list=get_default_holiday_list(),
	)


def start_clock(grievance):
	"""Stamp the SLA window onto the grievance. Idempotent."""
	if grievance.sla_due_date:
		return

	policy = resolve_policy(grievance.service_category, grievance)
	if not policy or not policy.sla_days:
		return

	started = get_datetime(grievance.sla_start_at or grievance.creation or now_datetime())
	due = calculate_working_deadline(started, policy.sla_days, holiday_list_name=policy.holiday_list)
	grievance.db_set(
		{
			"sla_days": policy.sla_days,
			"sla_start_at": started,
			"sla_due_date": due,
		},
		update_modified=False,
	)
	arm_escalation(grievance, policy=policy)
	oplog.emit(
		"sla.clock_started",
		**grievance_fields(grievance),
		sla_days=policy.sla_days,
		started_at=started,
		next_escalation_at=grievance.get("next_escalation_at"),
	)


def cleared_reminders():
	"""No reminder sent yet, for a window that starts again or a deadline that moves."""
	return {C.REMINDER_SENT_FIELD: 0}


def recalculate_sla_on_category_change(grievance, old_category=None, new_category=None):
	"""Recalculate SLA due date and escalation timestamps when category changes.

	Recalculates target resolution time based on the new category's policy starting from
	the creation / start date, taking into account any banked hold time and holidays.
	"""
	cat = new_category or grievance.service_category
	policy = resolve_policy(cat, grievance)
	if not policy or not policy.sla_days:
		return

	started = get_datetime(grievance.sla_start_at or grievance.creation or now_datetime())
	new_due = calculate_working_deadline(started, policy.sla_days, holiday_list_name=policy.holiday_list)

	banked = grievance.get("total_hold_time") or 0
	if banked:
		new_due = add_to_date(new_due, seconds=banked)

	updates = {
		"sla_days": policy.sla_days,
		"sla_start_at": started,
		"sla_due_date": new_due,
		**cleared_reminders(),
	}

	grievance.db_set(updates, update_modified=False)
	arm_escalation(grievance, policy=policy)
	oplog.emit(
		"sla.clock_recalculated",
		**grievance_fields(grievance),
		previous_category=old_category,
		new_category=cat,
		sla_days=policy.sla_days,
		banked_hold_seconds=banked or None,
	)


def follow_type_change(grievance):
	"""Re-length the SLA window when a case moves to another type of the same category.

	Only when the rule that matches the new type gives other SLA days than the case has: otherwise
	nothing moves, and the reminders already sent stay sent. The clock keeps its start.
	"""
	if not grievance.sla_due_date:
		return
	policy = resolve_policy(grievance.service_category, grievance)
	if policy and policy.sla_days and policy.sla_days != grievance.sla_days:
		recalculate_sla_on_category_change(grievance)


def reset_clock(grievance):
	"""Restart the SLA window from now when category changes on reassignment."""
	policy = resolve_policy(grievance.service_category, grievance)
	now = now_datetime()
	updates = {
		"sla_start_at": now,
		"total_hold_time": 0,
		**cleared_reminders(),
		"escalated": 0,
	}
	if policy and policy.sla_days:
		due = calculate_working_deadline(now, policy.sla_days, holiday_list_name=policy.holiday_list)
		updates["sla_days"] = policy.sla_days
		updates["sla_due_date"] = due
	else:
		updates["sla_days"] = 0
		updates["sla_due_date"] = None

	grievance.db_set(updates, update_modified=False)
	for k, v in updates.items():
		setattr(grievance, k, v)
	arm_escalation(grievance, policy=policy)
	oplog.emit("sla.clock_reset", **grievance_fields(grievance), sla_days=updates["sla_days"])


def start_level_window(grievance, level) -> bool:
	"""Start a fresh SLA window for the rung the case has just reached, if the rung has its own days.

	The Routing Matrix gives each rung its own working days (SLA L1, SLA L2). A case that moves
	one level up, by a missed deadline, a reopen or an officer's escalation, gets that rung's days
	from now: the reminders start again and its next escalation is the new deadline. Returns
	False and changes nothing when the rung has no days of its own for the case; the caller then
	keeps the rung's escalation hours.
	"""
	from oan_grievance_service.services import classification

	days = classification.level_sla_days(grievance.get("service_category"), grievance, level)
	if not days:
		return False
	policy = resolve_policy(grievance.get("service_category"), grievance, level=level)
	now = now_datetime()
	due = calculate_working_deadline(now, days, holiday_list_name=policy.holiday_list)
	updates = {
		"sla_days": days,
		"sla_start_at": now,
		"sla_due_date": due,
		"total_hold_time": 0,
		**cleared_reminders(),
		"next_escalation_at": due if policy.auto_escalate else None,
	}
	grievance.db_set(updates, update_modified=False)
	for fieldname, value in updates.items():
		setattr(grievance, fieldname, value)
	oplog.emit("sla.level_window_started", **grievance_fields(grievance), rung=level, sla_days=days)
	return True


def arm_escalation(grievance, policy=None):
	"""Point the escalation clock at the first bump.

	Until a case has escalated once the next bump is its SLA deadline, so this
	is re-run whenever the deadline moves (resume from hold, approved deferral, category change). Once
	the case starts climbing, the rung's own hours own the schedule and the deadline is
	no longer the thing being waited on.

	Turning the desk's `auto_escalate` off simply leaves the clock unarmed. That is the whole
	switch: the batch selects on `next_escalation_at`, so a null is invisible to it,
	and no per-case policy lookup is needed at escalation time. The SLA window, the
	reminders and the compliance reporting all carry on untouched.

	A case that has already escalated is left alone: its rung owns the schedule, and
	re-arming it from the start of the window would put the bump in the past. A case
	with no deadline (sla_days 0) has nothing to escalate against.
	"""
	if grievance.escalated or not grievance.sla_due_date:
		return

	policy = policy or resolve_policy(grievance.service_category, grievance)
	if not policy.auto_escalate:
		grievance.db_set("next_escalation_at", None, update_modified=False)
		grievance.next_escalation_at = None
		return

	# A case escalates automatically only once its full SLA window has elapsed (STG-516), so the
	# first bump is the deadline itself. The warning before the deadline is the SLA reminders
	# (C.SLA_REMINDER_THRESHOLDS), not an escalation.
	at = get_datetime(grievance.sla_due_date)
	grievance.db_set("next_escalation_at", at, update_modified=False)
	grievance.next_escalation_at = at


def open_hold_seconds(grievance):
	"""Seconds in the hold that is still running. Zero when the clock is not paused."""
	on_hold = grievance.get("on_hold_since")
	if not on_hold:
		return 0
	return max(0, int((now_datetime() - get_datetime(on_hold)).total_seconds()))


def sla_category_of(state):
	"""What the Grievance Workflow says a state does to the clock. Unset means Running."""
	for row in get_workflow("Grievance").states:
		if row.state == state:
			return row.get("sla_category") or RUNNING
	return RUNNING


def states_in_category(category):
	return {
		row.state
		for row in get_workflow("Grievance").states
		if (row.get("sla_category") or RUNNING) == category
	}


def open_states():
	"""States that do not Stop the clock, in workflow order.

	Read from the Grievance Workflow so a state added there counts without a code change.
	"""
	return list(
		dict.fromkeys(
			row.state
			for row in get_workflow("Grievance").states
			if (row.get("sla_category") or RUNNING) != STOPPED
		)
	)


def terminal_states():
	"""States the Grievance Workflow offers no way out of (Closed, Rejected)."""
	workflow = get_workflow("Grievance")
	outgoing = {row.state for row in workflow.transitions}
	return {row.state for row in workflow.states if row.state not in outgoing}


def awaiting_action_states():
	"""Open states where an officer owes the next move: those not Paused on the submitter."""
	waiting = states_in_category(PAUSED)
	return [state for state in open_states() if state not in waiting]


def on_status_change(grievance, to_state, from_state=None):
	"""Start, pause, resume or stop the clock for the state the case just entered.

	The clock starts when the case reaches a department. A Paused state holds it, and
	the deadline is pushed out by the hold when the case leaves. A Stopped state ends
	it: any hold is banked first, so the deadline the outcome is judged against stays
	fair, and escalation is disarmed. Only a reopen brings a case back from a Stopped
	state, and it starts the window again.
	"""
	if to_state == C.STATE_ASSIGNED:
		start_clock(grievance)

	category = sla_category_of(to_state)
	if category == PAUSED:
		if grievance.sla_due_date and not grievance.on_hold_since:
			grievance.db_set("on_hold_since", now_datetime(), update_modified=False)
			oplog.emit("sla.clock_paused", **grievance_fields(grievance), state=to_state)
		return

	resume_clock(grievance)
	if category == STOPPED:
		# Only the move that stops the clock is an outcome. Resolved to Closed (a manual close or
		# the auto-close two days later) moves between Stopped states, and judging it again
		# would count a case that met its deadline as breached once the auto-close ran late.
		entered_stopped = not (from_state and sla_category_of(from_state) == STOPPED)
		if entered_stopped and grievance.sla_due_date:
			# Whether the case met its deadline is judged here, once, after any hold is banked.
			oplog.emit(
				"sla.clock_stopped",
				**grievance_fields(grievance),
				state=to_state,
				breached=get_datetime(grievance.sla_due_date) < now_datetime(),
			)
		# Also on the way from one Stopped state to the next (Resolved to Closed): the
		# escalation was disarmed on entering the first and must stay so.
		if grievance.next_escalation_at:
			grievance.db_set("next_escalation_at", None, update_modified=False)
			grievance.next_escalation_at = None
	elif from_state and sla_category_of(from_state) == STOPPED:
		# A reopened case gets a fresh window: the first round's deadline, hold time
		# and reminders belong to the work that was already done.
		reset_clock(grievance)


def state_timer(state):
	"""(hours, on_expiry) for a state, or None when it has no timer.

	Read from the Grievance Deferral Policy's State Timers, which start with the
	`DEFAULT_STATE_TIMERS` rows.
	"""
	row = frappe.db.get_value(
		"Grievance State Timer",
		{
			"parenttype": "Grievance Deferral Policy",
			"parent": "Grievance Deferral Policy",
			"workflow_state": state,
		},
		["hours", "on_expiry"],
		as_dict=True,
	)
	return (row.hours, row.on_expiry) if row else None


def arm_state_timer(grievance, state):
	"""Stamp when the case's new state runs out, or clear the previous state's deadline."""
	timer = state_timer(state)
	deadline = add_to_date(now_datetime(), hours=timer[0]) if timer else None
	if deadline or grievance.get("state_deadline"):
		grievance.db_set("state_deadline", deadline, update_modified=False)


def pause_view(case, timer_cache=None):
	"""What a response says about a held clock, from fields the case already stores.

	`case` is a Grievance or a row of its fields (service_category, status, sla_due_date,
	on_hold_since, state_deadline). Nothing here is stored: the hold is banked by
	`resume_clock`, and this only reads ahead of it.

	`auto_close_at` is the state's deadline, but only when that state's timer closes the
	case; an Escalate timer is not a close date. While the clock is held the due date is
	the latest it can reach: the farmer replying at the last moment adds the rest of the
	wait to it, and a case that is never answered closes instead. `timer_cache` spares a
	list the repeat lookups of one state.
	"""
	state = case.get("workflow_state") or case.get("status")
	cache = timer_cache if timer_cache is not None else {}
	if state not in cache:
		timer = state_timer(state)
		cache[state] = bool(timer and timer[1] == "Auto Close")
	deadline = case.get("state_deadline") if cache[state] else None
	due = case.get("sla_due_date")
	held_since = case.get("on_hold_since")
	if held_since and due and deadline:
		held = max(0, (get_datetime(deadline) - get_datetime(held_since)).total_seconds())
		due = add_to_date(get_datetime(due), seconds=held)
	return {
		"is_paused": bool(held_since),
		"paused_since": held_since,
		"auto_close_at": deadline,
		"sla_due_date": due,
	}


def deadline_view(case, timer_cache=None):
	"""The one deadline a row shows, and whose move the case is waiting on.

	A case waiting on the submitter (a held clock, or a resolved case awaiting
	confirmation) is judged against the date it auto-closes; a case waiting on an
	officer, against its SLA due date. A closed or rejected case has neither.
	`overdue_seconds` is how far past that date it already is, None until then.
	"""
	state = case.get("workflow_state") or case.get("status")
	category = sla_category_of(state)
	auto_close_at = pause_view(case, timer_cache)["auto_close_at"]
	if auto_close_at:
		awaiting, due, kind = "submitter", auto_close_at, "auto_close"
	elif category == PAUSED:
		awaiting, due, kind = "submitter", None, None
	elif category == RUNNING:
		awaiting, due, kind = "officer", case.get("sla_due_date"), "sla_breach"
	else:
		awaiting, due, kind = None, None, None

	late = (now_datetime() - get_datetime(due)).total_seconds() if due else 0
	return {
		"awaiting": awaiting,
		"due_date": due,
		"due_kind": kind if due else None,
		"overdue_seconds": int(late) if late > 0 else None,
	}


def resume_clock(grievance):
	"""Bank the hold and push the deadline out by the same amount.

	Two writes total per hold: one on pause, one here. The reminder flags
	are deliberately left alone, because consumed percentage is unchanged across a resume:
	the window and the elapsed time both move by the hold duration.
	"""
	if not grievance.on_hold_since:
		return

	held = open_hold_seconds(grievance)
	updates = {
		"total_hold_time": (grievance.total_hold_time or 0) + held,
		"on_hold_since": None,
	}

	if held and grievance.sla_due_date:
		updates["sla_due_date"] = add_to_date(get_datetime(grievance.sla_due_date), seconds=held)
		# The escalation clock is pushed by the same amount rather than re-armed, so a
		# rung that was part-way through its own hours keeps the remainder instead of
		# being overtaken the moment the case comes off hold.
		if grievance.next_escalation_at:
			updates["next_escalation_at"] = add_to_date(
				get_datetime(grievance.next_escalation_at), seconds=held
			)

	grievance.db_set(updates, update_modified=False)
	oplog.emit("sla.clock_resumed", **grievance_fields(grievance), held_seconds=held)
	return held


def consumed_percent(grievance):
	"""The SLA tracker's consumed percentage, calculated hourly with upper limit (ceiling), excluding hold time."""
	start_val = grievance.get("sla_start_at")
	due_val = grievance.get("sla_due_date")
	if not (start_val and due_val):
		return 0
	start = get_datetime(start_val)
	due = get_datetime(due_val)
	banked = grievance.get("total_hold_time") or 0

	window_seconds = (due - start).total_seconds() - banked
	if window_seconds <= 0:
		return 100

	elapsed_seconds = (now_datetime() - start).total_seconds() - banked - open_hold_seconds(grievance)
	if elapsed_seconds <= 0:
		return 0

	# Hourly calculation taking upper limit (ceil)
	window_hours = max(1, math.ceil(window_seconds / 3600))
	elapsed_hours = math.ceil(elapsed_seconds / 3600)
	percent = math.ceil((elapsed_hours / window_hours) * 100)
	return max(0, min(percent, 999))


def higher_authority_of(
	user, department=None, administrative_area=None, log_unstaffed_for=None, category=None, level=None
):
	"""The person one step above `user` for a department and area, and their rung.

	1. The user's direct supervisor (reports_to), when they sit higher up the chain or
	   hold no rung at all.
	2. Otherwise whoever holds the next rung for the department and area.

	A user with no rung (a submitter, or nobody) enters at the most junior rung.
	`level` is the rung to climb from when the caller already knows it; otherwise the
	user's own rung is read.
	Returns (None, None) at the top of the chain; `log_unstaffed_for` names the
	grievance to report when a rung has nobody on it.
	"""
	chain = GrievanceRoleLevel.get_chain()
	if not chain:
		return None, None

	rank = {rung.name: index for index, rung in enumerate(chain)}
	current_level = level or current_level_of(user)
	current_rank = rank.get(current_level, -1)
	if current_level and current_level not in rank:
		# Sits on a rung that has since been deactivated: nowhere defined to climb to.
		return None, None
	if current_rank + 1 >= len(chain):
		return None, None
	supervisor = get_officer_supervisor(
		user, department=department, administrative_area=administrative_area, category=category
	)
	if supervisor and supervisor != user:
		sup_level = current_level_of(supervisor)
		if not sup_level:
			return supervisor, chain[current_rank + 1]
		if rank.get(sup_level, -1) > current_rank:
			return supervisor, chain[rank[sup_level]]

	for next_level in chain[current_rank + 1 :]:
		officer = find_officer_by_role_level(
			next_level.name, department=department, administrative_area=administrative_area, category=category
		)
		if officer and officer != user:
			return officer, next_level
		if not officer and log_unstaffed_for:
			oplog.log_error(
				title=f"Grievance escalation rung unstaffed: {next_level.name}",
				message=(
					f"No active Grievance RBAC Assignment holds role level '{next_level.name}' for "
					f"department '{department}' and administrative area '{administrative_area}'. "
					f"Grievance {log_unstaffed_for} skipped this rung."
				),
			)

	return None, None


def _first_escalation(grievance):
	"""`escalated_at` keeps the first escalation: a response clears the flag, and later
	rungs must not move the day the case first escalated."""
	return {} if grievance.get("escalated_at") else {"escalated_at": now_datetime()}


def escalate(grievance, reason=None, reassign=True, target=None, event=C.EVENT_SLA_BREACH):
	"""Move the case one rung up the chain and re-arm the clock.

	`escalated` stays a flag, never a status, so the lifecycle stage is untouched (the API shows
	an escalated case as Escalated while it waits on an officer). Returns the user the case was
	handed to, or None when it could not move.

	`target` is an officer the caller has already checked is at the next level up (an officer's
	own escalation); otherwise the senior is found as for a breach. When the rung the case lands
	on has its own SLA days, the case gets a fresh window of them (`start_level_window`);
	otherwise the rung's escalation hours set the next bump.

	`event` is the notification the receiving officer gets. For a breach, the `notify_on_breach`
	of the desk that routed the case decides whether it is queued; an officer's escalation is
	always told. The escalation, its timeline entry and the reassignment happen either way.
	"""
	from oan_grievance_service.services import notifications

	notify = (
		event != C.EVENT_SLA_BREACH or resolve_policy(grievance.service_category, grievance).notify_on_breach
	)

	previous_assignee = grievance.assigned_to
	if target:
		rung = rung_on_case(target, grievance)
		level = next((row for row in GrievanceRoleLevel.get_chain() if row.name == rung), None)
		if not level:
			frappe.throw(_("{0} holds no level on this grievance.").format(target), frappe.ValidationError)
	else:
		# A senior who is on leave, disabled or, on a sensitive case, Kebele-level is passed over
		# for the rung above (`can_take_escalation`); with nobody available above, the case stays.
		target, level = available_senior(grievance, grievance.assigned_to)
	if not target:
		changes = {"escalated": 1, **_first_escalation(grievance)}
		audit.record_version(grievance, changes)
		grievance.db_set({**changes, "next_escalation_at": None}, update_modified=False)
		grievance.escalated = 1
		grievance.next_escalation_at = None

		body = "Case escalated (no higher authority configured for reassignment)"
		if reason:
			body += f": {reason}"
		GrievanceTimeline.record(
			grievance=grievance.name,
			entry_type="escalation",
			is_internal=False,
			body=body,
			author_user=frappe.session.user if frappe.session.user != "Guest" else None,
		)

		oplog.emit(
			"escalation.no_higher_authority",
			logging.WARNING,
			**grievance_fields(grievance),
			reason=reason,
			escalated_at=grievance.get("escalated_at"),
			notified=bool(notify and grievance.assigned_to),
		)
		if notify and grievance.assigned_to:
			notifications.queue(grievance, event, recipient_override=grievance.assigned_to)
		return grievance.assigned_to or True

	changes = {"escalated": 1, **_first_escalation(grievance)}
	if reassign:
		changes["assigned_to"] = target
	audit.record_version(grievance, changes)
	grievance.db_set(changes, update_modified=False)
	grievance.escalated = 1
	if reassign:
		grievance.assigned_to = target
	# The rung the case just landed on owns the next deadline: its own SLA days when it has them,
	# else its escalation hours. No hours means this is a terminal rung and the ladder stops here.
	if not (reassign and start_level_window(grievance, level.name)):
		hours = level.escalation_hours or 0
		next_at = add_to_date(now_datetime(), hours=hours) if hours else None
		grievance.db_set("next_escalation_at", next_at, update_modified=False)
		grievance.next_escalation_at = next_at
	next_at = grievance.next_escalation_at

	level_role = level.level_name or level.name
	body = f"Case escalated to {level_role}"
	if reason:
		body += f": {reason}"
	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="escalation",
		is_internal=False,
		body=body,
		author_user=frappe.session.user if frappe.session.user != "Guest" else None,
	)

	oplog.emit(
		"escalation.escalated",
		**grievance_fields(grievance),
		reason=reason,
		rung=level_role,
		from_user=previous_assignee,
		to_user=target,
		reassigned=bool(reassign),
		escalated_at=grievance.get("escalated_at"),
		next_escalation_at=next_at,
		notified=bool(notify),
	)
	if notify:
		notifications.queue(grievance, event, recipient_override=target)
	return target


# Reopen with escalation (STG-521)
# --------------------------------
# A reopened case is escalated: it goes to the officer one rung above whoever resolved it last,
# never back to that officer. Each reopen climbs from the most recent resolver, so repeated
# reopens keep moving up.


def rung_on_case(user, grievance):
	"""The highest rung `user` holds on a desk covering the case; else their highest rung anywhere,
	or None. One query over the user's live desk rows.

	The fallback covers an officer the case was reassigned to from outside its area.
	"""
	if not user:
		return None
	rows = query_active_officer_assignments(
		user=user,
		fields=lambda c, p, u: [
			c.role_level,
			p.department_scope,
			p.category_scope,
			c.administrative_area.as_("administrative_area_scope"),
		],
		order_by=None,
	)
	rank = {rung.name: index for index, rung in enumerate(GrievanceRoleLevel.get_chain())}
	covering = covering_rows(
		rows,
		grievance.get("assigned_dept"),
		grievance.get("service_category"),
		grievance.get("administrative_area"),
	)
	for candidates in (covering, rows):
		levels = [row.role_level for row in candidates if row.role_level in rank]
		if levels:
			return max(levels, key=rank.get)
	return None


def can_take_escalation(user, grievance):
	"""Whether `user` may be handed the case now: enabled, not on leave, and, for a sensitive
	case, not a Kebele-level officer.

	Leave is read from the user's live desk rows (row and desk active, desk within its effective
	dates), as routing reads the row itself: `positions.is_officer_on_leave` sees only the rows
	that name a position, and a row on a retired desk says nothing about today.
	"""
	from oan_grievance_service import permissions

	if not user or not frappe.db.get_value("User", user, "enabled"):
		return False
	rows = query_active_officer_assignments(
		user=user, fields=lambda c, p, u: [c.on_leave], order_by=None, include_reviewers=True
	)
	if any(row.on_leave for row in rows):
		return False
	return not (grievance.get("is_sensitive") and permissions.is_kebele_level_officer(user))


def available_senior(grievance, user, level=None, passed_over=()):
	"""(officer, rung): the first officer above `user` who can take the case, or (None, None).

	Climbs from `user`'s rung, or from `level` when given (`higher_authority_of`, which also skips
	vacant rungs). A senior who cannot take the case now (`can_take_escalation`) is passed over for
	the rung above them, and so is anyone in `passed_over` and `user` themself. Shared by breach
	escalation and reopen escalation.
	"""
	passed_over = {user, *passed_over}
	# Each step climbs at least one rung, so the walk is no longer than the chain.
	for _rung in GrievanceRoleLevel.get_chain():
		target, rung = higher_authority_of(
			user,
			department=grievance.assigned_dept,
			administrative_area=grievance.administrative_area,
			log_unstaffed_for=grievance.name,
			category=grievance.service_category,
			level=level,
		)
		if not target:
			return None, None
		if target not in passed_over and can_take_escalation(target, grievance):
			return target, rung
		passed_over.add(target)
		user, level = target, rung.name
	return None, None


def senior_for_reopen(grievance, resolver):
	"""(base, target, level): who the climb starts from, the officer above them, and that rung.

	The climb starts from the resolver's rung. A resolver with no rung (an admin, say) gives way
	to the case's officer. `base` is None when neither holds a rung. The resolver is never the
	target. `target` is None when nobody above is available (`available_senior`).
	"""
	base, level = resolver, rung_on_case(resolver, grievance)
	if not level and grievance.assigned_to and grievance.assigned_to != resolver:
		base, level = grievance.assigned_to, rung_on_case(grievance.assigned_to, grievance)
	if not level:
		return None, None, None
	target, rung = available_senior(grievance, base, level=level, passed_over=(resolver,))
	return base, target, rung


def escalate_on_reopen(grievance):
	"""Hand a reopened case to the officer above its last resolver.

	The case is flagged escalated and gets a new SLA window: the days of the rung it lands on when
	that rung has its own (`start_level_window`), else the window the reopen started. The officer
	works to that deadline, and the breach ladder climbs from there. The submitter's reason is on
	the reopen's own timeline entry, so it is not repeated here.

	When nobody above is available (the resolver holds the top rung, or every rung above is vacant
	or on leave), the case stays with the officer the climb started from, falling back to its
	current officer. Which of these the business wants is still open (see the ticket). Either way
	the escalation is recorded.

	The officer holding the case afterwards gets the one Grievance Reopened notification: the
	caller takes the Reopen move with `notify=False`, so nobody the case has left is told.

	Returns the user the case is now assigned to.
	"""
	from oan_grievance_service.services import notifications

	# Whoever took the most recent Resolve; None for a move no user is recorded on.
	resolver = frappe.db.get_value(
		"Grievance Status History",
		{"grievance": grievance.name, "to_status": C.STATE_RESOLVED},
		"changed_by",
		order_by="timestamp desc, creation desc",
	)
	base, target, level = senior_for_reopen(grievance, resolver)
	previous_assignee = grievance.assigned_to

	changes = {"escalated": 1, **_first_escalation(grievance)}
	if target:
		changes["assigned_to"] = target
		body = _("Reopened with escalation: case escalated to {0}").format(level.level_name or level.name)
	else:
		if base and base != previous_assignee and can_take_escalation(base, grievance):
			changes["assigned_to"] = base
		body = _(
			"Reopened with escalation: no higher authority is available, so the case stays at this level"
		)
	audit.record_version(grievance, changes)
	grievance.db_set(changes, update_modified=False)
	assignee = grievance.assigned_to
	# The reopen started a window on the resolver's rung; the case now runs on the rung it sits on.
	final_level = level.name if target else rung_on_case(assignee, grievance)
	if final_level:
		start_level_window(grievance, final_level)

	GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="escalation",
		is_internal=False,
		body=body,
		author_user=frappe.session.user if frappe.session.user != "Guest" else None,
	)
	oplog.emit(
		"escalation.reopen_escalated" if target else "escalation.reopen_no_higher_authority",
		logging.INFO if target else logging.WARNING,
		**grievance_fields(grievance),
		resolver=resolver,
		from_user=previous_assignee,
		to_user=assignee,
		rung=(level.level_name or level.name) if target else None,
		reassigned=assignee != previous_assignee,
	)
	if assignee:
		notifications.queue(grievance, C.EVENT_REOPENED, recipient_override=assignee)
	return assignee
