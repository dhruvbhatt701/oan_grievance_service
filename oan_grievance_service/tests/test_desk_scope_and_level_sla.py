# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""Staging test report of 26 Nov 2026: who sees a case, who it may be handed to, and its SLA by level.

The desks mirror the seeded ones that went wrong. A Kebele head sits at L1 on a Land desk and on a
Credit desk, and on the Credit desk reports to the Credit officer; the Credit officer must not see
the Land case. A Livestock desk has two specialists at L2 (the Livestock SMS, whom the DA reports
to, and the Veterinarian); both must see the DA's case.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime, now_datetime

from oan_grievance_service import permissions
from oan_grievance_service.api.v1 import grievance as grievance_api
from oan_grievance_service.api.v1._options import STATUS_ESCALATED, public_status
from oan_grievance_service.api.v1.profile import resolve_user_profile_hook
from oan_grievance_service.grievance_access_control.doctype.grievance_rbac_assignment.grievance_rbac_assignment import (
	home_seat,
)
from oan_grievance_service.services import constants as C
from oan_grievance_service.services import dashboard, lifecycle, reassignment, sla
from oan_grievance_service.setup import install
from oan_grievance_service.tests.fixtures import a_grievance, a_unique_department, a_user_with_role
from oan_grievance_service.tests.test_category_assignment import _keep_transaction
from oan_grievance_service.tests.test_classification import a_category, a_type, add_rules

L1, L2, L3, L4 = "nodal_officer", "senior_nodal_officer", "zonal_head", "department_head"
RULE = "Grievance SLA Rule"


