# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

"""Duplicate grievances: flagging a likely repeat, the officer's ruling, the read-only state,
and the dashboard counts that include them."""

import random
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service import permissions
from oan_grievance_service.api.v1 import attachment, change_request, grievance
from oan_grievance_service.api.v1._options import get_status_summary, public_status
from oan_grievance_service.patches import dismiss_legacy_duplicate_flags
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import dashboard_rollup, duplicates, identity, ticket_export
from oan_grievance_service.services import ticket_number as tn
from oan_grievance_service.setup import install
from oan_grievance_service.tests.fixtures import (
	a_grievance,
	a_grievance_type,
	a_leaf_area,
	a_service_category,
	discard_grievance,
)
from oan_grievance_service.tests.test_category_assignment import _keep_transaction
from oan_grievance_service.tests.test_ticket_export import rows_of, sections_of, table_of

FERTILIZER = "Fertilizer allocated for the season has not reached the kebele store."
CANAL = "My irrigation canal is blocked by silt and the water cannot reach my plot at all."


def a_submitter_profile():
	"""A new farmer's profile, found by a unique Fayda ID; the caller deletes it."""
	fayda = str(random.randrange(10**15, 10**16))
	mobile = f"+2519{random.randrange(10**7, 10**8)}"
	return identity.find_or_create_submitter(
		{"fayda_id": fayda, "submitter_name": "Test Farmer", "contact_mobile": mobile}
	)


class TestDuplicateScoring(FrappeTestCase):
	def test_normalize_folds_case_punctuation_and_geez_homophones(self):
		self.assertEqual(duplicates.normalize("  Fertilizer, NOT arrived!! "), "fertilizer not arrived")
		# The same word spelled with either of two homophone letters is one word.
		self.assertEqual(duplicates.normalize("ሐገር"), duplicates.normalize("ሀገር"))
		self.assertEqual(duplicates.normalize("ሠላም"), duplicates.normalize("ሰላም"))
		self.assertEqual(duplicates.normalize("ዐለም"), duplicates.normalize("አለም"))

	def test_description_similarity_ranks_alike_above_unalike(self):
		same = duplicates.description_similarity(FERTILIZER, FERTILIZER.upper().replace(".", "!!"))
		paraphrase = duplicates.description_similarity(
			FERTILIZER, "The fertilizer allocated this season never arrived at our kebele store."
		)
		unrelated = duplicates.description_similarity(FERTILIZER, CANAL)
		self.assertAlmostEqual(same, 1.0)
		self.assertGreater(paraphrase, 0.5)
		self.assertLess(unrelated, 0.3)
		self.assertEqual(duplicates.description_similarity("", FERTILIZER), 0.0)

	def test_labels_alone_can_never_reach_the_threshold(self):
		config = duplicates.tuning()
		self.assertLess(config.category + config.type, config.threshold)
		self.assertAlmostEqual(config.description + config.category + config.type, 1.0)

	def test_identical_text_under_another_category_is_still_flagged(self):
		config = duplicates.tuning()
		first = frappe._dict(description=FERTILIZER, service_category="A", grievance_type="X")
		other = frappe._dict(description=FERTILIZER, service_category="B", grievance_type="Y")
		self.assertAlmostEqual(duplicates.score(first, other), config.description)
		self.assertGreaterEqual(duplicates.score(first, other), config.threshold)


