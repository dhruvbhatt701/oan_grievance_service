# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""Dashboard charts: every analytics query the API serves lives in this module.

Each chart is a builder over the rollups (services.dashboard_rollup), or, for the
few admin-only charts that must show a real case, a small indexed query over
Grievance. Three views share the builders:

- admin: `GET /api/v1/charts`, Grievance Admin; every chart, and live
  case detail where a chart has it.
- officer: `GET /api/v1/charts`, Grievance Officer; the public charts, narrowed to the
  caller's RBAC desks (see `caller_scope`). `grvByAdminLevel` also narrows the levels shown
  to the caller's own level and those below it.
- public: `GET /api/v1/charts/<chart_id>`, Grievance Dashboard Reader; only charts marked public,
  which are counts from the rollups and never name a case, a person or a text.

Results are cached per chart and parameters in Redis. A rollup chart's key
carries the rollup's as_of, so a refresh is a clean cut-over.
"""

import hashlib
import json
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import frappe
from frappe.query_builder.functions import Min, Sum
from frappe.utils import add_months, get_datetime, getdate, now_datetime
from oan_auth_service.api.utils import to_tz_aware_iso
from pypika.terms import Criterion

from oan_grievance_service import permissions as perms
from oan_grievance_service.grievance_masters.doctype.grievance_administrative_area.grievance_administrative_area import (
	area_names,
	country_area,
	rollup_keys_within,
	scope_areas,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import dashboard_rollup as rollup
from oan_grievance_service.services import sla

ROLLUP_TTL = 15 * 60
LIVE_TTL = 60
# Why a chart has no rows besides "there are no cases": see Context.unavailable.
NO_SNAPSHOT = "no_snapshot"
TITLE_LENGTH = 80
# The administrative levels grvByAdminLevel counts at, widest first. Federal is the whole
# country; Region and Woreda are the levels of the same name in the area tree.
LEVEL_FEDERAL, LEVEL_REGION, LEVEL_WOREDA = "federal", "region", "woreda"
LEVELS = (LEVEL_FEDERAL, LEVEL_REGION, LEVEL_WOREDA)


def status_order():
	"""Every state, in lifecycle order, so the donut always has the same
	segments whether or not a state currently holds a case."""
	return rollup.live_states()


@dataclass(frozen=True)
class Params:
	"""Chart parameters after validation. Empty tuples mean "all"."""

	region: tuple = ()
	service_category: tuple = ()
	assigned_dept: tuple = ()
	from_date: date | None = None
	to_date: date | None = None
	month: str | None = None
	# Only grvResolutionRateByRegion reads this. It is not `month`, which a page sends to
	# every chart in one request, so a region chart labelled "All time" is never moved by it.
	as_of_month: str | None = None
	granularity: str = "month"
	limit: int = 10
	# Only grvByAdminLevel reads this: which of LEVELS to return. Empty means all it may show.
	level: tuple = ()

	def used_by(self, chart):
		"""Only the parameters a chart reads, so the others do not split its cache."""
		values = asdict(self)
		return {name: values[name] for name in sorted(chart.params)}


FILTERS = frozenset({"region", "service_category", "assigned_dept"})
PERIOD = FILTERS | {"from_date", "to_date"}


@dataclass(frozen=True)
class Chart:
	build: callable
	params: frozenset = FILTERS
	public: bool = True
	# "live" charts read Grievance directly (admin view only) and cache briefly.
	live_for_admin: bool = False
	# Only Grievance Stat Area has a Woreda column, so only a chart that reads it can honour
	# a desk scoped to Woreda level; see get_chart.
	by_woreda: bool = False


class Context:
	"""What a builder needs besides the parameters: the view, the scope and the snapshot day."""

	def __init__(self, params, admin, scope=None):
		self.params = params
		self.admin = admin
		self.scope = scope
		self.today = getdate(now_datetime())
		self.snapshot_date = _latest_snapshot_date()
		# Set by a builder whose empty result means "no data was kept", not "no cases".
		self.unavailable = None


# Caller scope
# ------------


@dataclass(frozen=True)
class Clause:
	"""One RBAC desk, or one officer's own cases, as rollup filters. `regions` / `woredas` /
	`assigned_to` None means no limit on that.

	`level` is the widest administrative level the desk's area reaches: federal for a desk
	with no area (or the Country), region for a Region, woreda for a Zone or a Woreda.
	`assigned_to` is the officers whose assigned cases the clause covers; see `caller_scope`.
	"""

	regions: tuple | None = None
	service_category: str | None = None
	assigned_dept: str | None = None
	woredas: tuple | None = None
	level: str = LEVEL_FEDERAL
	assigned_to: tuple | None = None


def caller_scope(user=None):
	"""The rollup filters for what `user` may see: None for no restriction, else desk clauses.

	Mirrors permissions.grievance_query_conditions, as far as the rollups can. They are
	grouped by region, category, department and assignee (Grievance Stat Area also by woreda), so:

	- A Grievance Officer reads the cases assigned to them and, on each desk of theirs, the cases
	  of that desk held by an officer on a lower rung, as their case list does. A Category Admin
	  also reads their desks.
	  Without this an officer below the top rung read nothing, though their case list showed
	  cases.
	- A desk's area counts as the Regions inside it. A desk on a Zone or a Woreda has no
	  Region inside it and counts as the Woredas inside it instead, which only Grievance Stat Area
	  knows (see `Chart.by_woreda`). A desk below Woreda level contributes nothing rather
	  than its whole Woreda.
	- A desk narrowed by grievance type or service provider contributes nothing.
	- A case waiting on the officer's ruling on a change request is not counted.

	The last three err towards showing less, never more. A user with no usable desk and no
	assigned case reads zeros.
	"""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if roles & perms.UNRESTRICTED_ROLES:
		return None

	# The RBAC rows are read once, and so is each area's set of regions, whatever the clause.
	placements = perms.active_scopes(user)
	areas = scope_areas(placements)
	regions_in = {}

	def regions_of(area):
		if area.name not in regions_in:
			regions_in[area.name] = rollup_keys_within(C.AREA_LEVEL_REGION, area)
		return regions_in[area.name]

	# A Grievance Officer reads a desk only through the top rung; a Review Officer reads every desk.
	if perms.ROLE_REVIEW_OFFICER in roles:
		scopes = placements
	else:
		scopes = perms.officer_case_scopes(user, placements)

	def desk_clause(scope, assigned_to=None):
		"""The scope as a Clause, or None when the rollups cannot honour it."""
		if scope.get("grievance_type_scope") or scope.get("service_provider_scope"):
			return None
		regions = woredas = None
		level = LEVEL_FEDERAL
		if scope.get("administrative_area_scope"):
			area = areas.get(scope.administrative_area_scope)
			if not area:
				return None
			regions = regions_of(area)
			if regions:
				level = LEVEL_FEDERAL if area.level_name == C.AREA_LEVEL_COUNTRY else LEVEL_REGION
			else:
				regions, woredas = None, rollup_keys_within(C.AREA_LEVEL_WOREDA, area)
				level = LEVEL_WOREDA
				if not woredas:
					return None
		return Clause(
			regions,
			scope.get("category_scope") or None,
			scope.get("department_scope") or None,
			woredas,
			level,
			assigned_to,
		)

	clauses = []
	for scope in scopes:
		if not (scope.administrative_area_scope or scope.department_scope or scope.category_scope):
			continue
		if clause := desk_clause(scope):
			clauses.append(clause)
	if perms.ROLE_OFFICER in roles:
		# The officer's own cases, and on each desk the cases held below them, as the case list
		# reads them (`permissions.desk_teams`).
		clauses.append(Clause(level=_placement_level(placements, areas, regions_of), assigned_to=(user,)))
		# Seats are among the placements, so their areas are already read.
		for seat, team in perms.desk_teams(user):
			if clause := desk_clause(seat, tuple(sorted(team))):
				clauses.append(clause)
	return tuple(dict.fromkeys(clauses))


def _placement_level(placements, areas, regions_of):
	"""The widest administrative level of the places an officer is put on, whatever their rung.

	It is the level the cases assigned to them may be read at: their own and the ones below,
	never the country row for an officer placed on a Region, which would present their cases as
	the nation's. A placement with no area covers every region, as a desk with none does, so it
	is the widest. An officer placed nowhere reads Woreda counts only, the narrowest.
	"""
	widest = LEVEL_WOREDA
	for placement in placements:
		if not placement.administrative_area_scope:
			level = LEVEL_FEDERAL
		else:
			area = areas.get(placement.administrative_area_scope)
			if not area:
				continue
			if area.level_name == C.AREA_LEVEL_COUNTRY:
				level = LEVEL_FEDERAL
			elif regions_of(area):
				level = LEVEL_REGION
			else:
				level = LEVEL_WOREDA
		if LEVELS.index(level) < LEVELS.index(widest):
			widest = level
	return widest


def _apply_scope(query, table, scope):
	if scope is None:
		return query
	if not scope:
		# `name` is never null, so this matches no row and every chart reads as zero.
		return query.where(table.name.isnull())
	desks = []
	for clause in scope:
		parts = []
		if clause.regions is not None:
			parts.append(table.region.isin(clause.regions))
		if clause.woredas is not None:
			parts.append(table.woreda.isin(clause.woredas))
		if clause.service_category:
			parts.append(table.service_category == clause.service_category)
		if clause.assigned_dept:
			parts.append(table.assigned_dept == clause.assigned_dept)
		if clause.assigned_to is not None:
			# An empty IN () is a syntax error, and a clause for nobody covers no case.
			parts.append(
				table.assigned_to.isin(clause.assigned_to) if clause.assigned_to else table.name.isnull()
			)
		desks.append(Criterion.all(parts))
	return query.where(Criterion.any(desks))


# Reading the rollups
# -------------------


def _apply_filters(query, table, params, scope=None):
	query = _apply_scope(query, table, scope)
	if params.region:
		query = query.where(table.region.isin(params.region))
	if params.service_category:
		query = query.where(table.service_category.isin(params.service_category))
	if params.assigned_dept:
		query = query.where(table.assigned_dept.isin(params.assigned_dept))
	return query


def _latest_snapshot_date():
	return _snapshot_on_or_before(getdate(now_datetime()))


def _snapshot(ctx, group_by, on_date=None, params=None):
	"""Summed snapshot rows for one day (default: the latest), grouped by `group_by`."""
	day = on_date or ctx.snapshot_date
	if not day:
		return []
	S = frappe.qb.DocType(rollup.SNAPSHOT)
	columns = [getattr(S, name) for name in group_by]
	query = (
		frappe.qb.from_(S)
		.select(
			*columns,
			Sum(S.grievance_count).as_("grievances"),
			Sum(S.dup_pending_count).as_("dup_pending"),
			Min(S.oldest_open_creation).as_("oldest_open"),
		)
		.where(S.snapshot_date == day)
		# Only today's workflow states, so every chart adds up to the same total even
		# while rows from a retired state are still being migrated.
		.where(S.status.isin(status_order()))
	)
	query = _apply_filters(query, S, params or ctx.params, ctx.scope)
	if columns:
		query = query.groupby(*columns)
	return query.run(as_dict=True)


def _daily(ctx, start, end, group_by=("stat_date",)):
	"""Summed daily metrics for start <= stat_date <= end, grouped by `group_by`."""
	D = frappe.qb.DocType(rollup.DAILY)
	columns = [getattr(D, name) for name in group_by]
	query = (
		frappe.qb.from_(D)
		.select(*columns, *(Sum(getattr(D, m)).as_(m) for m in rollup.DAILY_METRICS))
		.where(D.stat_date >= start)
		.where(D.stat_date <= end)
	)
	query = _apply_filters(query, D, ctx.params, ctx.scope)
	if columns:
		query = query.groupby(*columns)
	return query.run(as_dict=True)


def _total(rows, metric):
	# SUM() comes back as Decimal, which will not mix with the floats rates are made of.
	return float(sum(row.get(metric) or 0 for row in rows))


def _by_status(rows):
	counts = dict.fromkeys(status_order(), 0)
	for row in rows:
		if row.status in counts:
			counts[row.status] += int(row.grievances or 0)
	return counts


def _pct(part, whole):
	return round(part * 100.0 / whole, 1) if whole else None


def _month_start(day):
	return day.replace(day=1)


def _week_start(day):
	return day - timedelta(days=day.weekday())


def _region_names():
	return dict(
		frappe.get_all(
			"Grievance Administrative Area",
			filters={"level_name": "Region"},
			fields=["code", "area_name"],
			as_list=True,
		)
	)


# Builders
# --------


def build_kpis(ctx):
	"""Total / Awaiting Action / Resolved / Escalated / Duplicates cards with their deltas."""
	by_status = _by_status(_snapshot(ctx, ("status",)))
	escalated = _total(
		[r for r in _snapshot(ctx, ("status", "escalated")) if r.escalated and r.status in sla.open_states()],
		"grievances",
	)
	total = sum(by_status.values())

	month_start = _month_start(ctx.today)
	week_start = _week_start(ctx.today)
	this_month = _daily(ctx, month_start, ctx.today, group_by=())
	this_week = _daily(ctx, week_start, ctx.today, group_by=())

	# The total at the end of last month is today's total less what was filed since.
	previous_total = total - int(_total(this_month, "submitted_count"))
	delta_pct = round((total - previous_total) * 100.0 / previous_total, 1) if previous_total > 0 else None
	return [
		{"metric": "total", "value": total, "delta_pct": delta_pct, "compare": "prev_month"},
		{
			"metric": "awaiting_action",
			"value": sum(by_status[s] for s in sla.awaiting_action_states()),
			"delta": int(_total(this_week, "submitted_count")),
			"compare": "this_week_new",
		},
		{
			"metric": "resolved",
			"value": sum(by_status[s] for s in C.RESOLVED_STATES),
			"delta": int(_total(this_week, "resolved_count")),
			"compare": "this_week_resolved",
		},
		{"metric": "escalated", "value": int(escalated)},
		{"metric": "duplicates", "value": by_status[C.STATE_DUPLICATE]},
	]


def _performance(ctx, start, end, snapshot_day):
	events = _daily(ctx, start, end, group_by=())
	resolved = _total(events, "resolved_count")
	submitted = _total(events, "submitted_count")
	feedback = _total(events, "feedback_count")
	by_status = _by_status(_snapshot(ctx, ("status",), on_date=snapshot_day)) if snapshot_day else None
	return {
		"avg_resolution_time": (
			round(float(_total(events, "resolution_hours_sum")) / resolved / 24, 1) if resolved else None
		),
		"resolution_rate": (
			_pct(sum(by_status[s] for s in C.RESOLVED_STATES), sum(by_status.values())) if by_status else None
		),
		# Of the cases filed in the period, the share escalated since: both counts are of the
		# same cases, so the rate cannot pass 100%.
		"escalation_rate": _pct(_total(events, "escalated_count"), submitted),
		"satisfaction": _pct(_total(events, "feedback_satisfied_count"), feedback),
		"basis": int(feedback),
	}


def _snapshot_on_or_before(day):
	S = frappe.qb.DocType(rollup.SNAPSHOT)
	row = (
		frappe.qb.from_(S)
		.select(S.snapshot_date)
		.where(S.snapshot_date <= day)
		.orderby(S.snapshot_date, order=frappe.qb.desc)
		.limit(1)
		.run()
	)
	return row[0][0] if row else None


def _month_bounds(ctx, month=None):
	"""First and last day of `month` (default this one); the last is never after today."""
	month_start = getdate(f"{month}-01") if month else _month_start(ctx.today)
	month_end = min(month_start.replace(day=monthrange(month_start.year, month_start.month)[1]), ctx.today)
	return month_start, month_end


def _readable_snapshot_day(ctx, day):
	"""`day`, or None when the caller's scope cannot read that day's snapshot.

	Only today's snapshot rows are split by assignee: when a day ends, its rows are folded to one
	per case group (see dashboard_rollup._compact_history) so the retained history does not grow
	with the number of officers. A scope made only of assigned-case clauses has nothing to read
	on a past day, and reading zeros there would pass for data, so it gets None, the same
	"no data" a month before the snapshots began gets. A scope with a desk reads the desk's
	cases on a past day: less than the officer's case list, never more.
	"""
	assigned_only = bool(ctx.scope) and all(clause.assigned_to is not None for clause in ctx.scope)
	if day and day < ctx.today and assigned_only:
		return None
	return day


def _month_snapshot_day(ctx, month_start, month_end):
	"""The snapshot that stood at the end of the month, or None when none was kept within it, or
	when the caller's scope cannot read it."""
	day = _snapshot_on_or_before(month_end)
	return _readable_snapshot_day(ctx, day if day and day >= month_start else None)


