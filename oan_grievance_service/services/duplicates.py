"""Duplicate grievances: flagging a likely repeat, and the officer's ruling on it.

The system only flags. A new grievance is compared with the same submitter's earlier
grievances; a pair that scores at or above the threshold is recorded as a Grievance Duplicate
row awaiting review. Nothing about the ticket changes until an officer confirms it, which
moves it to the terminal Duplicate state and points it at the main ticket. An officer who
rules it is not a duplicate leaves the ticket exactly as it was.

The comparison is the "Option A" score of GRIEVANCE_SIMILARITY_ALGORITHMS.md, phase 1:

    score = w_description * description + w_category * same category + w_type * same type

The description is a cosine over character n-grams of the normalised text. Only one
submitter's tickets are ever compared (a handful, not millions), so there is no index and
no IDF weighting: the pool is too small for inverse document frequency to mean anything.
Category and type are weights, not filters, because submitters sometimes pick the wrong
category. One exception: another sub-type of the same category is a second grievance and is
never compared (`other_sub_type`). The weights and thresholds are starting points an administrator tunes in Grievance
Settings from measurements on labelled pairs; the defaults live here until they are set.
"""

import math
import re
import unicodedata
from collections import Counter
from typing import NamedTuple

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, now_datetime
from oan_auth_service.api import operational_log as oplog