class TestDuplicateTuning(FrappeTestCase):
	"""An administrator sets the weights and thresholds in Grievance Settings."""

	FIELDS = (
		"duplicate_weight_category",
		"duplicate_weight_type",
		"duplicate_threshold",
		"duplicate_high_threshold",
	)

	def setUp(self):
		frappe.set_user("Administrator")
		self.was = frappe.db.get_singles_dict("Grievance Settings")
		self.farmer = a_submitter_profile()
		self.made = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for field in self.FIELDS:
			if field in self.was:
				frappe.db.set_single_value("Grievance Settings", field, self.was[field])
			else:
				frappe.db.delete("Singles", {"doctype": "Grievance Settings", "field": field})
		for name in reversed(self.made):
			discard_grievance(name)
		frappe.delete_doc("Grievance Submitter Profile", self.farmer, force=True, ignore_permissions=True)

	def _set(self, category=None, type_=None, threshold=None, high=None):
		for field, value in zip(self.FIELDS, (category, type_, threshold, high), strict=True):
			frappe.db.set_single_value("Grievance Settings", field, value)

	def _case(self, **overrides):
		doc = a_grievance(submitter=self.farmer, **overrides)
		self.made.append(doc.name)
		return doc

	def _save_settings(self, **values):
		settings = frappe.get_doc("Grievance Settings")
		settings.update(values)
		settings.save()

	def test_missing_or_unusable_settings_use_the_defaults(self):
		defaults = (0.7, duplicates.DEFAULT_W_CATEGORY, duplicates.DEFAULT_W_TYPE, 0.65, 0.85)
		self._set()
		self.assertEqual(duplicates.tuning(), defaults)
		# A threshold of 0 would flag every pair, so it is never used.
		self._set(category=0.1, type_=0.2, threshold=0, high=0.85)
		self.assertEqual(duplicates.tuning(), defaults)

	def test_the_description_weight_is_what_the_labels_leave(self):
		self._set(category=0.2, type_=0.3, threshold=0.7, high=0.9)
		self.assertEqual(duplicates.tuning(), (0.5, 0.2, 0.3, 0.7, 0.9))

	def test_the_weights_change_the_score(self):
		self._set(category=0.3, type_=0.1, threshold=0.65, high=0.85)
		first = frappe._dict(description="", service_category="A", grievance_type="X")
		same_category = frappe._dict(description="", service_category="A", grievance_type="Y")
		same_type = frappe._dict(description="", service_category="B", grievance_type="X")
		self.assertAlmostEqual(duplicates.score(first, same_category), 0.3)
		self.assertAlmostEqual(duplicates.score(first, same_type), 0.1)

	def test_a_lower_threshold_flags_a_looser_match(self):
		self._case(description=FERTILIZER)
		dup = self._case(description=CANAL)
		self.assertEqual(duplicates.flag_possible_duplicates(dup), [], "unflagged at the defaults")

		self._set(category=0.1, type_=0.2, threshold=0.4, high=0.9)
		(row,) = duplicates.flag_possible_duplicates(dup)

		self.assertEqual(row.match_level, "Medium")

	def test_the_high_threshold_sets_the_match_level(self):
		self._set(category=0.1, type_=0.2, threshold=0.65, high=0.9)
		self.assertEqual(duplicates.match_level(0.89), "Medium")
		self.assertEqual(duplicates.match_level(0.9), "High")

		self._case(description=FERTILIZER)
		dup = self._case(description=FERTILIZER)
		(row,) = duplicates.flag_possible_duplicates(dup)
		self.assertEqual(row.match_level, "High", "an identical repeat scores 1.0")

	def test_saving_rejects_labels_that_alone_would_flag(self):
		with self.assertRaises(frappe.ValidationError):
			self._save_settings(
				duplicate_weight_category=0.4,
				duplicate_weight_type=0.3,
				duplicate_threshold=0.65,
				duplicate_high_threshold=0.85,
			)

	def test_saving_rejects_a_high_threshold_below_the_flag_threshold(self):
		with self.assertRaises(frappe.ValidationError):
			self._save_settings(
				duplicate_weight_category=0.1,
				duplicate_weight_type=0.2,
				duplicate_threshold=0.7,
				duplicate_high_threshold=0.6,
			)

	def test_saving_rejects_negative_weights_and_thresholds_out_of_range(self):
		for values in (
			{"duplicate_weight_category": -0.1},
			{"duplicate_threshold": 0, "duplicate_high_threshold": 0.85},
			{"duplicate_threshold": 0.9, "duplicate_high_threshold": 1.2},
		):
			with self.subTest(values), self.assertRaises(frappe.ValidationError):
				self._save_settings(
					**{
						"duplicate_weight_category": 0.1,
						"duplicate_weight_type": 0.2,
						"duplicate_threshold": 0.65,
						"duplicate_high_threshold": 0.85,
						**values,
					}
				)

	def test_a_valid_change_is_saved_and_used(self):
		self._save_settings(
			duplicate_weight_category=0.15,
			duplicate_weight_type=0.25,
			duplicate_threshold=0.7,
			duplicate_high_threshold=0.9,
		)
		self.assertEqual(duplicates.tuning(), (0.6, 0.15, 0.25, 0.7, 0.9))