def build_performance_kpis(ctx):
	"""Avg Resolution Time / Resolution Rate / Escalation Rate / Satisfaction for a month."""
	month_start, month_end = _month_bounds(ctx, ctx.params.month)
	prev_start = getdate(add_months(month_start, -1))
	prev_end = month_start - timedelta(days=1)

	current = _performance(
		ctx, month_start, month_end, _readable_snapshot_day(ctx, _snapshot_on_or_before(month_end))
	)
	# A previous month from before the rollups existed has no snapshot to compare with.
	previous = _performance(ctx, prev_start, prev_end, _month_snapshot_day(ctx, prev_start, prev_end))

	def delta(metric):
		if current[metric] is None or previous[metric] is None:
			return None
		return round(current[metric] - previous[metric], 1)

	return [
		{
			"metric": "avg_resolution_time",
			"value": current["avg_resolution_time"],
			"unit": "days",
			"delta": delta("avg_resolution_time"),
		},
		{
			"metric": "resolution_rate",
			"value": current["resolution_rate"],
			"unit": "percent",
			"delta": delta("resolution_rate"),
		},
		{
			"metric": "escalation_rate",
			"value": current["escalation_rate"],
			"unit": "percent",
			"delta": delta("escalation_rate"),
		},
		{
			"metric": "satisfaction",
			"value": current["satisfaction"],
			"unit": "percent",
			"basis": current["basis"],
		},
	]