def _desk(department, category, rows):
	"""An active desk; `rows` are (user, level, reports_to) and the first is the desk's primary."""
	return (
		frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": department,
				"category_scope": category,
				"active": 1,
				"effective_from": frappe.utils.today(),
				"officers": [
					{
						"user": user,
						"role_level": level,
						"reports_to": reports_to,
						"is_primary": 1 if index == 0 else 0,
						"active": 1,
					}
					for index, (user, level, reports_to) in enumerate(rows)
				],
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class _Base(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		install.seed_workflow()
		install.seed_role_levels()
		install.seed_notifications()
		frappe.cache.delete_value("grievance_role_level_chain")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.cache.delete_value("grievance_role_level_chain")
		super().tearDown()

	def officer(self, label):
		return a_user_with_role(C.ROLE_OFFICER, label)

	def type_of(self, category):
		"""The category's first type, made on first use."""
		return frappe.db.get_value("Grievance Type", {"service_category": category}, "name") or a_type(
			category
		)

	def case(self, category, department, assigned_to, **values):
		"""An Assigned case in `category`, of a type of its own unless one is given."""
		values.setdefault("grievance_type", self.type_of(category))
		doc = a_grievance(
			service_category=category, assigned_dept=department, assigned_to=assigned_to, **values
		)
		lifecycle.transition(doc, "Assign", reason="Routed for the test")
		return frappe.get_doc("Grievance", doc.name)

	def visible_to(self, user, name):
		frappe.set_user(user)
		try:
			return bool(frappe.get_list("Grievance", filters={"name": name}, pluck="name"))
		finally:
			frappe.set_user("Administrator")


class TestDeskTeams(_Base):
	"""Item 3: an officer reads the cases below them on their own desks, and nothing else."""

	def setUp(self):
		super().setUp()
		self.land_dept, self.credit_dept = a_unique_department("Land"), a_unique_department("Credit")
		self.land, self.credit = a_category(), a_category()
		self.kao, self.land_expert, self.credit_officer = (
			self.officer("kao"),
			self.officer("land"),
			self.officer("credit"),
		)
		_desk(self.land_dept, self.land, [(self.kao, L1, self.land_expert), (self.land_expert, L2, None)])
		_desk(
			self.credit_dept,
			self.credit,
			[(self.kao, L1, self.credit_officer), (self.credit_officer, L2, None)],
		)

		self.vet_dept, self.livestock = a_unique_department("Livestock"), a_category()
		self.da, self.sms, self.vet = self.officer("da"), self.officer("sms"), self.officer("vet")
		_desk(
			self.vet_dept,
			self.livestock,
			[(self.da, L1, self.sms), (self.sms, L2, None), (self.vet, L2, None)],
		)

	def test_a_shared_officer_does_not_carry_other_desks_cases_up(self):
		land_case = self.case(self.land, self.land_dept, self.kao)
		self.assertTrue(self.visible_to(self.land_expert, land_case.name))
		self.assertFalse(self.visible_to(self.credit_officer, land_case.name))
		self.assertFalse(permissions.has_grievance_permission(land_case, "read", self.credit_officer))
		self.assertFalse(permissions.has_grievance_permission(land_case, "write", self.credit_officer))

	def test_every_officer_above_on_the_desk_sees_it_whoever_the_row_reports_to(self):
		livestock_case = self.case(self.livestock, self.vet_dept, self.da)
		for user in (self.sms, self.vet):
			self.assertTrue(self.visible_to(user, livestock_case.name), user)
			self.assertTrue(permissions.has_grievance_permission(livestock_case, "write", user), user)

	def test_peers_and_juniors_do_not_see_each_others_cases(self):
		sms_case = self.case(self.livestock, self.vet_dept, self.sms)
		self.assertFalse(self.visible_to(self.vet, sms_case.name))
		self.assertFalse(self.visible_to(self.da, sms_case.name))
		self.assertTrue(self.visible_to(self.sms, sms_case.name))

	def test_the_case_list_lets_an_officer_act_on_desk_cases_only(self):
		credit_case = self.case(self.credit, self.credit_dept, self.kao)
		land_case = self.case(self.land, self.land_dept, self.kao)
		may_act = permissions.officer_write_check(self.credit_officer)
		self.assertTrue(may_act(credit_case.as_dict()))
		self.assertFalse(may_act(land_case.as_dict()))

	def test_the_dashboard_scope_follows_the_desks(self):
		clauses = dashboard.caller_scope(self.credit_officer)
		team = [clause for clause in clauses if clause.assigned_to and self.kao in clause.assigned_to]
		self.assertEqual(
			[(c.service_category, c.assigned_dept) for c in team], [(self.credit, self.credit_dept)]
		)


class TestOfficerHandover(_Base):
	"""Item 6: an officer keeps the category; the next level up, or down, is in line, and any other
	pick waits for the receiving head."""

	def setUp(self):
		super().setUp()
		self.dept, self.category = a_unique_department("Hand"), a_category()
		self.other_category = a_category()
		# One type, so a move to the category needs no type named.
		self.type_of(self.other_category)
		self.user = {level: self.officer(level.split("_")[0]) for level in (L1, L2, L3, L4)}
		self.peer_l2 = self.officer("peer")
		_desk(
			self.dept,
			self.category,
			[
				(self.user[L1], L1, None),
				(self.user[L2], L2, None),
				(self.peer_l2, L2, None),
				(self.user[L3], L3, None),
				(self.user[L4], L4, None),
			],
		)
		_desk(self.dept, self.other_category, [(self.user[L1], L1, None)])

	def reassign_as(self, user, **targets):
		frappe.set_user(user)
		try:
			with _keep_transaction():
				return grievance_api.reassign(self.doc.name, **targets)
		finally:
			frappe.set_user("Administrator")

	def test_next_level_is_every_officer_on_the_rung_above(self):
		self.doc = self.case(self.category, self.dept, self.user[L1])
		self.assertEqual(reassignment.next_level_officers(self.doc), {self.user[L2], self.peer_l2})

	def test_a_pick_out_of_line_always_waits_for_the_head(self):
		"""Skipping a level is not refused, but never applies directly, even on a desk that needs
		no approval."""
		frappe.db.set_value(
			"Grievance RBAC Assignment",
			{"department_scope": self.dept, "category_scope": self.category},
			"reassignment_requires_approval",
			0,
		)
		self.doc = self.case(self.category, self.dept, self.user[L1])
		res = self.reassign_as(self.user[L1], target_officer=self.user[L3], target_department=self.dept)
		self.assertEqual(res["status"], "success", res)
		self.assertEqual(frappe.db.get_value("Grievance", self.doc.name, "assigned_to"), self.user[L1])
		pending = frappe.db.get_value(
			"Grievance Change Request", {"grievance": self.doc.name, "status": "Pending"}, "pending_with"
		)
		self.assertEqual(pending, self.user[L4])

	def test_the_next_level_up_is_in_line(self):
		frappe.db.set_value(
			"Grievance RBAC Assignment",
			{"department_scope": self.dept, "category_scope": self.category},
			"reassignment_requires_approval",
			0,
		)
		self.doc = self.case(self.category, self.dept, self.user[L1])
		res = self.reassign_as(self.user[L1], target_officer=self.peer_l2, target_department=self.dept)
		self.assertEqual(res["status"], "success", res)
		self.assertEqual(frappe.db.get_value("Grievance", self.doc.name, "assigned_to"), self.peer_l2)

	def test_an_officer_cannot_change_the_category(self):
		self.doc = self.case(self.category, self.dept, self.user[L1])
		res = self.reassign_as(self.user[L1], target_category=self.other_category)
		self.assertEqual(res["code"], "PERMISSION_DENIED", res)
		self.assertEqual(frappe.db.get_value("Grievance", self.doc.name, "service_category"), self.category)

	def test_an_administrator_still_moves_anything(self):
		self.doc = self.case(self.category, self.dept, self.user[L1])
		admin = a_user_with_role(C.ROLE_ADMIN, "admin")
		res = self.reassign_as(admin, target_category=self.other_category)
		self.assertEqual(res["status"], "success", res)

	def test_a_senior_may_pass_a_case_down_a_junior_may_not_pass_it_sideways(self):
		self.doc = self.case(self.category, self.dept, self.user[L3])
		self.assertIn(self.user[L1], reassignment.officer_move_limits(self.doc, self.user[L4]))
		self.assertIn(self.user[L1], reassignment.officer_move_limits(self.doc, self.user[L3]))
		self.doc = self.case(self.category, self.dept, self.user[L2])
		self.assertNotIn(self.peer_l2, reassignment.officer_move_limits(self.doc, self.user[L2]))

	def test_the_picker_offers_the_next_level_and_those_below(self):
		self.doc = self.case(self.category, self.dept, self.user[L2])
		frappe.set_user(self.user[L2])
		try:
			data = grievance_api.options(ticket_number=self.doc.name)["data"]
		finally:
			frappe.set_user("Administrator")
		# L3 is next and L1 is below; not the peer beside them, nor the category admin above L3.
		self.assertEqual({o["user_id"] for o in data["officers"]}, {self.user[L3], self.user[L1]})
		self.assertFalse(data["can_change_category"])
		self.assertTrue(grievance_api.options(ticket_number=self.doc.name)["data"]["can_change_category"])

	def test_the_category_admin_is_offered_when_they_are_next(self):
		self.doc = self.case(self.category, self.dept, self.user[L3])
		frappe.set_user(self.user[L3])
		try:
			officers = grievance_api.options(ticket_number=self.doc.name)["data"]["officers"]
		finally:
			frappe.set_user("Administrator")
		self.assertIn(self.user[L4], {o["user_id"] for o in officers})


class TestLevelSLA(_Base):
	"""Items 4 and 5: a case runs on its level's working days, and a new level starts a new window."""

	def setUp(self):
		super().setUp()
		self.dept, self.category = a_unique_department("SLA"), a_category(sla_days=17)
		self.type = a_type(self.category)
		add_rules(
			self.category,
			[
				{"rule_label": "L1", "grievance_type": self.type, "role_level": L1, "sla_days": 7},
				{"rule_label": "L2", "grievance_type": self.type, "role_level": L2, "sla_days": 10},
			],
		)
		self.user = {level: self.officer(level.split("_")[0]) for level in (L1, L2, L3, L4)}
		_desk(self.dept, self.category, [(self.user[level], level, None) for level in (L1, L2, L3, L4)])

	def leveled_case(self, **values):
		return self.case(self.category, self.dept, self.user[L1], grievance_type=self.type, **values)

	def test_a_case_starts_on_its_level_days(self):
		doc = self.leveled_case()
		self.assertEqual(doc.sla_days, 7)
		self.assertEqual(get_datetime(doc.next_escalation_at), get_datetime(doc.sla_due_date))

	def test_a_type_with_no_level_rule_keeps_the_category_days(self):
		doc = self.case(self.category, self.dept, self.user[L1], grievance_type=a_type(self.category))
		self.assertEqual(doc.sla_days, 17)

	def test_a_missed_deadline_climbs_and_starts_the_next_levels_window(self):
		doc = self.leveled_case()
		before = now_datetime()
		self.assertEqual(sla.escalate(doc, reason="SLA breached"), self.user[L2])
		doc.reload()
		self.assertEqual(doc.sla_days, 10)
		self.assertGreaterEqual(get_datetime(doc.sla_start_at), get_datetime(before).replace(microsecond=0))
		self.assertEqual(get_datetime(doc.next_escalation_at), get_datetime(doc.sla_due_date))
		self.assertEqual(doc.get(C.REMINDER_SENT_FIELD) or 0, 0)

	def test_a_level_with_no_days_of_its_own_keeps_its_escalation_hours(self):
		doc = self.leveled_case()
		sla.escalate(doc)
		due_at_l2 = doc.sla_due_date
		self.assertEqual(sla.escalate(doc), self.user[L3])
		doc.reload()
		self.assertEqual(get_datetime(doc.sla_due_date), get_datetime(due_at_l2))
		hours = frappe.db.get_value("Grievance Role Level", L3, "escalation_hours")
		if hours:
			self.assertGreater(get_datetime(doc.next_escalation_at), now_datetime())

	def reopen(self, doc):
		frappe.set_user(self.user[L1])
		try:
			lifecycle.transition(doc, "Resolve", reason="Looked into it and fixed it.")
		finally:
			frappe.set_user("Administrator")
		with _keep_transaction():
			res = grievance_api.action(doc.name, action="Reopen", reason="Still not fixed.")
		self.assertEqual(res["status"], "success", res)
		return res, frappe.get_doc("Grievance", doc.name)

	def test_a_reopen_shows_escalated_one_level_up_with_a_new_window(self):
		res, doc = self.reopen(self.leveled_case())
		self.assertEqual(doc.assigned_to, self.user[L2])
		self.assertEqual(doc.sla_days, 10)
		self.assertEqual(res["data"]["current_state"]["status"], STATUS_ESCALATED)

	def test_a_reopen_tells_the_submitter_who_looks_again_and_by_when(self):
		_res, doc = self.reopen(self.leveled_case())
		sms = frappe.get_all(
			"Grievance Notification Log",
			filters={"grievance": doc.name, "event": C.EVENT_REOPENED, "channel": "SMS"},
			fields=["recipient", "message"],
		)
		self.assertEqual([row.recipient for row in sms], [doc.contact_mobile])
		self.assertIn(self.dept, sms[0].message)

	def test_a_sensitive_reopen_tells_the_submitter_only_the_reference_and_pin(self):
		sensitive_type = a_type(self.category, sensitive=1)
		doc = self.case(self.category, self.dept, self.user[L2], grievance_type=sensitive_type)
		self.assertTrue(doc.is_sensitive)
		frappe.set_user(self.user[L2])
		try:
			lifecycle.transition(doc, "Resolve", reason="Looked into it and fixed it.")
		finally:
			frappe.set_user("Administrator")
		with _keep_transaction():
			grievance_api.action(doc.name, action="Reopen", reason="Still not fixed.")
		message = frappe.db.get_value(
			"Grievance Notification Log",
			{"grievance": doc.name, "event": C.EVENT_REOPENED, "channel": "SMS"},
			"message",
		)
		self.assertIn("PIN", message)
		self.assertNotIn(self.dept, message)


class TestEscalateEndpoint(_Base):
	"""Item 6: an officer escalates a case one level up themselves."""

	def setUp(self):
		super().setUp()
		self.dept, self.category = a_unique_department("Esc"), a_category()
		self.user = {level: self.officer(level.split("_")[0]) for level in (L1, L2, L3, L4)}
		_desk(self.dept, self.category, [(self.user[level], level, None) for level in (L1, L2, L3, L4)])

	def escalate_as(self, user, name, **values):
		frappe.set_user(user)
		try:
			with _keep_transaction():
				return grievance_api.escalate(name, **values)
		finally:
			frappe.set_user("Administrator")

	def test_an_officer_escalates_to_the_next_level(self):
		doc = self.case(self.category, self.dept, self.user[L1])
		res = self.escalate_as(self.user[L1], doc.name, reason="Needs the Woreda expert.")
		self.assertEqual(res["status"], "success", res)
		doc.reload()
		self.assertEqual(doc.assigned_to, self.user[L2])
		self.assertTrue(doc.escalated)
		self.assertEqual(res["data"]["current_state"]["status"], STATUS_ESCALATED)
		self.assertTrue(
			frappe.db.exists(
				"Grievance Notification Log",
				{"grievance": doc.name, "event": C.EVENT_MANUAL_ESCALATION, "recipient": self.user[L2]},
			)
		)
		bodies = frappe.get_all(
			"Grievance Timeline", filters={"grievance": doc.name, "entry_type": "escalation"}, pluck="body"
		)
		self.assertTrue(any("Needs the Woreda expert." in body for body in bodies), bodies)

	def test_skipping_a_level_is_refused(self):
		doc = self.case(self.category, self.dept, self.user[L1])
		res = self.escalate_as(self.user[L1], doc.name, reason="Skip.", target_officer=self.user[L3])
		self.assertEqual(res["code"], "VALIDATION_ERROR", res)
		self.assertEqual(frappe.db.get_value("Grievance", doc.name, "assigned_to"), self.user[L1])

	def test_an_officer_who_cannot_act_on_the_case_cannot_escalate_it(self):
		doc = self.case(self.category, self.dept, self.user[L2])
		res = self.escalate_as(self.user[L1], doc.name, reason="Not mine.")
		self.assertIn(res["code"], ("PERMISSION_DENIED", "NOT_FOUND"), res)

	def test_the_top_of_the_chain_has_nowhere_to_go(self):
		doc = self.case(self.category, self.dept, self.user[L4])
		res = self.escalate_as(self.user[L4], doc.name, reason="Up.")
		self.assertEqual(res["code"], "VALIDATION_ERROR", res)


class TestEscalatedStatus(FrappeTestCase):
	"""Item 4: Escalated is shown while an escalated case waits on an officer."""

	def test_open_officer_states_show_escalated(self):
		for state in (C.STATE_ASSIGNED, C.STATE_REOPENED, C.STATE_IN_PROGRESS):
			self.assertEqual(public_status(state, escalated=True), STATUS_ESCALATED, state)

	def test_other_states_keep_their_stage(self):
		self.assertEqual(public_status(C.STATE_MORE_INFO_NEEDED, escalated=True), C.STATE_MORE_INFO_NEEDED)
		self.assertEqual(public_status(C.STATE_RESOLVED, escalated=True), "Resolved")
		self.assertEqual(public_status(C.STATE_REOPENED), "Reopened")


class TestStaffProfile(_Base):
	"""Item 9: the Reviewer gets a profile, and a co-handling officer keeps their own department."""

	def test_a_reviewer_gets_a_profile(self):
		reviewer = a_user_with_role(C.ROLE_REVIEW_OFFICER, "reviewer")
		_namespace, profile = resolve_user_profile_hook(frappe.get_doc("User", reviewer))
		self.assertEqual(profile["type"], C.ROLE_REVIEW_OFFICER)

		dept, category = a_unique_department("Rev"), a_category()
		_desk(dept, category, [(self.officer("l1"), L1, None), (reviewer, "review_officer", None)])
		_namespace, profile = resolve_user_profile_hook(frappe.get_doc("User", reviewer))
		self.assertEqual(profile["department"], dept)
		self.assertEqual(profile["departments"], [dept])

	def test_a_co_handling_officer_belongs_to_the_desk_they_specialise_on(self):
		crop_dept, coop_dept = a_unique_department("Crop"), a_unique_department("Coop")
		coop_officer = self.officer("coop")
		# The co-handled desk is made first, so it has the lower name: the old rule picked it.
		_desk(
			crop_dept,
			a_category(),
			[(self.officer("da"), L1, None), (self.officer("cropsms"), L2, None), (coop_officer, L2, None)],
		)
		_desk(coop_dept, a_category(), [(self.officer("kao"), L1, None), (coop_officer, L2, None)])

		_namespace, profile = resolve_user_profile_hook(frappe.get_doc("User", coop_officer))
		self.assertEqual(profile["department"], coop_dept)
		self.assertEqual(sorted(profile["departments"]), sorted([crop_dept, coop_dept]))
		self.assertEqual(home_seat(permissions.active_scopes(coop_officer)).department_scope, coop_dept)

		from oan_grievance_service.services import officer as officer_service

		self.assertEqual(officer_service.record(coop_officer)["department"], coop_dept)