class TestDuplicateFlow(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.farmer = a_submitter_profile()
		self.other_farmer = a_submitter_profile()
		self.profiles = [self.farmer, self.other_farmer]
		self.made = []
		frappe.clear_messages()

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in reversed(self.made):
			discard_grievance(name)
		for profile in self.profiles:
			frappe.delete_doc("Grievance Submitter Profile", profile, force=True, ignore_permissions=True)

	def _case(self, **overrides):
		overrides.setdefault("submitter", self.farmer)
		overrides.setdefault("description", FERTILIZER)
		doc = a_grievance(**overrides)
		self.made.append(doc.name)
		return doc

	def _pair(self, **newer):
		"""An earlier ticket and a later one flagged against it."""
		main = self._case()
		dup = self._case(**newer)
		return main, dup

	# Flagging

	def test_a_repeat_by_the_same_farmer_is_flagged_against_the_older_ticket(self):
		main, dup = self._pair()
		rows = duplicates.flag_possible_duplicates(dup)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].grievance, rows[0].duplicate_of), (dup.name, main.name))
		self.assertEqual(rows[0].detection_method, duplicates.METHOD_SIMILARITY)
		self.assertEqual(rows[0].match_level, "High")
		self.assertAlmostEqual(rows[0].similarity_score, 1.0)
		self.assertEqual(dup.status, C.STATE_SUBMITTED, "flagging does not change the ticket")

	def test_the_older_ticket_is_never_flagged_against_the_newer(self):
		main, _dup = self._pair()
		self.assertEqual(duplicates.flag_possible_duplicates(main), [])

	def test_flagging_twice_records_the_pair_once(self):
		_main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		self.assertEqual(duplicates.flag_possible_duplicates(dup), [])
		self.assertEqual(len(duplicates.pending_reviews(dup.name)), 1)

	def test_another_farmers_ticket_is_not_compared(self):
		self._case(submitter=self.other_farmer)
		dup = self._case()
		self.assertEqual(duplicates.flag_possible_duplicates(dup), [])

	def test_an_anonymous_ticket_has_no_history_to_compare(self):
		self._case()
		anonymous = self._case(submitter=None)
		self.assertEqual(duplicates.flag_possible_duplicates(anonymous), [])

	def test_unrelated_text_is_not_flagged_even_with_the_same_category_and_type(self):
		self._case()
		dup = self._case(description=CANAL)
		self.assertEqual(duplicates.flag_possible_duplicates(dup), [])

	def test_identical_text_under_another_category_and_type_is_flagged(self):
		self._case()
		dup = self._case(service_category=a_service_category(), grievance_type=a_grievance_type())
		self.assertEqual(len(duplicates.flag_possible_duplicates(dup)), 1)

	def test_another_sub_type_of_the_same_category_is_a_new_grievance(self):
		"""Staging report 26 Nov 2026: the same farmer, category and text, another sub-type, was held."""
		from oan_grievance_service.tests.test_classification import a_type

		first = self._case()
		other_type = a_type(first.service_category)
		dup = self._case(grievance_type=other_type)
		self.assertEqual(duplicates.flag_possible_duplicates(dup), [])
		self.assertEqual(duplicates.matches_for(dup), [])

	def test_only_live_or_resolved_tickets_are_candidates(self):
		anchor = self._case(description=CANAL)
		for state in (C.STATE_CLOSED, C.STATE_REJECTED):
			self._case(workflow_state=state)
		self._case(workflow_state=C.STATE_DUPLICATE, duplicate_of=anchor.name)
		dup = self._case()
		self.assertEqual(duplicates.flag_possible_duplicates(dup), [])
		for state in (C.STATE_RESOLVED, C.STATE_IN_PROGRESS, C.STATE_MORE_INFO_NEEDED):
			# A fresh farmer per state, so earlier tickets do not fill the flag cap.
			farmer = a_submitter_profile()
			self.profiles.append(farmer)
			kept = self._case(submitter=farmer, workflow_state=state)
			later = self._case(submitter=farmer)
			flagged = {row.duplicate_of for row in duplicates.flag_possible_duplicates(later)}
			self.assertIn(kept.name, flagged, state)

	def test_a_ticket_is_flagged_against_its_best_matches_only(self):
		for _ in range(duplicates.MAX_FLAGS + 2):
			self._case()
		dup = self._case()
		self.assertEqual(len(duplicates.flag_possible_duplicates(dup)), duplicates.MAX_FLAGS)

	# The officer's ruling

	def test_confirming_makes_the_ticket_a_read_only_duplicate_of_the_main(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)

		result = grievance.confirm_duplicate(ticket_number=dup.name, note="Same fertilizer complaint")

		self.assertEqual(result["data"]["status"], C.STATE_DUPLICATE, msg=result)
		self.assertEqual(result["data"]["duplicate_of"], tn.display(main.name))
		dup.reload()
		self.assertEqual(
			(dup.status, dup.workflow_state, dup.duplicate_of), (C.STATE_DUPLICATE,) * 2 + (main.name,)
		)
		row = frappe.get_doc("Grievance Duplicate", {"grievance": dup.name, "duplicate_of": main.name})
		self.assertEqual((row.is_confirmed, row.is_dismissed, row.decided_by), (1, 0, "Administrator"))
		self.assertEqual(row.decision_note, "Same fertilizer complaint")
		self.assertTrue(
			frappe.db.exists(
				"Grievance Status History",
				{"grievance": dup.name, "to_status": C.STATE_DUPLICATE, "action": duplicates.ACTION_CONFIRM},
			)
		)
		entry = frappe.get_all(
			"Grievance Timeline",
			filters={"grievance": dup.name, "entry_type": "status_change"},
			fields=["body", "is_internal"],
		)[0]
		self.assertIn("All further handling will happen on the main ticket", entry.body)
		self.assertFalse(entry.is_internal)

	def test_a_confirmed_duplicate_cannot_be_changed_or_discussed(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name)
		dup.reload()

		dup.description = "A changed description that is long enough to be valid."
		with self.assertRaises(frappe.ValidationError):
			dup.save(ignore_permissions=True)
		dup.reload()
		with _keep_transaction():
			posted = grievance.message(ticket_number=dup.name, body="Any news on this?")
		self.assertEqual(posted["code"], "VALIDATION_ERROR", msg=posted)
		with self.assertRaises(frappe.ValidationError):
			change_request.raise_change_request(dup, "Defer", {"sla_due_date": "2030-01-01"})
		self.assertFalse(
			frappe.db.exists("Grievance Timeline", {"grievance": dup.name, "entry_type": "message"})
		)

	def test_the_main_defaults_to_the_best_match_and_an_officer_may_name_another(self):
		main = self._case()
		chosen = self._case(description=CANAL)
		dup = self._case()
		duplicates.flag_possible_duplicates(dup)

		result = grievance.confirm_duplicate(ticket_number=dup.name, main_ticket=chosen.name)

		self.assertEqual(result["data"]["duplicate_of"], tn.display(chosen.name), msg=result)
		manual = frappe.get_doc("Grievance Duplicate", {"grievance": dup.name, "duplicate_of": chosen.name})
		self.assertEqual((manual.detection_method, manual.is_confirmed), (duplicates.METHOD_MANUAL, 1))
		self.assertEqual(
			frappe.db.get_value(
				"Grievance Duplicate", {"grievance": dup.name, "duplicate_of": main.name}, "is_confirmed"
			),
			0,
		)

	def test_a_ticket_cannot_duplicate_itself_or_another_duplicate(self):
		main, dup = self._pair()
		third = self._case()
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name)
		with _keep_transaction():
			itself = grievance.confirm_duplicate(ticket_number=third.name, main_ticket=third.name)
			chained = grievance.confirm_duplicate(ticket_number=third.name, main_ticket=dup.name)
		self.assertEqual(itself["code"], "VALIDATION_ERROR", msg=itself)
		self.assertEqual(chained["code"], "VALIDATION_ERROR", msg=chained)
		self.assertEqual(frappe.db.get_value("Grievance", third.name, "status"), C.STATE_SUBMITTED)

	def test_the_main_ticket_must_be_the_same_farmers(self):
		theirs = self._case(submitter=self.other_farmer)
		dup = self._case()
		with _keep_transaction():
			result = grievance.confirm_duplicate(ticket_number=dup.name, main_ticket=theirs.name)
		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
		self.assertEqual(frappe.db.get_value("Grievance", dup.name, "status"), C.STATE_SUBMITTED)
		self.assertIsNone(frappe.db.get_value("Grievance", dup.name, "duplicate_of"))

	def test_an_anonymous_ticket_has_no_farmer_to_share_a_main_ticket_with(self):
		anonymous = self._case(submitter=None)
		other = self._case(submitter=None)
		with _keep_transaction():
			result = grievance.confirm_duplicate(ticket_number=anonymous.name, main_ticket=other.name)
		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)

	def test_the_main_ticket_cannot_be_closed_or_rejected(self):
		dup = self._case()
		for state in (C.STATE_CLOSED, C.STATE_REJECTED):
			finished = self._case(workflow_state=state)
			with _keep_transaction():
				result = grievance.confirm_duplicate(ticket_number=dup.name, main_ticket=finished.name)
			self.assertEqual(result["code"], "VALIDATION_ERROR", msg=(state, result))
		self.assertEqual(frappe.db.get_value("Grievance", dup.name, "status"), C.STATE_SUBMITTED)

	def test_a_resolved_ticket_may_be_the_main_ticket(self):
		resolved = self._case(workflow_state=C.STATE_RESOLVED)
		dup = self._case()
		result = grievance.confirm_duplicate(ticket_number=dup.name, main_ticket=resolved.name)
		self.assertEqual(result["status"], "success", msg=result)

	def test_confirming_closes_the_other_pending_flags(self):
		first, second = self._case(), self._case()
		dup = self._case()
		duplicates.flag_possible_duplicates(dup)
		self.assertEqual(len(duplicates.pending_reviews(dup.name)), 2)

		grievance.confirm_duplicate(ticket_number=dup.name)

		self.assertEqual(duplicates.pending_reviews(dup.name), [])
		confirmed = frappe.get_doc("Grievance Duplicate", {"grievance": dup.name, "is_confirmed": 1})
		self.assertEqual(confirmed.duplicate_of, first.name)
		left = frappe.get_doc("Grievance Duplicate", {"grievance": dup.name, "duplicate_of": second.name})
		self.assertEqual((left.is_confirmed, left.is_dismissed), (0, 1))
		self.assertIn("Superseded", left.decision_note)
		self.assertEqual(left.decided_by, "Administrator")

	def test_a_confirmed_duplicate_cannot_be_dismissed(self):
		_main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name)
		notes = frappe.db.count("Grievance Timeline", {"grievance": dup.name})

		with _keep_transaction():
			result = grievance.dismiss_duplicate(ticket_number=dup.name, note="Changed my mind")

		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
		self.assertEqual(frappe.db.count("Grievance Timeline", {"grievance": dup.name}), notes)
		row = frappe.get_doc("Grievance Duplicate", {"grievance": dup.name})
		self.assertEqual((row.is_confirmed, row.is_dismissed), (1, 0))

	def test_no_evidence_can_be_added_to_a_confirmed_duplicate(self):
		_main, dup = self._pair()
		self.assertEqual(attachment._case_for_write(dup.name).name, dup.name)
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name)

		with self.assertRaises(frappe.ValidationError):
			attachment._case_for_write(dup.name)

	def test_the_ticket_export_shows_a_duplicate_closed_on_its_main_ticket(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name, note="Same delivery.")

		closure = sections_of(ticket_export.build_form(frappe.get_doc("Grievance", dup.name)))[12]
		rows = rows_of(closure)

		self.assertIsNone(closure["note"], "a duplicate is not left as never closed")
		self.assertTrue(rows["Closure Date"])
		self.assertEqual(
			rows["Closure Remarks"],
			f"Duplicate of {tn.display(main.name)}. Same delivery.",
		)
		self.assertEqual(rows["Current Status"], C.STATE_DUPLICATE)
		(closed,) = table_of(closure, "Closure History")["rows"]
		self.assertEqual(closed[1:3], [duplicates.ACTION_CONFIRM, C.STATE_DUPLICATE])

	def test_a_reopened_case_confirmed_as_a_duplicate_is_not_exported_as_reopened(self):
		_main, dup = self._pair()
		frappe.get_doc(
			{
				"doctype": "Grievance Status History",
				"grievance": dup.name,
				"from_status": C.STATE_RESOLVED,
				"to_status": C.STATE_REOPENED,
				"action": "Reopen",
				"reason": "The fertilizer still has not arrived.",
				"changed_by": "Administrator",
				"timestamp": frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1),
			}
		).insert(ignore_permissions=True)
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name)

		closure = sections_of(ticket_export.build_form(frappe.get_doc("Grievance", dup.name)))[12]

		self.assertNotIn("Reopened", dict(closure["checks"])["Final Status"])

	def test_opening_a_flagged_ticket_shows_the_flag(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)

		opened = grievance.timeline(dup.ticket_number or dup.name)["data"]

		flag = opened["possible_duplicate"]
		self.assertEqual(flag["ticket_number"], tn.display(main.name), msg=opened.get("possible_duplicate"))
		self.assertEqual(flag["match_level"], "High")
		self.assertTrue(flag["can_review"])
		self.assertEqual(flag["description"], main.description)
		self.assertIsNone(opened["duplicate_of"])

	def test_opening_a_confirmed_duplicate_names_its_main_ticket(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		grievance.confirm_duplicate(ticket_number=dup.name)

		opened = grievance.timeline(dup.ticket_number or dup.name)["data"]

		self.assertEqual(opened["status"], C.STATE_DUPLICATE)
		self.assertEqual(opened["duplicate_of"], tn.display(main.name))
		self.assertIsNone(opened["possible_duplicate"], "a ruled ticket is no longer flagged")

	def test_a_submitter_opening_their_flagged_ticket_does_not_see_the_flag(self):
		_main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)

		# Seen as a non-staff caller, without a new User: the suite creates enough users to
		# reach Frappe's user-creation throttle (60 an hour), and one more fails later tests.
		with patch.object(permissions, "is_staff", return_value=False):
			result = grievance.timeline(dup.ticket_number or dup.name)

		self.assertEqual(result["status"], "success", msg=result)
		self.assertIsNone(result["data"]["possible_duplicate"], "the flag is for officers only")

	def test_opening_an_unflagged_ticket_shows_no_flag(self):
		lone = self._case()
		opened = grievance.timeline(lone.ticket_number or lone.name)["data"]
		self.assertIsNone(opened["possible_duplicate"])
		self.assertIsNone(opened["duplicate_of"])

	def test_unreviewed_flags_of_the_old_detector_are_dismissed_by_the_patch(self):
		main, dup = self._pair()
		kept = self._case()
		legacy = {"doctype": "Grievance Duplicate", "detected_at": frappe.utils.now_datetime()}
		legacy["detection_method"] = "Identity + Type + Time Proximity"
		old = frappe.get_doc({**legacy, "grievance": dup.name, "duplicate_of": main.name}).insert()
		confirmed = frappe.get_doc(
			{**legacy, "grievance": kept.name, "duplicate_of": main.name, "is_confirmed": 1}
		).insert()
		new = duplicates.flag_possible_duplicates(dup)
		self.assertEqual(new, [], "the pair is already recorded by the old detector")
		self.assertEqual(len(duplicates.pending_reviews(dup.name)), 1)

		dismiss_legacy_duplicate_flags.execute()

		self.assertEqual(duplicates.pending_reviews(dup.name), [])
		old.reload()
		confirmed.reload()
		self.assertEqual((old.is_dismissed, old.is_confirmed), (1, 0))
		self.assertEqual((confirmed.is_dismissed, confirmed.is_confirmed), (0, 1))

	def test_confirming_needs_a_main_ticket_when_nothing_was_flagged(self):
		lone = self._case()
		with _keep_transaction():
			result = grievance.confirm_duplicate(ticket_number=lone.name)
		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)

	def test_dismissing_leaves_the_ticket_a_normal_one(self):
		"""A normal one is routed: a ruling on a ticket still waiting for a desk sends it to one."""
		_main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		install.seed_unrouted_desk()

		result = grievance.dismiss_duplicate(ticket_number=dup.name, note="Different plot")

		dup.reload()
		self.assertEqual(dup.workflow_state, C.STATE_ASSIGNED, msg=result)
		self.assertTrue(dup.assigned_to)
		self.assertIsNone(result["data"]["current_state"]["possible_duplicate"])
		self.assertEqual(duplicates.pending_reviews(dup.name), [])
		row = frappe.get_doc("Grievance Duplicate", {"grievance": dup.name})
		self.assertEqual((row.is_dismissed, row.is_confirmed), (1, 0))
		self.assertTrue(
			frappe.db.exists(
				"Grievance Timeline", {"grievance": dup.name, "entry_type": "note", "is_internal": 1}
			)
		)
		with _keep_transaction():
			again = grievance.dismiss_duplicate(ticket_number=dup.name)
		self.assertEqual(again["code"], "VALIDATION_ERROR", msg=again)

	def test_the_generic_action_endpoint_cannot_confirm_a_duplicate(self):
		_main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)
		with _keep_transaction():
			result = grievance.action(ticket_number=dup.name, action=duplicates.ACTION_CONFIRM, reason="Same")
		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
		offered = grievance._current_state(dup)["available_actions"]
		self.assertNotIn(duplicates.ACTION_CONFIRM, [a["action"] for a in offered])

	# What the officer and the farmer see

	def test_the_flag_tells_the_officer_what_matched(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)

		flag = grievance._current_state(dup)["possible_duplicate"]

		self.assertEqual(flag["ticket_number"], tn.display(main.name))
		self.assertEqual(flag["match_level"], "High")
		self.assertTrue(flag["can_review"])
		self.assertTrue(flag["same_submitter"] and flag["same_category"] and flag["same_type"])
		self.assertEqual(flag["description"], main.description)

	def test_the_list_shows_the_flag_then_the_main_ticket(self):
		main, dup = self._pair()
		duplicates.flag_possible_duplicates(dup)

		def listed():
			items = grievance.list_grievances(search=dup.name, page_size=5)["data"]["items"]
			return next(item for item in items if item["ticket_number"] == tn.display(dup.name))

		self.assertEqual(listed()["possible_duplicate_of"], tn.display(main.name))
		self.assertIsNone(listed()["duplicate_of"])
		grievance.confirm_duplicate(ticket_number=dup.name)
		after = listed()
		self.assertEqual(after["status"], "Duplicate")
		self.assertEqual(after["duplicate_of"], tn.display(main.name))
		self.assertIsNone(after["possible_duplicate_of"])

	def test_the_duplicate_status_is_shown_as_it_is(self):
		self.assertEqual(public_status(C.STATE_DUPLICATE), "Duplicate")

	# Dashboard

	def test_the_status_cards_count_duplicates_and_fold_more_info_needed_into_in_progress(self):
		def counts():
			cards = {card["status"]: card["count"] for card in get_status_summary()}
			return cards

		before = counts()
		self.assertNotIn("More Info Needed", before)
		main = self._case()
		self._case(workflow_state=C.STATE_IN_PROGRESS)
		self._case(workflow_state=C.STATE_MORE_INFO_NEEDED)
		self._case(workflow_state=C.STATE_DUPLICATE, duplicate_of=main.name)
		after = counts()

		self.assertEqual(after["Duplicate"] - before["Duplicate"], 1)
		self.assertEqual(after["In Progress"] - before["In Progress"], 2)
		self.assertEqual(after["Submitted"] - before["Submitted"], 1)
		self.assertEqual(after["All"] - before["All"], 4, "All includes duplicates")
		self.assertEqual(public_status(C.STATE_MORE_INFO_NEEDED), C.STATE_MORE_INFO_NEEDED)

	def test_the_dashboard_counts_the_duplicate_state(self):
		self.assertIn(C.STATE_DUPLICATE, dashboard_rollup.live_states())