def _periods(start, end, granularity):
	"""The period keys from start to end inclusive, and a function from a day to its key."""
	if granularity == "week":
		first = _week_start(start)
		keys = []
		day = first
		while day <= end:
			keys.append(day.isoformat())
			day += timedelta(days=7)
		return keys, lambda d: _week_start(d).isoformat()
	keys = []
	day = _month_start(start)
	while day <= end:
		keys.append(day.strftime("%Y-%m"))
		day = getdate(add_months(day, 1))
	return keys, lambda d: d.strftime("%Y-%m")


def _bucketed(ctx, start, end, granularity, metrics):
	keys, key_of = _periods(start, end, granularity)
	buckets = {key: dict.fromkeys(metrics, 0) for key in keys}
	for row in _daily(ctx, start, end):
		bucket = buckets.get(key_of(getdate(row.stat_date)))
		if bucket is not None:
			for metric in metrics:
				bucket[metric] += int(row[metric] or 0)
	return keys, buckets


def build_monthly_trend(ctx):
	"""Filed and resolved per month; six months by default, empty months as zeros."""
	start = ctx.params.from_date or getdate(add_months(_month_start(ctx.today), -5))
	end = ctx.params.to_date or ctx.today
	keys, buckets = _bucketed(ctx, start, end, "month", ("submitted_count", "resolved_count"))
	return [
		{"month": k, "submitted": buckets[k]["submitted_count"], "resolved": buckets[k]["resolved_count"]}
		for k in keys
	]