from oan_grievance_service.grievance_management.doctype.grievance_timeline.grievance_timeline import (
	GrievanceTimeline,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle
from oan_grievance_service.services import ticket_number as tn

ACTION_CONFIRM = "Confirm Duplicate"
METHOD_SIMILARITY = "Same Submitter Similarity"
METHOD_MANUAL = "Manual"

# An earlier ticket is a candidate while it is still live or was resolved. Closed, Rejected
# and Duplicate tickets are finished business. Submitted, Assigned and Reopened are included
# on purpose: a double submit is compared with a ticket that has barely been routed.
CANDIDATE_STATES = (
	C.STATE_SUBMITTED,
	C.STATE_ASSIGNED,
	C.STATE_REOPENED,
	C.STATE_IN_PROGRESS,
	C.STATE_MORE_INFO_NEEDED,
	C.STATE_RESOLVED,
)

# Starting values, the same as the defaults of the Duplicate Detection fields in Grievance
# Settings, where an administrator tunes them once confirm and dismiss rulings show what
# works. They stand in only when the stored values are unusable (see tuning). The description
# weight is whatever the category and type weights leave of 1.
DEFAULT_W_CATEGORY = 0.10
DEFAULT_W_TYPE = 0.20
# Category and type together must stay below the threshold, so unrelated text can never be
# flagged on matching labels alone. Saving the settings enforces it (validate_tuning).
DEFAULT_THRESHOLD = 0.65
DEFAULT_HIGH_THRESHOLD = 0.85

NGRAM_SIZES = (3, 4, 5)
# One submitter's recent tickets are the pool; the cap only guards a runaway account.
POOL_LIMIT = 200
FLAG_ATTEMPTS = 2
# A ticket is flagged against its best few matches, not every similar ticket it ever had.
MAX_FLAGS = 3

# Before filing, a farmer on an interactive channel is asked whether this is a new grievance
# when a similar one of theirs is this recent. IVR and SMS are one way: nobody could answer,
# so they are only flagged afterwards, for an officer.
PROMPT_WINDOW_HOURS = 24
INTERACTIVE_CHANNELS = ("Web Portal",)


def prompt_enabled():
	"""Whether a web farmer is asked before a similar grievance is filed.

	Off by default: the question is answered with a 409 the web client must understand, so it
	is switched on in Grievance Settings once the client sends `confirm_new`. Flagging for an
	officer does not depend on it.
	"""
	return bool(frappe.db.get_single_value("Grievance Settings", "ask_before_filing_duplicate"))


def _homophone_folds():
	"""Ge'ez letters that sound alike and are used interchangeably, folded onto one form.

	Applied to the copy of the text used for matching only: it can merge words that differ
	in meaning (ድህነት "poverty", ድኅነት "salvation"), so the stored description is never
	changed.
	"""
	folds = {}
	for offset in range(7):
		folds[chr(0x1210 + offset)] = chr(0x1200 + offset)  # HHA row -> HA row
		folds[chr(0x1280 + offset)] = chr(0x1200 + offset)  # XA row -> HA row
		folds[chr(0x12D0 + offset)] = chr(0x12A0 + offset)  # pharyngeal A row -> glottal A row
		folds[chr(0x1338 + offset)] = chr(0x1340 + offset)  # TSA row -> TZA row
	for offset in range(8):
		folds[chr(0x1220 + offset)] = chr(0x1230 + offset)  # SZA row -> SA row
	return str.maketrans(folds)


_FOLD = _homophone_folds()
_NON_WORD = re.compile(r"\W+")


def normalize(text):
	"""Lower-cased, punctuation-free, single-spaced text with Ge'ez homophones folded.

	Numbers, place names and cooperative names stay: they are strong evidence.
	"""
	text = unicodedata.normalize("NFC", text or "").casefold().translate(_FOLD)
	return " ".join(_NON_WORD.sub(" ", text).split())


def _vector(text):
	"""Character n-gram counts, taken within words so a phrase does not match across them."""
	counts = Counter()
	for word in text.split():
		padded = f" {word} "
		for size in NGRAM_SIZES:
			for start in range(len(padded) - size + 1):
				counts[padded[start : start + size]] += 1
	return counts


def description_similarity(first, second):
	"""Cosine of the two descriptions' character n-grams, 0 to 1."""
	one, two = _vector(normalize(first)), _vector(normalize(second))
	if not one or not two:
		return 0.0
	dot = sum(count * two[gram] for gram, count in one.items() if gram in two)
	norm = math.sqrt(sum(v * v for v in one.values())) * math.sqrt(sum(v * v for v in two.values()))
	return dot / norm if norm else 0.0


class Tuning(NamedTuple):
	"""The weights and thresholds a comparison uses."""

	description: float
	category: float
	type: float
	threshold: float
	high_threshold: float


def tuning_problem(category, type_, threshold, high_threshold):
	"""Why these weights and thresholds are unusable, or None when they are fine.

	Category plus type is what two tickets score with identical labels and no shared words, so
	it must stay below the threshold, or unrelated complaints would be flagged.
	"""
	if category < 0 or type_ < 0:
		return _("The category and type weights cannot be negative.")
	if not 0 < threshold <= high_threshold <= 1:
		return _("The thresholds must satisfy 0 < flag threshold <= high threshold <= 1.")
	if category + type_ >= threshold:
		return _(
			"The category weight plus the type weight ({0}) must be below the flag threshold ({1}), "
			"or matching labels alone would flag a ticket."
		).format(round(category + type_, 4), threshold)
	return None


def validate_tuning(category, type_, threshold, high_threshold):
	"""Refuse to save settings that would flag on labels alone or make no sense."""
	problem = tuning_problem(category, type_, threshold, high_threshold)
	if problem:
		frappe.throw(problem, title=_("Invalid Duplicate Detection Settings"))


def tuning():
	"""The weights and thresholds in force: Grievance Settings, else the defaults.

	The defaults also apply when the stored values are unusable, such as code running before
	the migrate that adds the fields: a threshold of 0 would flag every pair.
	"""
	settings = frappe.get_doc("Grievance Settings")
	values = (
		flt(settings.get("duplicate_weight_category")),
		flt(settings.get("duplicate_weight_type")),
		flt(settings.get("duplicate_threshold")),
		flt(settings.get("duplicate_high_threshold")),
	)
	if tuning_problem(*values):
		values = (DEFAULT_W_CATEGORY, DEFAULT_W_TYPE, DEFAULT_THRESHOLD, DEFAULT_HIGH_THRESHOLD)
	category, type_, threshold, high_threshold = values
	return Tuning(round(1 - category - type_, 4), category, type_, threshold, high_threshold)


def score(grievance, other, config=None):
	"""How alike two grievances are, 0 to 1. A ranking score, not a probability."""
	config = config or tuning()
	return round(
		config.description * description_similarity(grievance.get("description"), other.get("description"))
		+ config.category * (grievance.get("service_category") == other.get("service_category"))
		+ config.type * (grievance.get("grievance_type") == other.get("grievance_type")),
		4,
	)


def match_level(value, config=None):
	return "High" if value >= (config or tuning()).high_threshold else "Medium"


def candidates_for(grievance, since=None):
	"""The submitter's earlier grievances a new one is compared with, newest first.

	Earlier only, so the older ticket is always the main one and two tickets can never flag
	each other. A grievance with no submitter (anonymous) has no history to compare. `since`
	limits the pool to tickets filed after it. Works on a grievance not yet inserted, which
	has no name or creation time to exclude.
	"""
	if not grievance.submitter:
		return []
	filters = [
		["submitter", "=", grievance.submitter],
		["status", "in", CANDIDATE_STATES],
	]
	if grievance.get("name"):
		filters.append(["name", "!=", grievance.name])
	if grievance.get("creation"):
		filters.append(["creation", "<", grievance.creation])
	if since:
		filters.append(["creation", ">=", since])
	return frappe.get_all(
		"Grievance",
		filters=filters,
		fields=["name", "status", "description", "service_category", "grievance_type", "creation"],
		order_by="creation desc, name desc",
		limit_page_length=POOL_LIMIT,
	)


def other_sub_type(grievance, other) -> bool:
	"""Whether the tickets share a category but name different sub-types.

	A farmer who files a second sub-type of the same category (late seed, then fertilizer price)
	has a second grievance, however alike the two texts read, so the pair is never flagged. Across
	categories the texts still decide: the same complaint filed again under the wrong category is
	a repeat. An officer who finds a repeat under the wrong sub-type still confirms it by naming the
	main ticket (`confirm`).
	"""
	mine, theirs = grievance.get("grievance_type"), other.get("grievance_type")
	same_category = grievance.get("service_category") == other.get("service_category")
	return bool(same_category and mine and theirs and mine != theirs)


def matches_for(grievance, since=None):
	"""[(score, earlier ticket)] at or above the threshold, best first; the older wins a tie.

	Tickets of another sub-type are not compared (`other_sub_type`).
	"""
	config = tuning()
	matches = []
	for other in candidates_for(grievance, since):
		if other_sub_type(grievance, other):
			continue
		value = score(grievance, other, config)
		if value >= config.threshold:
			matches.append((value, other))
	matches.sort(key=lambda match: (-match[0], match[1].creation, match[1].name))
	return matches[:MAX_FLAGS]


def likely_repeats(grievance):
	"""The submitter's recent similar tickets, to ask before a new one is filed.

	`grievance` is the unsaved filing. Only tickets from the last PROMPT_WINDOW_HOURS count: a
	farmer filing the same thing again within a day, perhaps through another channel, is
	probably repeating themselves. Older matches are left to the officer's flag.
	"""
	since = add_to_date(now_datetime(), hours=-PROMPT_WINDOW_HOURS)
	return [{"match_level": match_level(value), **other} for value, other in matches_for(grievance, since)]


def flag_possible_duplicates(grievance):
	"""Record the earlier tickets this one likely repeats. Returns the new review rows.

	Best match first; the older ticket wins a tie. Safe to run twice: a pair already
	recorded is not recorded again.
	"""
	rows = []
	for value, other in matches_for(grievance):
		if frappe.db.exists("Grievance Duplicate", {"grievance": grievance.name, "duplicate_of": other.name}):
			continue
		rows.append(
			frappe.get_doc(
				{
					"doctype": "Grievance Duplicate",
					"grievance": grievance.name,
					"duplicate_of": other.name,
					"detected_at": now_datetime(),
					"detection_method": METHOD_SIMILARITY,
					"similarity_score": value,
					"match_level": match_level(value),
				}
			).insert(ignore_permissions=True)
		)
	return rows


def enqueue_flagging(grievance_name: str) -> None:
	"""Flag look-alikes after the filing commits, so scoring never delays the submitter."""
	try:
		frappe.enqueue(
			"oan_grievance_service.services.duplicates.flag_grievance_job",
			queue=C.QUEUE_DUPLICATES,
			grievance_name=grievance_name,
			enqueue_after_commit=True,
		)
	except Exception:
		oplog.log_error(
			title=f"Failed to enqueue duplicate flagging for grievance: {grievance_name}",
			message=frappe.get_traceback(),
		)


def flag_grievance_job(grievance_name: str) -> None:
	if not frappe.db.exists("Grievance", grievance_name):
		return
	flag_without_failing(frappe.get_doc("Grievance", grievance_name))


def flag_without_failing(grievance):
	"""Flag a grievance that is already filed, without ever losing the filing.

	Flagging only advises an officer. A fault in the scoring or in writing a review row must
	not roll back the farmer's grievance, so it is undone to a savepoint and logged instead.
	A deadlock (e.g. on the review-row naming series) aborts the whole transaction and takes the
	savepoint with it, so it is rolled back in full and tried once more.
	"""
	save_point = "flag_possible_duplicates"
	for attempt in range(1, FLAG_ATTEMPTS + 1):
		frappe.db.savepoint(save_point)
		try:
			return flag_possible_duplicates(grievance)
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt < FLAG_ATTEMPTS:
				continue
			_log_flag_failure(grievance)
		except Exception:
			frappe.db.rollback(save_point=save_point)
			_log_flag_failure(grievance)
		return []


def _log_flag_failure(grievance):
	oplog.log_error(
		title=f"Failed to flag possible duplicates for grievance: {grievance.name}",
		message=frappe.get_traceback(),
	)


def pending_reviews(grievance_name):
	"""The rows awaiting an officer's ruling on a grievance, best match first."""
	return frappe.get_all(
		"Grievance Duplicate",
		filters={"grievance": grievance_name, "is_confirmed": 0, "is_dismissed": 0},
		fields=["name", "duplicate_of", "similarity_score", "match_level", "detected_at"],
		order_by="similarity_score desc, detected_at asc, name asc",
	)


def is_read_only(grievance):
	return grievance.workflow_state == C.STATE_DUPLICATE


def assert_not_duplicate(grievance):
	"""A confirmed duplicate takes no comments, deferrals or reassignment: the main ticket does."""
	if is_read_only(grievance):
		frappe.throw(
			_("This ticket is a duplicate of {0}. All further handling happens on the main ticket.").format(
				tn.display(frappe.db.get_value("Grievance", grievance.duplicate_of, "ticket_number"))
				if grievance.duplicate_of
				else _("another ticket")
			),
			frappe.ValidationError,
			title=_("Duplicate Ticket"),
		)


def validate_main_ticket(grievance, main_name):
	"""The main ticket must be a live or resolved case of the same submitter.

	The same states and the same submitter as the candidates the system flags. The farmer is
	told "all further handling will happen on the main ticket" and sees its number, so it
	must be a ticket that is still handled and is theirs, never another farmer's.
	"""
	if main_name == grievance.name:
		frappe.throw(_("A ticket cannot be a duplicate of itself."), title=_("Invalid Main Ticket"))
	main = frappe.db.get_value("Grievance", main_name, ["workflow_state", "submitter"], as_dict=True)
	if main.workflow_state == C.STATE_DUPLICATE:
		frappe.throw(
			_("The main ticket is itself a duplicate. Choose the ticket it points to."),
			title=_("Invalid Main Ticket"),
		)
	if main.workflow_state not in CANDIDATE_STATES:
		frappe.throw(
			_(
				"The main ticket is {0}. A duplicate can only point to a ticket that is still open or resolved."
			).format(_(main.workflow_state)),
			title=_("Invalid Main Ticket"),
		)
	if not grievance.submitter or main.submitter != grievance.submitter:
		frappe.throw(
			_("The main ticket must belong to the same submitter as this ticket."),
			title=_("Invalid Main Ticket"),
		)


def confirm(grievance, main_name=None, note=None):
	"""An officer rules this ticket is a duplicate of `main_name`.

	The main ticket defaults to the best pending match. Moves the case to Duplicate through
	the workflow, which records the status history, stops the SLA clock and, with the
	terminal state, makes the ticket read-only. Returns the Grievance Status History row and
	the timeline entry.
	"""
	pending = pending_reviews(grievance.name)
	main_name = main_name or (pending[0].duplicate_of if pending else None)
	if not main_name:
		frappe.throw(
			_("Name the main ticket this ticket duplicates."),
			frappe.ValidationError,
			title=_("Main Ticket Required"),
		)
	validate_main_ticket(grievance, main_name)
	if ACTION_CONFIRM not in lifecycle.actions_available(grievance):
		frappe.throw(
			_("A grievance in status '{0}' cannot be confirmed as a duplicate.").format(
				_(grievance.workflow_state)
			),
			frappe.ValidationError,
			title=_("Action Not Permitted"),
		)

	# The workflow engine reloads the case from the database before it moves it, so the
	# link is saved first and the move validates against it.
	grievance.duplicate_of = main_name
	grievance.save(ignore_permissions=True)
	history = lifecycle.transition(grievance, ACTION_CONFIRM, reason=note)

	now, user = now_datetime(), frappe.session.user
	review = next((row for row in pending if row.duplicate_of == main_name), None)
	if review:
		row = frappe.get_doc("Grievance Duplicate", review.name)
	else:
		row = frappe.get_doc(
			{
				"doctype": "Grievance Duplicate",
				"grievance": grievance.name,
				"duplicate_of": main_name,
				"detected_at": now,
				"detection_method": METHOD_MANUAL,
			}
		)
	row.update({"is_confirmed": 1, "decided_by": user, "decided_at": now, "decision_note": note})
	row.save(ignore_permissions=True)

	main_ticket = frappe.db.get_value("Grievance", main_name, "ticket_number")
	# The other flags are moot now: the ticket is a duplicate of the one the officer chose.
	for other in pending:
		if other is review:
			continue
		superseded = frappe.get_doc("Grievance Duplicate", other.name)
		superseded.update(
			{
				"is_dismissed": 1,
				"decided_by": user,
				"decided_at": now,
				"decision_note": _("Superseded: confirmed as a duplicate of {0}.").format(
					tn.display(main_ticket)
				),
			}
		)
		superseded.save(ignore_permissions=True)

	entry = GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="status_change",
		is_internal=False,
		body=_("Marked as duplicate of {0}. All further handling will happen on the main ticket.").format(
			tn.display(main_ticket)
		),
		author_user=user,
		author_type="officer",
		ref_doctype="Grievance Status History",
		ref_docname=history.name if history else None,
	)
	return history, entry