class TestAskBeforeFiling(FrappeTestCase):
	"""A farmer on the web portal is asked, once, before a repeat of the last day is filed."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.farmer = a_submitter_profile()
		self.category = a_service_category()
		self.type = a_grievance_type()
		self.area = a_leaf_area()
		self.asking_was = frappe.db.get_single_value("Grievance Settings", "ask_before_filing_duplicate")
		frappe.db.set_single_value("Grievance Settings", "ask_before_filing_duplicate", 1)
		frappe.clear_messages()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Grievance Settings", "ask_before_filing_duplicate", self.asking_was or 0)
		for name in frappe.get_all("Grievance", filters={"submitter": self.farmer}, pluck="name"):
			discard_grievance(name)
		frappe.delete_doc("Grievance Submitter Profile", self.farmer, force=True, ignore_permissions=True)

	def _file(self, **overrides):
		values = {
			"submitter": self.farmer,
			"description": FERTILIZER,
			"service_category": self.category,
			"grievance_type": self.type,
			"administrative_area": self.area,
			"submission_channel": "Web Portal",
		}
		values.update(overrides)
		with _keep_transaction():
			return grievance.submit(**values)

	def _filed(self):
		"""Names filed so far, oldest first."""
		return frappe.get_all(
			"Grievance", filters={"submitter": self.farmer}, order_by="creation asc, name asc", pluck="name"
		)

	def _run_flagging(self):
		"""Run the after-commit flagging job for the newest filing, as the queue would."""
		duplicates.flag_grievance_job(self._filed()[-1])

	def test_a_similar_grievance_of_the_last_day_is_asked_about_not_filed(self):
		self._file()
		result = self._file()

		self.assertEqual(result["code"], "POSSIBLE_DUPLICATE", msg=result)
		self.assertEqual(frappe.response.get("http_status_code"), 409)
		(found,) = result["details"]["possible_duplicates"]
		self.assertEqual(found["match_level"], "High")
		self.assertEqual(found["description"], FERTILIZER)
		self.assertEqual(len(self._filed()), 1, "nothing was filed by the question")

	def test_saying_it_is_new_files_it_and_it_is_still_flagged_for_an_officer(self):
		self._file()
		result = self._file(confirm_new=1)

		self.assertEqual(result["status"], "success", msg=result)
		self.assertEqual(len(self._filed()), 2)
		self._run_flagging()
		self.assertEqual(len(duplicates.pending_reviews(self._filed()[-1])), 1)

	def test_the_question_is_off_until_the_setting_is_turned_on(self):
		frappe.db.set_single_value("Grievance Settings", "ask_before_filing_duplicate", 0)
		self._file()
		result = self._file()

		self.assertEqual(result["status"], "success", msg=result)
		self.assertEqual(len(self._filed()), 2)
		self._run_flagging()
		self.assertEqual(
			len(duplicates.pending_reviews(self._filed()[-1])), 1, "an officer is still told about it"
		)

	def test_a_one_way_channel_is_never_asked_but_is_flagged(self):
		self._file()
		result = self._file(submission_channel="IVR Helpline")

		self.assertEqual(result["status"], "success", msg=result)
		self.assertEqual(len(self._filed()), 2)

	def test_filing_a_repeat_flags_it_against_the_first_for_an_officer(self):
		first = self._file(submission_channel="IVR Helpline")
		second = self._file(submission_channel="IVR Helpline")

		self._run_flagging()
		earlier, later = self._filed()
		(review,) = duplicates.pending_reviews(later)
		self.assertEqual((review.duplicate_of, review.match_level), (earlier, "High"))
		self.assertEqual(duplicates.pending_reviews(earlier), [])
		self.assertEqual(second["data"]["status"], C.STATE_SUBMITTED, "the ticket is untouched")
		flag = grievance._current_state(frappe.get_doc("Grievance", later))["possible_duplicate"]
		self.assertEqual(flag["ticket_number"], first["data"]["ticket_number"])

	def test_a_fault_in_flagging_does_not_lose_the_grievance(self):
		self._file()
		with (
			patch.object(duplicates, "flag_possible_duplicates", side_effect=RuntimeError("boom")),
			patch.object(frappe, "log_error") as log_error,
		):
			result = self._file(submission_channel="IVR Helpline")
			self._run_flagging()

		self.assertEqual(result["status"], "success", msg=result)
		self.assertEqual(len(self._filed()), 2)
		self.assertEqual(duplicates.pending_reviews(self._filed()[-1]), [])
		titles = [call.kwargs["title"] for call in log_error.call_args_list]
		self.assertEqual(sum("possible duplicates" in title for title in titles), 1, titles)

	def test_a_ticket_older_than_a_day_is_left_to_the_officers_flag(self):
		self._file()
		older = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-25)
		frappe.db.set_value("Grievance", self._filed()[0], "creation", older, update_modified=False)

		self.assertEqual(self._file()["status"], "success")
		self.assertEqual(len(self._filed()), 2)

	def test_a_different_issue_is_not_asked_about(self):
		self._file()
		self.assertEqual(self._file(description=CANAL)["status"], "success")

	def test_an_anonymous_filing_has_nobody_to_ask(self):
		self._file()
		self.assertEqual(
			self._file(submitter=None, submitter_name="Anon", contact_mobile="+251911000001")["status"],
			"success",
		)