def build_weekly_trend(ctx):
	"""Received and resolved per ISO week (Monday start); seven weeks by default."""
	start = ctx.params.from_date or (_week_start(ctx.today) - timedelta(weeks=6))
	end = ctx.params.to_date or ctx.today
	keys, buckets = _bucketed(ctx, start, end, "week", ("submitted_count", "resolved_count"))
	return [
		{"week_start": k, "received": buckets[k]["submitted_count"], "resolved": buckets[k]["resolved_count"]}
		for k in keys
	]


def build_net_backlog_trend(ctx):
	"""Filed, resolved and the open backlog at the end of each period.

	Worked backwards from today's open count: the backlog at the end of a period is
	today's, less everything filed since, plus everything resolved or rejected since.
	"""
	granularity = ctx.params.granularity
	end = ctx.today
	if ctx.params.from_date:
		start = ctx.params.from_date
	elif granularity == "week":
		start = _week_start(end) - timedelta(weeks=5)
	else:
		start = getdate(add_months(_month_start(end), -5))

	metrics = ("submitted_count", "resolved_count", "rejected_count")
	keys, buckets = _bucketed(ctx, start, end, granularity, metrics)
	open_now = sum(_by_status(_snapshot(ctx, ("status",)))[s] for s in sla.open_states())

	rows = []
	backlog = open_now
	for key in reversed(keys):
		bucket = buckets[key]
		rows.append(
			{
				"period": key,
				"submitted": bucket["submitted_count"],
				"resolved": bucket["resolved_count"],
				"backlog": max(backlog, 0),
			}
		)
		backlog -= bucket["submitted_count"] - bucket["resolved_count"] - bucket["rejected_count"]
	return list(reversed(rows))