def dismiss(grievance, note=None):
	"""An officer rules the flag is wrong. The ticket carries on as a normal one.

	A ticket still waiting for a desk (Submitted, nobody assigned) is routed now, as a new filing
	is, so the ruling never leaves it unassigned (`route_if_waiting`).
	"""
	entry = _dismiss_reviews(grievance, note)
	route_if_waiting(grievance)
	return entry


def route_if_waiting(grievance):
	"""Route a ticket that is still Submitted with nobody on it. Returns the desk, or None."""
	from oan_grievance_service.services import routing

	grievance.reload()
	if grievance.workflow_state != C.STATE_SUBMITTED or grievance.assigned_to:
		return None
	return routing.apply_routing(grievance)


def _dismiss_reviews(grievance, note=None):
	assert_not_duplicate(grievance)
	pending = pending_reviews(grievance.name)
	if not pending:
		frappe.throw(
			_("This ticket is not flagged as a possible duplicate."),
			frappe.ValidationError,
			title=_("Nothing To Review"),
		)
	now, user = now_datetime(), frappe.session.user
	for review in pending:
		row = frappe.get_doc("Grievance Duplicate", review.name)
		row.update({"is_dismissed": 1, "decided_by": user, "decided_at": now, "decision_note": note})
		row.save(ignore_permissions=True)
	return GrievanceTimeline.record(
		grievance=grievance.name,
		entry_type="note",
		is_internal=True,
		body=_("Reviewed as a possible duplicate of {0} and ruled not a duplicate.").format(
			", ".join(
				tn.display(frappe.db.get_value("Grievance", row.duplicate_of, "ticket_number"))
				for row in pending
			)
		),
		author_user=user,
		author_type="officer",
	)
