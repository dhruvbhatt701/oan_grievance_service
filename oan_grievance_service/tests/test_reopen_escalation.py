# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""STG-521: a reopened case is escalated one level above whoever resolved it last.

The reopen is sent the way the portal sends it: the Reopen action with a reason, nothing else.

The chain is the seeded one: L1 Kebele Nodal Officer, L2 Woreda Nodal Officer, L3 Zonal Head,
L4 the regional head. One desk for a department of its own holds an officer at each level, so
nothing else on the site can answer for a rung.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from oan_grievance_service.api.v1 import grievance as grievance_api
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle, sla
from oan_grievance_service.services.ticket_number import region_of
from oan_grievance_service.setup import install
from oan_grievance_service.tests.fixtures import (
	a_desk,
	a_grievance,
	a_leaf_area,
	a_service_category,
	a_unique_department,
	a_user_with_role,
	an_administrative_area,
)
from oan_grievance_service.tests.test_category_assignment import _keep_transaction
from oan_grievance_service.tests.test_classification import a_type

L1, L2, L3, L4 = "nodal_officer", "senior_nodal_officer", "zonal_head", "department_head"


class TestReopenWithEscalation(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		install.seed_workflow()
		install.seed_role_levels()
		frappe.cache.delete_value("grievance_role_level_chain")

		self.dept = a_unique_department("Reopen")
		self.category = a_service_category()
		self.officer = {
			level: a_user_with_role(C.ROLE_OFFICER, level.split("_")[0]) for level in (L1, L2, L3, L4)
		}
		desk = a_desk(self.dept, self.category, L1, (self.officer[L1], None))
		desk = frappe.get_doc("Grievance RBAC Assignment", desk)
		for level in (L2, L3, L4):
			desk.append(
				"officers",
				{"user": self.officer[level], "role_level": level, "is_primary": 0, "active": 1},
			)
		desk.save(ignore_permissions=True)
		self.desk = desk

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.cache.delete_value("grievance_role_level_chain")
		super().tearDown()

	# Helpers -----------------------------------------------------------------

	def _case(self, assigned_to, **values):
		case = a_grievance(
			service_category=self.category, assigned_dept=self.dept, assigned_to=assigned_to, **values
		)
		lifecycle.transition(case, "Assign", reason="Routed for the test")
		return case.name

	def _doc(self, name):
		return frappe.get_doc("Grievance", name)

	def _resolve_as(self, name, user):
		frappe.set_user(user)
		try:
			lifecycle.transition(self._doc(name), "Resolve", reason="Looked into it and fixed it.")
		finally:
			frappe.set_user("Administrator")

	def _reopen(self, name):
		with _keep_transaction():
			res = grievance_api.action(name, action="Reopen", reason="Still not fixed.")
		self.assertEqual(res["status"], "success", res)
		return self._doc(name)

	def _row_of(self, level):
		return next(row for row in self.desk.officers if row.role_level == level)

	def _set_row(self, level, **values):
		frappe.db.set_value("Grievance RBAC Assignment Officer", self._row_of(level).name, values)

	def _notified(self, name, user, event):
		return frappe.db.exists(
			"Grievance Notification Log", {"grievance": name, "recipient": user, "event": event}
		)

	def _reopen_recipients(self, name):
		"""Who got the officer's Grievance Reopened. The submitter's SMS (N8) is checked on its own."""
		return set(
			frappe.get_all(
				"Grievance Notification Log",
				filters={"grievance": name, "event": C.EVENT_REOPENED, "channel": ["!=", "SMS"]},
				pluck="recipient",
			)
		)

	def _escalation_entries(self, name):
		return frappe.get_all(
			"Grievance Timeline",
			filters={"grievance": name, "entry_type": "escalation"},
			pluck="body",
			order_by="creation asc",
		)

	def _climbs(self, from_level, to_level):
		name = self._case(self.officer[from_level])
		self._resolve_as(name, self.officer[from_level])
		doc = self._reopen(name)

		self.assertEqual(doc.workflow_state, C.STATE_REOPENED)
		self.assertEqual(doc.assigned_to, self.officer[to_level])
		self.assertEqual(doc.escalated, 1)
		rung_name = frappe.db.get_value("Grievance Role Level", to_level, "level_name")
		self.assertIn(f"escalated to {rung_name}", self._escalation_entries(name)[-1])
		# One Grievance Reopened, to the officer the case landed on: not to the officer it left, and
		# no second, Manual Escalation, message.
		self.assertEqual(self._reopen_recipients(name), {self.officer[to_level]})
		self.assertFalse(self._notified(name, self.officer[to_level], C.EVENT_MANUAL_ESCALATION))
		versions = frappe.get_all(
			"Version", filters={"ref_doctype": "Grievance", "docname": name}, pluck="data"
		)
		self.assertTrue(
			any(self.officer[from_level] in data and self.officer[to_level] in data for data in versions),
			"the reassignment is in the Version log",
		)
		return name

	# Each level --------------------------------------------------------------

	def test_l1_resolution_goes_to_l2(self):
		self._climbs(L1, L2)

	def test_l2_resolution_goes_to_l3(self):
		self._climbs(L2, L3)

	def test_l3_resolution_goes_to_l4(self):
		self._climbs(L3, L4)

	# Top of the chain ----------------------------------------------------------

	def test_l4_resolution_stays_with_l4(self):
		"""Proposed behaviour, pending the business decision: nobody is above L4, so it stays there."""
		name = self._case(self.officer[L4])
		self._resolve_as(name, self.officer[L4])
		doc = self._reopen(name)

		self.assertEqual(doc.assigned_to, self.officer[L4])
		self.assertEqual(doc.escalated, 1)
		self.assertIn("no higher authority", self._escalation_entries(name)[-1])
		self.assertEqual(self._reopen_recipients(name), {self.officer[L4]})

	def test_l4_resolving_for_l1_keeps_the_case_with_l4(self):
		"""The climb starts from the resolver, not the assignee, so the case never drops back to L1."""
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L4])
		doc = self._reopen(name)
		self.assertEqual(doc.assigned_to, self.officer[L4])

	# Vacancy -----------------------------------------------------------------

	def test_an_l2_on_leave_is_skipped_for_l3(self):
		self._set_row(L2, on_leave=1)
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		self.assertEqual(self._reopen(name).assigned_to, self.officer[L3])

	def test_an_inactive_l2_seat_is_skipped_for_l3(self):
		self._set_row(L2, active=0)
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		self.assertEqual(self._reopen(name).assigned_to, self.officer[L3])

	def test_a_disabled_l2_is_skipped_for_l3(self):
		frappe.db.set_value("User", self.officer[L2], "enabled", 0)
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		self.assertEqual(self._reopen(name).assigned_to, self.officer[L3])

	def test_leave_on_a_retired_desk_does_not_count(self):
		"""Only live desk rows say whether someone is on leave today."""
		other = a_desk(a_unique_department("Old"), self.category, L2, (self.officer[L2], None))
		frappe.db.set_value("Grievance RBAC Assignment Officer", {"parent": other}, "on_leave", 1)
		frappe.db.set_value("Grievance RBAC Assignment", other, "active", 0)

		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		self.assertEqual(self._reopen(name).assigned_to, self.officer[L2])

	def test_breach_escalation_skips_the_same_officers(self):
		"""SLA breach escalation shares the rule: an L2 on leave is passed over for L3."""
		self._set_row(L2, on_leave=1)
		doc = self._doc(self._case(self.officer[L1]))
		self.assertEqual(sla.escalate(doc, reason="SLA breached"), self.officer[L3])
		self.assertEqual(self._doc(doc.name).assigned_to, self.officer[L3])

	def test_a_kebele_level_senior_is_skipped_on_a_sensitive_case(self):
		region = region_of(a_leaf_area())["name"]
		woreda = an_administrative_area(f"Woreda {frappe.generate_hash(length=5)}", region, "Woreda")
		kebele = an_administrative_area(f"Kebele {frappe.generate_hash(length=5)}", woreda, "Kebele")
		self._set_row(L3, administrative_area=kebele)

		# Control: on an ordinary case the Kebele-seated L3 does take it.
		ordinary = self._case(self.officer[L2], administrative_area=kebele)
		self._resolve_as(ordinary, self.officer[L2])
		self.assertEqual(self._reopen(ordinary).assigned_to, self.officer[L3])

		sensitive = self._case(
			self.officer[L2], administrative_area=kebele, grievance_type=a_type(self.category, sensitive=1)
		)
		self.assertEqual(self._doc(sensitive).is_sensitive, 1)
		self._resolve_as(sensitive, self.officer[L2])
		self.assertEqual(self._reopen(sensitive).assigned_to, self.officer[L4])

	def test_with_everyone_above_away_the_case_stays_with_the_resolver(self):
		"""Proposed behaviour, pending the business decision."""
		self._set_row(L3, on_leave=1)
		self._set_row(L4, on_leave=1)
		name = self._case(self.officer[L2])
		self._resolve_as(name, self.officer[L2])
		doc = self._reopen(name)
		self.assertEqual(doc.assigned_to, self.officer[L2])
		self.assertEqual(doc.escalated, 1)
		self.assertIn("no higher authority", self._escalation_entries(name)[-1])

	# SLA window and logging ----------------------------------------------------

	def test_the_reopen_keeps_its_new_sla_window_and_logs_the_escalation(self):
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		reopened_from = get_datetime(frappe.utils.now_datetime())
		with patch.object(sla.oplog, "emit", wraps=sla.oplog.emit) as emit:
			doc = self._reopen(name)

		events = [call.args[0] for call in emit.call_args_list]
		self.assertIn("sla.clock_reset", events)
		self.assertIn("escalation.reopen_escalated", events)
		self.assertGreaterEqual(get_datetime(doc.sla_start_at), reopened_from)
		self.assertIsNotNone(doc.sla_due_date)
		# The breach ladder still waits for the new deadline, and then climbs from the new officer.
		self.assertEqual(get_datetime(doc.next_escalation_at), get_datetime(doc.sla_due_date))

	# Repeated reopens ----------------------------------------------------------

	def test_each_reopen_climbs_one_more_level(self):
		name = self._case(self.officer[L1])
		for resolver, expected in ((L1, L2), (L2, L3), (L3, L4), (L4, L4)):
			self._resolve_as(name, self.officer[resolver])
			doc = self._reopen(name)
			self.assertEqual(doc.assigned_to, self.officer[expected], f"resolved by {resolver}")
		self.assertEqual(doc.reopen_count, 4)
		self.assertEqual(len(self._escalation_entries(name)), 4)

	# Outside the hierarchy -----------------------------------------------------

	def test_a_resolver_outside_the_chain_climbs_from_the_case_officer(self):
		"""An admin holds no rung: the climb starts from the officer who held the case."""
		name = self._case(self.officer[L1])
		self._resolve_as(name, "Administrator")
		self.assertEqual(self._reopen(name).assigned_to, self.officer[L2])

	# Grievance Settings switch -------------------------------------------------

	def test_with_the_setting_off_a_reopen_keeps_its_officer(self):
		frappe.db.set_single_value("Grievance Settings", "reopen_escalation_enabled", 0)
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		doc = self._reopen(name)

		self.assertEqual(doc.workflow_state, C.STATE_REOPENED)
		self.assertEqual(doc.assigned_to, self.officer[L1])
		self.assertFalse(doc.escalated)
		self.assertEqual(self._escalation_entries(name), [])
		# The reopen still notifies as it did before escalation existed.
		self.assertTrue(self._reopen_recipients(name))

	def test_a_setting_never_stored_means_on(self):
		from oan_grievance_service.grievance_management.doctype.grievance_settings.grievance_settings import (
			reopen_escalation_enabled,
		)

		frappe.db.delete("Singles", {"doctype": "Grievance Settings", "field": "reopen_escalation_enabled"})
		self.assertTrue(reopen_escalation_enabled())
		frappe.db.set_single_value("Grievance Settings", "reopen_escalation_enabled", 0)
		self.assertFalse(reopen_escalation_enabled())

	# Only a reopen escalates --------------------------------------------------

	def test_closing_a_resolved_case_does_not_escalate(self):
		name = self._case(self.officer[L1])
		self._resolve_as(name, self.officer[L1])
		with _keep_transaction():
			res = grievance_api.action(name, action="Close Case", reason="Satisfied.")
		self.assertEqual(res["status"], "success", res)
		doc = self._doc(name)
		self.assertEqual(doc.assigned_to, self.officer[L1])
		self.assertFalse(doc.escalated)
		self.assertEqual(self._escalation_entries(name), [])
		self.assertFalse(self._notified(name, self.officer[L2], C.EVENT_MANUAL_ESCALATION))