def build_status_distribution(ctx):
	"""Every state, zeros included, in lifecycle order."""
	return [{"status": s, "count": n} for s, n in _by_status(_snapshot(ctx, ("status",))).items()]


def build_by_category(ctx):
	rows = _snapshot(ctx, ("service_category",))
	return sorted(
		({"category": r.service_category, "count": int(r.grievances or 0)} for r in rows if r.grievances),
		key=lambda r: (-r["count"], r["category"] or ""),
	)


def _category_window(ctx):
	"""The period the category charts cover: from to to, default the last six months."""
	start = ctx.params.from_date or getdate(add_months(_month_start(ctx.today), -5))
	return start, ctx.params.to_date or ctx.today


def build_category_resolution(ctx):
	"""Filed and resolved per category in the window, resolved split by SLA outcome."""
	rows = _daily(ctx, *_category_window(ctx), group_by=("service_category",))
	result = [
		{
			"category": r.service_category,
			"filed": int(r.submitted_count or 0),
			"resolved": int(r.resolved_count or 0),
			"resolved_on_time": int(r.resolved_on_time_count or 0),
			"resolved_breached": int(r.resolved_breached_count or 0),
		}
		for r in rows
	]
	return sorted(result, key=lambda r: (-r["filed"], r["category"] or ""))


def build_avg_resolution_by_category(ctx):
	"""Average days from filing to resolution per category, for the cases resolved in the window.

	Summed hours over summed resolved cases, so a category's average weighs each case
	equally however the cases split across regions and departments. A category with no
	resolved case in the window has no average and is left out.
	"""
	rows = _daily(ctx, *_category_window(ctx), group_by=("service_category",))
	result = [
		{
			"category": r.service_category,
			"resolved": int(r.resolved_count),
			"avg_resolution_days": round(float(r.resolution_hours_sum or 0) / int(r.resolved_count) / 24, 1),
		}
		for r in rows
		if r.resolved_count
	]
	return sorted(result, key=lambda r: (-r["resolved"], r["category"] or ""))


def build_performance_score(ctx):
	"""The six System Performance Score axes, each 0 to 100, for a month. 100 means:

	- timeliness: every resolved case that has an SLA was resolved by its due date.
	- resolution_rate: every case is Resolved or Closed, as at the end of the month.
	- satisfaction: every rating given was 4 or 5.
	- coverage: every case, as at the end of the month, sits with a real desk, not none
	  and not the Unrouted catch-all. A desk with no department still counts: see
	  dashboard_rollup._HAS_DESK.
	- transparency: every case filed in the month has had a public officer update.
	- follow_up: every case resolved in the month was confirmed (rated) by its submitter.

	`basis` is the number of cases the score is a share of. A score is None when there are
	none, so the chart can show a gap rather than a false zero. The axes come in the order
	the radar reads, clockwise from the top.
	"""
	month_start, month_end = _month_bounds(ctx, ctx.params.month)
	events = _daily(ctx, month_start, month_end, group_by=())
	on_time = _total(events, "resolved_on_time_count")
	with_sla = on_time + _total(events, "resolved_breached_count")
	submitted = _total(events, "submitted_count")
	resolved = _total(events, "resolved_count")
	feedback = _total(events, "feedback_count")

	cases = resolved_cases = routed = 0
	day = _month_snapshot_day(ctx, month_start, month_end)
	if day:
		for r in _snapshot(ctx, ("status", "has_desk"), on_date=day):
			n = int(r.grievances or 0)
			cases += n
			if r.status in C.RESOLVED_STATES:
				resolved_cases += n
			if r.has_desk:
				routed += n

	def axis(name, part, whole):
		return {"axis": name, "score": _pct(part, whole), "basis": int(whole)}

	return [
		axis("timeliness", on_time, with_sla),
		axis("resolution_rate", resolved_cases, cases),
		axis("satisfaction", _total(events, "feedback_satisfied_count"), feedback),
		axis("transparency", _total(events, "informed_count"), submitted),
		axis("follow_up", feedback, resolved),
		axis("coverage", routed, cases),
	]


def build_resolution_rate_by_region(ctx):
	"""Resolved or Closed over all cases per region: as at today, or at the end of `as_of_month`.

	A month with no snapshot kept inside it has no history to read. It returns no rows,
	as a month with no cases does, but marks the chart `unavailable` so a client can tell
	the two apart.
	"""
	names = _region_names()
	totals = {}
	snapshot_day = None
	if ctx.params.as_of_month:
		snapshot_day = _month_snapshot_day(ctx, *_month_bounds(ctx, ctx.params.as_of_month))
		if not snapshot_day:
			ctx.unavailable = NO_SNAPSHOT
			return []
	for r in _snapshot(ctx, ("region", "status"), on_date=snapshot_day):
		if not r.region:
			continue
		entry = totals.setdefault(r.region, {"resolved": 0, "total": 0})
		entry["total"] += int(r.grievances or 0)
		if r.status in C.RESOLVED_STATES:
			entry["resolved"] += int(r.grievances or 0)
	rows = [
		{
			"region_code": code,
			"region_name": names.get(code, code),
			"resolved": v["resolved"],
			"total": v["total"],
			"rate": _pct(v["resolved"], v["total"]),
		}
		for code, v in totals.items()
	]
	return sorted(rows, key=lambda r: (-r["total"], r["region_code"]))


def build_sla_risk(ctx):
	"""At risk (due within 24 hours), breached, and escalated; a case may be in several."""
	counts = {"at_risk": 0, "breached": 0, "escalated": 0}
	for r in _snapshot(ctx, ("status", "sla_state", "escalated")):
		if r.status not in sla.open_states():
			continue
		n = int(r.grievances or 0)
		if r.sla_state in ("at_risk", "breached"):
			counts[r.sla_state] += n
		if r.escalated:
			counts["escalated"] += n
	return [{"bucket": bucket, "count": count} for bucket, count in counts.items()]


def build_pending_duplicates(ctx):
	return [{"count": int(_total(_snapshot(ctx, ()), "dup_pending"))}]


def _age_days(created, now):
	return (now - get_datetime(created)).days


def build_oldest_open(ctx):
	"""The longest-open case that is not escalated: its age, and for an admin, which case."""
	now = now_datetime()
	if ctx.admin:
		rows = _live_grievances(ctx, statuses=sla.open_states(), escalated=0, limit=1, order="asc")
		return [
			{
				"ticket_number": r.ticket_number,
				"title": _title(r.description),
				"status": r.status,
				"age_days": _age_days(r.creation, now),
				"created_at": to_tz_aware_iso(r.creation),
			}
			for r in rows
		]
	rows = _snapshot(ctx, ())
	oldest = rows[0].oldest_open if rows else None
	if not oldest:
		return []
	return [{"age_days": _age_days(oldest, now), "created_at": to_tz_aware_iso(oldest)}]


def build_recent(ctx):
	names = _region_names()
	rows = _live_grievances(ctx, limit=ctx.params.limit, order="desc")
	return [
		{
			"ticket_number": r.ticket_number,
			"title": _title(r.description),
			"status": r.status,
			"service_category": r.service_category,
			"submitter_display": None if r.is_anonymous else r.submitter_name,
			"area_name": r.area_name,
			"region_code": r.region_code,
			"region_name": names.get(r.region_code),
			"created_at": to_tz_aware_iso(r.creation),
		}
		for r in rows
	]


def _visible_levels(ctx):
	"""The levels the caller may read: their own and the ones below it, never a wider one.

	A wider level would count cases outside the caller's desks as the same row, so a
	Region desk reads Region and Woreda counts, a Woreda desk only Woreda counts, and an
	officer on no usable desk none. The unscoped views read all three.
	"""
	if ctx.scope is None:
		return LEVELS
	if not ctx.scope:
		return ()
	widest = min(LEVELS.index(clause.level) for clause in ctx.scope)
	return LEVELS[widest:]


def _area_counts(ctx):
	"""Today's cases per (region, woreda) after the caller's scope and the filters."""
	A = frappe.qb.DocType(rollup.AREA)
	query = frappe.qb.from_(A).select(A.region, A.woreda, Sum(A.grievance_count).as_("grievances"))
	return _apply_filters(query, A, ctx.params, ctx.scope).groupby(A.region, A.woreda).run(as_dict=True)


def build_by_admin_level(ctx):
	"""Grievance counts at each administrative level: federal, per region and per woreda.

	Every count is over the same cases the caller's scope and filters leave, so the federal
	row is the sum the region rows split, plus any case filed under no region. Regions and
	woredas holding no case are left out, as an empty category is. A case filed above
	Woreda level (on a Region or Zone) counts at federal and region level only.
	"""
	rows = _area_counts(ctx)
	visible = [lvl for lvl in _visible_levels(ctx) if not ctx.params.level or lvl in ctx.params.level]

	by_region, by_woreda, woreda_region = {}, {}, {}
	for r in rows:
		n = int(r.grievances or 0)
		if r.region:
			by_region[r.region] = by_region.get(r.region, 0) + n
		if r.woreda:
			by_woreda[r.woreda] = by_woreda.get(r.woreda, 0) + n
			woreda_region[r.woreda] = r.region or None

	result = []
	if LEVEL_FEDERAL in visible:
		country = country_area() or {"code": None, "area_name": None}
		result.append(
			{
				"level": LEVEL_FEDERAL,
				"code": country["code"],
				"name": country["area_name"],
				"region_code": None,
				"count": sum(int(r.grievances or 0) for r in rows),
			}
		)
	if LEVEL_REGION in visible:
		names = _region_names()
		result += [
			{
				"level": LEVEL_REGION,
				"code": code,
				"name": names.get(code, code),
				"region_code": code,
				"count": n,
			}
			for code, n in sorted(by_region.items(), key=lambda kv: (-kv[1], kv[0]))
			if n
		]
	if LEVEL_WOREDA in visible:
		names = area_names(C.AREA_LEVEL_WOREDA, by_woreda)
		result += [
			{
				"level": LEVEL_WOREDA,
				"code": code,
				"name": names.get(code, code),
				"region_code": woreda_region[code],
				"count": n,
			}
			for code, n in sorted(by_woreda.items(), key=lambda kv: (-kv[1], kv[0]))
			if n
		]
	return result


def _filter_options(ctx, dimension, key, names=None):
	# Options list every value, so they ignore the filters the caller has set.
	rows = _snapshot(ctx, (dimension,), params=Params())
	result = []
	for r in rows:
		value = r[dimension]
		if not value or not r.grievances:
			continue
		entry = {key: value, "grievances": int(r.grievances)}
		if names is not None:
			entry["region_name"] = names.get(value, value)
		result.append(entry)
	return sorted(result, key=lambda e: (-e["grievances"], e[key]))


def build_filter_regions(ctx):
	return _filter_options(ctx, "region", "region_code", names=_region_names())


def build_filter_categories(ctx):
	return _filter_options(ctx, "service_category", "category")


def build_filter_departments(ctx):
	return _filter_options(ctx, "assigned_dept", "department")


# Live reads (admin view only)
# ----------------------------


def _title(description):
	text = " ".join((description or "").split())
	return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip() + "…"


def _live_grievances(ctx, statuses=None, escalated=None, limit=10, order="desc"):
	"""A handful of cases straight from Grievance, newest or oldest first."""
	G = frappe.qb.DocType("Grievance")
	A = frappe.qb.DocType("Grievance Administrative Area")
	R = frappe.qb.DocType("Grievance Administrative Area").as_("region_area")
	params = ctx.params
	query = (
		frappe.qb.from_(G)
		.join(A)
		.on(A.name == G.administrative_area)
		.left_join(R)
		.on((R.level_name == "Region") & (A.lft >= R.lft) & (A.lft <= R.rgt))
		.select(
			G.ticket_number,
			G.description,
			G.status,
			G.service_category,
			G.submitter_name,
			G.is_anonymous,
			G.creation,
			A.area_name,
			R.code.as_("region_code"),
		)
		.orderby(G.creation, order=frappe.qb.desc if order == "desc" else frappe.qb.asc)
		.limit(limit)
	)
	if statuses:
		query = query.where(G.status.isin(statuses))
	if escalated is not None:
		query = query.where(G.escalated == escalated)
	if params.region:
		query = query.where(R.code.isin(params.region))
	if params.service_category:
		query = query.where(G.service_category.isin(params.service_category))
	if params.assigned_dept:
		query = query.where(G.assigned_dept.isin(params.assigned_dept))
	return query.run(as_dict=True)


# Registry
# --------

CHARTS = {
	"grvKpis": Chart(build_kpis),
	"grvPerformanceKpis": Chart(build_performance_kpis, params=FILTERS | {"month"}),
	"grvMonthlyTrend": Chart(build_monthly_trend, params=PERIOD),
	"grvWeeklyTrend": Chart(build_weekly_trend, params=PERIOD),
	"grvNetBacklogTrend": Chart(build_net_backlog_trend, params=FILTERS | {"from_date", "granularity"}),
	"grvStatusDistribution": Chart(build_status_distribution),
	"grvByCategory": Chart(build_by_category),
	"grvByAdminLevel": Chart(build_by_admin_level, params=FILTERS | {"level"}, by_woreda=True),
	"grvCategoryResolution": Chart(build_category_resolution, params=PERIOD),
	"grvAvgResolutionByCategory": Chart(build_avg_resolution_by_category, params=PERIOD),
	"grvPerformanceScore": Chart(build_performance_score, params=FILTERS | {"month"}),
	"grvResolutionRateByRegion": Chart(build_resolution_rate_by_region, params=FILTERS | {"as_of_month"}),
	"grvSlaRisk": Chart(build_sla_risk),
	"grvPendingDuplicates": Chart(build_pending_duplicates),
	"grvOldestOpen": Chart(build_oldest_open, live_for_admin=True),
	"grvRecent": Chart(build_recent, params=FILTERS | {"limit"}, public=False, live_for_admin=True),
	"grvFilterRegions": Chart(build_filter_regions, params=frozenset()),
	"grvFilterCategories": Chart(build_filter_categories, params=frozenset()),
	"grvFilterDepartments": Chart(build_filter_departments, params=frozenset(), public=False),
}
PUBLIC_CHARTS = tuple(chart_id for chart_id, chart in CHARTS.items() if chart.public)


def get_chart(chart_id, params, admin, scope=None):
	"""Rows and as_of for one chart, from cache when possible. Raises on failure.

	`scope` is caller_scope() for the officer view; None otherwise. The admin view is never
	scoped, because its live charts read Grievance and would ignore it.
	"""
	if admin and scope is not None:
		raise ValueError("The admin view cannot be scoped")
	chart = CHARTS[chart_id]
	if scope and not chart.by_woreda:
		# The daily rollup has no Woreda column, so a Woreda desk cannot be applied to this
		# chart and contributes nothing, as every desk below Region level did before.
		scope = tuple(clause for clause in scope if clause.woredas is None)
	live = admin and chart.live_for_admin
	refreshed = rollup.as_of()
	used = params.used_by(chart)

	fingerprint = json.dumps(
		{
			"used": used,
			"as_of": None if live else str(refreshed),
			"scope": None if scope is None else sorted(repr(clause) for clause in scope),
		},
		sort_keys=True,
		default=str,
	)
	view = "admin" if admin else "public" if scope is None else "officer"
	key = "grievance:charts:{}:{}:{}".format(chart_id, view, hashlib.sha256(fingerprint.encode()).hexdigest())
	cached = frappe.cache.get_value(key)
	if cached is not None:
		return cached

	ctx = Context(params, admin, scope)
	rows = chart.build(ctx)
	stamp = now_datetime() if live else refreshed
	result = {
		"rows": rows,
		"as_of": to_tz_aware_iso(stamp) if stamp else None,
		"unavailable": ctx.unavailable,
	}
	frappe.cache.set_value(key, result, expires_in_sec=LIVE_TTL if live else ROLLUP_TTL)
	return result
