# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

from contextlib import contextmanager

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.api.v1.category_assignment import (
	create_assignment,
	deactivate_assignment,
	get_assignment,
	list_assignments,
	update_assignment,
)


class TestGrievanceCategoryAssignment(FrappeTestCase):
	def setUp(self):
		self.l1 = _user("stg404-l1@example.com", "Tigist Alemu")
		self.l2 = _user("stg404-l2@example.com", "Yonas Mekonnen")
		self.category = _category("STG404 Inputs", "Z94")
		_role_level("nodal_officer", 10)
		_role_level("senior_nodal_officer", 20)
		self.department = _department("STG404 Inputs Agency", "S404")
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"Grievance RBAC Assignment", filters={"category_scope": self.category}, pluck="name"
		):
			frappe.delete_doc("Grievance RBAC Assignment", name, force=1, ignore_permissions=True)
		frappe.clear_messages()

	def test_crud_projects_desk_and_sla(self):
		created = create_assignment(
			service_category=self.category,
			department="S404",
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=14,
			auto_escalate=True,
		)
		self.assertEqual(created["status"], "success")
		assignment = created["data"]["assignment"]
		self.assertEqual(assignment["service_category"], self.category)
		self.assertEqual(assignment["department"], self.department)
		self.assertEqual(assignment["sla_days"], 14)
		self.assertTrue(assignment["auto_escalate"])
		self.assertEqual(assignment["l1_officer_name"], "Tigist Alemu")
		self.assertNotIn("rbac_assignment", assignment)
		self.assertNotIn("priority", assignment)
		self.assertNotIn("notify_on_submit", assignment)

		desk = frappe.get_doc("Grievance RBAC Assignment", assignment["name"])
		self.assertEqual(desk.category_scope, self.category)
		self.assertEqual(desk.department_scope, self.department)
		self.assertEqual(desk.active, 1)
		self.assertEqual(desk.routing_strategy, "Primary First")
		self.assertEqual(assignment["l1_role_level"], "nodal_officer")
		self.assertEqual(assignment["routing_strategy"], "Primary First")
		officers = {row.role_level: row for row in desk.officers}
		self.assertEqual(officers["nodal_officer"].user, self.l1)
		self.assertEqual(officers["nodal_officer"].is_primary, 1)
		self.assertEqual(officers["senior_nodal_officer"].user, self.l2)

		auto_escalate = frappe.db.get_value("Grievance RBAC Assignment", assignment["name"], "auto_escalate")
		# The days are the category's base SLA rule's.
		sla_days = frappe.db.get_value(
			"Grievance SLA Rule", {"service_category": self.category, "is_base": 1}, "sla_days"
		)
		self.assertEqual(sla_days, 14)
		self.assertEqual(auto_escalate, 1)

		updated = update_assignment(
			assignment["name"],
			sla_days=10,
			l2_officer=None,
		)
		self.assertEqual(updated["status"], "success")
		self.assertEqual(updated["data"]["assignment"]["sla_days"], 10)
		self.assertIsNone(updated["data"]["assignment"]["l2_officer"])
		desk.reload()
		self.assertEqual([row.user for row in desk.officers], [self.l1])

		listed = list_assignments(service_category=self.category)
		self.assertEqual(listed["data"]["pagination"]["total_count"], 1)
		self.assertEqual(listed["data"]["assignments"][0]["name"], assignment["name"])

		fetched = get_assignment(assignment["name"])
		self.assertEqual(fetched["data"]["assignment"]["sla_days"], 10)

		deactivated = deactivate_assignment(assignment["name"])
		self.assertEqual(deactivated["status"], "success")
		self.assertFalse(deactivated["data"]["assignment"]["active"])
		self.assertEqual(frappe.db.get_value("Grievance RBAC Assignment", assignment["name"], "active"), 0)
		self.assertTrue(
			frappe.db.exists("Grievance SLA Rule", {"service_category": self.category, "is_base": 1})
		)

		again = deactivate_assignment(assignment["name"])
		self.assertFalse(again["data"]["assignment"]["active"])

	def test_desk_assigns_role_levels_and_default_routing_strategy(self):
		category = _category("STG404 Routing", "Z9R")
		created = create_assignment(
			service_category=category,
			department=self.department,
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=6,
		)
		self.assertEqual(created["status"], "success", msg=created)
		assignment = created["data"]["assignment"]
		self.assertEqual(assignment["l1_role_level"], "nodal_officer")
		self.assertEqual(assignment["l2_role_level"], "senior_nodal_officer")
		self.assertEqual(assignment["routing_strategy"], "Primary First")
		desk = frappe.get_doc("Grievance RBAC Assignment", assignment["name"])
		self.assertEqual(desk.routing_strategy, "Primary First")
		officers = {row.role_level: row.user for row in desk.officers}
		self.assertEqual(officers["nodal_officer"], self.l1)
		self.assertEqual(officers["senior_nodal_officer"], self.l2)

	def test_unknown_department_is_rejected(self):
		result = create_assignment(
			service_category=self.category,
			department="Non Existent Dept",
			l1_officer=self.l1,
			sla_days=5,
		)
		self.assertEqual(result["code"], "VALIDATION_ERROR")
		self.assertIn("does not exist", result["message"])

	def test_duplicate_category_is_rejected(self):
		create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			sla_days=7,
		)
		duplicate = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			sla_days=8,
		)
		self.assertEqual(duplicate["code"], "DUPLICATE_ENTRY")

	def test_category_can_have_one_assignment_per_department(self):
		other = _department("STG404 Second Agency", "S404B")
		first = self._assignment()
		second = self._assignment(department=other)
		self.assertNotEqual(first, second)
		listed = list_assignments(service_category=self.category)
		self.assertEqual(listed["data"]["pagination"]["total_count"], 2)
		self.assertEqual(
			sorted(row["department"] for row in listed["data"]["assignments"]),
			sorted([self.department, other]),
		)
		with _keep_transaction():
			moved = update_assignment(second, department=self.department)
		self.assertEqual(moved["code"], "DUPLICATE_ENTRY")

	def test_deactivate_leaves_the_shared_sla_row_alone(self):
		other = _department("STG404 Shared SLA Agency", "S404C")
		first = self._assignment()
		second = self._assignment(department=other)
		deactivate_assignment(first)
		self.assertTrue(frappe.db.get_value("Grievance RBAC Assignment", second, "active"))
		self.assertTrue(
			frappe.db.exists("Grievance SLA Rule", {"service_category": self.category, "is_base": 1})
		)

	def test_patch_active_false_matches_delete(self):
		name = self._assignment()
		patched = update_assignment(name, active=False)
		self.assertFalse(patched["data"]["assignment"]["active"])
		self.assertEqual(deactivate_assignment(name)["data"]["assignment"], patched["data"]["assignment"])
		revived = update_assignment(name, active=True)
		self.assertTrue(revived["data"]["assignment"]["active"])

	def test_update_keeps_officer_rows_that_do_not_change(self):
		created = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=7,
		)
		name = created["data"]["assignment"]["name"]
		before = {row.user: row.name for row in frappe.get_doc("Grievance RBAC Assignment", name).officers}
		update_assignment(name, sla_days=9)
		after = {row.user: row.name for row in frappe.get_doc("Grievance RBAC Assignment", name).officers}
		self.assertEqual(before, after)

	def test_update_keeps_officers_outside_the_l1_and_l2_seats(self):
		extra = _user("stg404-l1b@example.com", "Hana Girma")
		replacement = _user("stg404-l1c@example.com", "Dawit Haile")
		name = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=7,
		)["data"]["assignment"]["name"]
		desk = frappe.get_doc("Grievance RBAC Assignment", name)
		desk.append(
			"officers",
			{
				"user": extra,
				"role_level": "nodal_officer",
				"is_primary": 1,
				"active": 1,
				"reports_to": self.l2,
			},
		)
		desk.save()

		update_assignment(name, sla_days=9)
		self.assertEqual(_desk_users(name), {self.l1, self.l2, extra})

		update_assignment(name, l1_officer=replacement)
		self.assertEqual(_desk_users(name), {replacement, self.l2, extra})
		reports_to = {
			row.user: row.reports_to for row in frappe.get_doc("Grievance RBAC Assignment", name).officers
		}
		self.assertEqual(reports_to[extra], self.l2)
		self.assertEqual(reports_to[replacement], self.l2)

	def test_l2_cannot_be_replaced_while_others_report_to_them(self):
		extra = _user("stg404-l1b@example.com", "Hana Girma")
		other_l2 = _user("stg404-l2b@example.com", "Almaz Worku")
		name = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=7,
		)["data"]["assignment"]["name"]
		desk = frappe.get_doc("Grievance RBAC Assignment", name)
		desk.append(
			"officers",
			{
				"user": extra,
				"role_level": "nodal_officer",
				"is_primary": 1,
				"active": 1,
				"reports_to": self.l2,
			},
		)
		desk.save()

		with _keep_transaction():
			result = update_assignment(name, l2_officer=other_l2)
		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
		self.assertEqual(_desk_users(name), {self.l1, self.l2, extra})

	def test_l2_is_found_by_role_level_when_several_l1_officers_come_first(self):
		first = _user("stg404-l1b@example.com", "Hana Girma")
		second = _user("stg404-l1c@example.com", "Dawit Haile")
		new_l2 = _user("stg404-l2b@example.com", "Almaz Worku")
		name = self._desk_with_officers(
			[
				(self.l1, "nodal_officer", 1),
				# Not primary and listed before the L2: a positional read takes the first for L2.
				(first, "nodal_officer", 0),
				(second, "nodal_officer", 0),
				(self.l2, "senior_nodal_officer", 0),
			]
		)

		shown = get_assignment(name)["data"]["assignment"]
		self.assertEqual((shown["l1_officer"], shown["l2_officer"]), (self.l1, self.l2))

		update_assignment(name, l2_officer=new_l2)
		self.assertEqual(_desk_users(name), {self.l1, first, second, new_l2})

		update_assignment(name, l2_officer=None)
		self.assertEqual(_desk_users(name), {self.l1, first, second})

	def test_moving_the_l1_seat_to_an_officer_on_the_desk_points_them_at_the_l2(self):
		stale = _user("stg404-l2b@example.com", "Almaz Worku")
		extra = _user("stg404-l1b@example.com", "Hana Girma")
		name = self._desk_with_officers(
			[
				(self.l1, "nodal_officer", 1),
				(extra, "nodal_officer", 1),
				(self.l2, "senior_nodal_officer", 0),
			]
		)
		frappe.db.set_value(
			"Grievance RBAC Assignment Officer", {"parent": name, "user": extra}, "reports_to", stale
		)

		update_assignment(name, l1_officer=extra)

		reports_to = {
			row.user: row.reports_to for row in frappe.get_doc("Grievance RBAC Assignment", name).officers
		}
		self.assertEqual(reports_to, {extra: self.l2, self.l2: None})

	def test_an_inactive_officer_does_not_block_replacing_the_l2(self):
		gone = _user("stg404-l1b@example.com", "Hana Girma")
		new_l2 = _user("stg404-l2b@example.com", "Almaz Worku")
		name = self._desk_with_officers(
			[
				(self.l1, "nodal_officer", 1),
				(gone, "nodal_officer", 1),
				(self.l2, "senior_nodal_officer", 0),
			]
		)
		frappe.db.set_value(
			"Grievance RBAC Assignment Officer",
			{"parent": name, "user": gone},
			{"reports_to": self.l2, "active": 0},
		)

		update_assignment(name, l2_officer=new_l2)

		self.assertEqual(_desk_users(name), {self.l1, gone, new_l2})

	def test_the_l2_seat_follows_the_reporting_line_and_is_not_primary(self):
		other = _user("stg404-l2b@example.com", "Almaz Worku")
		name = self._desk_with_officers(
			[
				(self.l1, "nodal_officer", 1),
				(other, "senior_nodal_officer", 1),
				(self.l2, "senior_nodal_officer", 0),
			]
		)

		def l2_seat():
			return get_assignment(name)["data"]["assignment"]["l2_officer"]

		# No reporting line: the one that is not primary wins over list order.
		self.assertEqual(l2_seat(), self.l2)
		# A reporting line wins over everything else.
		frappe.db.set_value(
			"Grievance RBAC Assignment Officer", {"parent": name, "user": self.l1}, "reports_to", other
		)
		self.assertEqual(l2_seat(), other)

	def test_update_without_an_l1_officer_on_the_desk_is_accepted(self):
		"""Officers are optional on a desk, so a desk whose L1 has gone can still be edited."""
		name = self._assignment()
		frappe.db.set_value("Grievance RBAC Assignment Officer", {"parent": name}, "active", 0)
		result = update_assignment(name, department=self.department)
		self.assertEqual(result["status"], "success", msg=result)
		self.assertIsNone(result["data"]["assignment"]["l1_officer"])

	def test_options_categories_follow_the_selected_department(self):
		from oan_grievance_service.api.v1.grievance import options

		other_category = _category("STG404 Markets", "Z95")
		other_department = _department("STG404 Markets Agency", "S405")
		name = self._assignment()
		self._assignment(category=other_category, department=other_department)

		def categories(**params):
			result = options(**params)
			self.assertEqual(result["status"], "success", msg=result)
			return {row["category_name"] for row in result["data"]["service_categories"]}

		self.assertLessEqual({self.category, other_category}, categories())
		self.assertEqual(categories(department=self.department), {self.category})
		self.assertEqual(categories(department=other_department), {other_category})
		self.assertEqual(categories(department="S404"), {self.category})
		self.assertEqual(categories(department="S405"), {other_category})
		self.assertEqual(categories(department="No Such Department"), set())

		# The officers cascade resolves the same way, and an unknown department has none.
		officers = options(department="S404")["data"]["officers"]
		self.assertIn(self.l1, {officer["user_id"] for officer in officers})
		self.assertEqual(options(department="No Such Department")["data"]["officers"], [])

		update_assignment(name, active=False)
		self.assertEqual(categories(department=self.department), set())

	def test_unknown_and_immutable_fields_are_rejected(self):
		name = self._assignment()
		with _keep_transaction():
			for field in ("priority", "notify_on_submit", "service_category"):
				result = update_assignment(name, **{field: "Low"})
				self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
				self.assertIn(field, result["details"])
			created = create_assignment(
				service_category=self.category,
				department=self.department,
				l1_officer=self.l1,
				sla_days=5,
				priority="Low",
			)
			self.assertEqual(created["code"], "VALIDATION_ERROR")
			self.assertIn("priority", created["details"])

	def test_category_assignment_is_the_rbac_desk(self):
		created = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			sla_days=5,
		)
		self.assertEqual(created["status"], "success", msg=created)
		assignment = created["data"]["assignment"]
		self.assertTrue(frappe.db.exists("Grievance RBAC Assignment", assignment["name"]))
		self.assertFalse(assignment["name"].startswith("GR-CAT"))

	def test_missing_assignment_is_not_found(self):
		for result in (
			get_assignment("GR-CAT-DOES-NOT-EXIST"),
			update_assignment("GR-CAT-DOES-NOT-EXIST", sla_days=3),
			deactivate_assignment("GR-CAT-DOES-NOT-EXIST"),
		):
			self.assertEqual(result["code"], "NOT_FOUND")

	def test_null_department_makes_the_desk_serve_every_department(self):
		name = self._assignment()
		for blank in (None, "", " "):
			with self.subTest(blank=blank):
				frappe.db.set_value("Grievance RBAC Assignment", name, "department_scope", self.department)
				result = update_assignment(name, department=blank)
				self.assertEqual(result["status"], "success", msg=result)
				self.assertIsNone(result["data"]["assignment"]["department"])
				stored = frappe.db.get_value(
					"Grievance RBAC Assignment", name, ["department_scope", "category_scope"], as_dict=True
				)
				self.assertFalse(stored.department_scope)
				self.assertEqual(stored.category_scope, self.category)

	def test_null_l1_officer_leaves_the_desk_without_one(self):
		name = self._assignment()
		for blank in (None, ""):
			with self.subTest(blank=blank):
				result = update_assignment(name, l1_officer=blank)
				self.assertEqual(result["status"], "success", msg=result)
				self.assertIsNone(result["data"]["assignment"]["l1_officer"])
				self.assertEqual(_desk_users(name), set())
				# Seat the L1 again for the next blank.
				self.assertEqual(update_assignment(name, l1_officer=self.l1)["status"], "success")

	def test_a_desk_can_be_created_without_department_or_officers(self):
		created = create_assignment(service_category=self.category)
		self.assertEqual(created["status"], "success", msg=created)
		assignment = created["data"]["assignment"]
		self.assertIsNone(assignment["department"])
		self.assertIsNone(assignment["l1_officer"])
		self.assertEqual(_desk_users(assignment["name"]), set())
		# One such desk per category.
		with _keep_transaction():
			again = create_assignment(service_category=self.category)
		self.assertNotEqual(again["status"], "success", msg=again)

	def test_sla_days_must_be_at_least_one(self):
		with _keep_transaction():
			for days in (0, -1):
				result = create_assignment(
					service_category=self.category,
					department=self.department,
					l1_officer=self.l1,
					sla_days=days,
				)
				self.assertEqual(result["code"], "VALIDATION_ERROR")
				self.assertIn("sla_days", result["details"])
			name = self._assignment()
			updated = update_assignment(name, sla_days=0)
			self.assertEqual(updated["code"], "VALIDATION_ERROR")
			self.assertIn("sla_days", updated["details"])

	def test_page_and_filter_bounds(self):
		with _keep_transaction():
			self.assertEqual(list_assignments(page=0)["code"], "VALIDATION_ERROR")
			self.assertEqual(list_assignments(page_size=0)["code"], "VALIDATION_ERROR")
			self.assertEqual(list_assignments(page_size=101)["code"], "VALIDATION_ERROR")
			self.assertEqual(list_assignments(page="nope")["code"], "VALIDATION_ERROR")
			self.assertEqual(list_assignments(active="maybe")["code"], "VALIDATION_ERROR")

	def test_unknown_links_and_same_officers_are_rejected(self):
		with _keep_transaction():
			missing_category = create_assignment(
				service_category="Missing Category",
				department=self.department,
				l1_officer=self.l1,
				sla_days=5,
			)
			self.assertEqual(missing_category["code"], "VALIDATION_ERROR")
			missing_department = create_assignment(
				service_category=self.category,
				department="Missing Department",
				l1_officer=self.l1,
				sla_days=5,
			)
			self.assertEqual(missing_department["code"], "VALIDATION_ERROR")
			missing_officer = create_assignment(
				service_category=self.category,
				department=self.department,
				l1_officer="nobody@example.com",
				sla_days=5,
			)
			self.assertEqual(missing_officer["code"], "VALIDATION_ERROR")
			same_officer = create_assignment(
				service_category=self.category,
				department=self.department,
				l1_officer=self.l1,
				l2_officer=self.l1,
				sla_days=5,
			)
			self.assertEqual(same_officer["code"], "VALIDATION_ERROR")
			self.assertIn("different", same_officer["message"])

	def test_unknown_department_and_disabled_officer_are_rejected(self):
		disabled = _user("stg404-disabled@example.com", "Disabled Officer", enabled=0)
		with _keep_transaction():
			unknown_result = create_assignment(
				service_category=self.category,
				department="Non Existent Dept",
				l1_officer=self.l1,
				sla_days=5,
			)
			self.assertEqual(unknown_result["code"], "VALIDATION_ERROR")
			self.assertIn("does not exist", unknown_result["message"])
			disabled_result = create_assignment(
				service_category=self.category,
				department=self.department,
				l1_officer=disabled,
				sla_days=5,
			)
			self.assertEqual(disabled_result["code"], "VALIDATION_ERROR")
			self.assertIn("disabled", disabled_result["message"])

	def test_empty_update_is_rejected(self):
		name = self._assignment()
		with _keep_transaction():
			empty = update_assignment(name)
		self.assertEqual(empty["code"], "VALIDATION_ERROR")
		self.assertIn("No fields", empty["message"])

	def test_list_order_is_stable_when_modified_ties(self):
		department = _department("STG404 Order Agency", "S404O")
		first_category = _category("STG404 Order A", "Z9A")
		second_category = _category("STG404 Order B", "Z9B")
		first = self._assignment(category=first_category, department=department)
		second = self._assignment(category=second_category, department=department)
		stamp = "2026-01-01 00:00:00"
		for name in (first, second):
			frappe.db.set_value(
				"Grievance RBAC Assignment",
				name,
				"modified",
				stamp,
				update_modified=False,
			)
		listed = list_assignments(department=department, page_size=10)
		names = [row["name"] for row in listed["data"]["assignments"]]
		self.assertEqual(names, sorted([first, second], reverse=True))
		page_one = list_assignments(department=department, page=1, page_size=1)
		page_two = list_assignments(department=department, page=2, page_size=1)
		self.assertEqual(page_one["data"]["assignments"][0]["name"], names[0])
		self.assertEqual(page_two["data"]["assignments"][0]["name"], names[1])
		self.assertNotEqual(names[0], names[1])

	def test_officer_cannot_manage_assignments(self):
		name = self._assignment()
		frappe.set_user(self.l1)
		with _keep_transaction():
			for result in (
				list_assignments(),
				get_assignment(name),
				create_assignment(
					service_category=self.category,
					department=self.department,
					l1_officer=self.l1,
					sla_days=6,
				),
				update_assignment(name, sla_days=9),
				deactivate_assignment(name),
			):
				self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)

	def test_grievance_admin_can_list(self):
		self._assignment()
		admin = _user("stg404-admin@example.com", "Selam Admin", role="Grievance Admin")
		frappe.set_user(admin)
		result = list_assignments(service_category=self.category)
		self.assertEqual(result["status"], "success")
		self.assertEqual(result["data"]["pagination"]["total_count"], 1)

	def test_grievance_admin_can_write(self):
		admin = _user("stg433-admin@example.com", "Selam Writer", role="Grievance Admin")
		frappe.set_user(admin)
		created = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			sla_days=6,
		)
		self.assertEqual(created["status"], "success", msg=created)
		name = created["data"]["assignment"]["name"]
		self.assertEqual(update_assignment(name, sla_days=4)["status"], "success")
		self.assertEqual(deactivate_assignment(name)["status"], "success")

	def test_guest_cannot_manage_assignments(self):
		name = self._assignment()
		frappe.set_user("Guest")
		with _keep_transaction():
			for result in (
				list_assignments(),
				get_assignment(name),
				create_assignment(
					service_category=self.category,
					department=self.department,
					l1_officer=self.l1,
					sla_days=6,
				),
				update_assignment(name, sla_days=9),
				deactivate_assignment(name),
			):
				self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)

	def test_non_admin_with_a_bad_body_gets_403_not_400(self):
		name = self._assignment()
		frappe.set_user(self.l1)
		with _keep_transaction():
			for result in (
				create_assignment(service_category=self.category, priority="Low"),
				update_assignment(name, priority="Low"),
				get_assignment(" "),
				deactivate_assignment(" "),
			):
				self.assertEqual(result["code"], "PERMISSION_DENIED", msg=result)

	def test_blank_assignment_id_is_a_validation_error(self):
		with _keep_transaction():
			for result in (get_assignment(" "), deactivate_assignment(" ")):
				self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
				self.assertIn("assignment", result["details"])

	def test_desk_validation_applies_outside_the_api(self):
		def desk(category=None, officer=None, department=None):
			doc = frappe.new_doc("Grievance RBAC Assignment")
			doc.department_scope = department or self.department
			doc.category_scope = category or self.category
			doc.effective_from = frappe.utils.today()
			doc.append(
				"officers",
				{"user": officer or self.l1, "role_level": "nodal_officer", "is_primary": 1, "active": 1},
			)
			return doc

		disabled = _user("stg404-direct-disabled@example.com", "Direct Disabled", enabled=0)
		with self.assertRaisesRegex(frappe.ValidationError, "disabled"):
			desk(officer=disabled).insert()
		plain = _user("stg404-direct-plain@example.com", "Direct Plain", role="Guest")
		with self.assertRaisesRegex(frappe.ValidationError, "Grievance Officer"):
			desk(officer=plain).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "Could not find Department Scope|does not exist"):
			desk(department="Non Existent Dept").insert()

		desk().insert()
		with self.assertRaises(frappe.DuplicateEntryError):
			desk().insert()

		repeated = desk(category=_category("STG404 Repeat", "Z9X"))
		repeated.append(
			"officers",
			{"user": self.l1, "role_level": "senior_nodal_officer", "is_primary": 0, "active": 1},
		)
		with self.assertRaisesRegex(frappe.ValidationError, "more than once"):
			repeated.insert()

	def test_area_aware_desks_skip_category_only_checks(self):
		area_desk = frappe.new_doc("Grievance RBAC Assignment")
		area_desk.department_scope = self.department
		area_desk.category_scope = self.category
		area_desk.effective_from = frappe.utils.today()
		area_desk.service_provider_scope = "Provider X"
		area_desk.append("officers", {"user": self.l1, "role_level": "nodal_officer", "is_primary": 1})
		self.assertFalse(area_desk.is_category_only())
		area_desk.insert()
		self._assignment()

	def test_l1_reports_to_l2_and_l2_reports_to_nobody(self):
		created = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=7,
		)
		name = created["data"]["assignment"]["name"]
		reports_to = {
			row.user: row.reports_to for row in frappe.get_doc("Grievance RBAC Assignment", name).officers
		}
		self.assertEqual(reports_to, {self.l1: self.l2, self.l2: None})
		from oan_grievance_service.permissions import desk_teams
		from oan_grievance_service.services import sla

		self.assertEqual(sla.get_officer_supervisor(self.l1, department=self.department), self.l2)
		self.assertIsNone(sla.get_officer_supervisor(self.l2, department=self.department))
		self.assertIn(self.l1, {user for _seat, team in desk_teams(self.l2) for user in team})
		self.assertEqual(desk_teams(self.l1), [])

		update_assignment(name, l2_officer=None)
		rows = frappe.get_doc("Grievance RBAC Assignment", name).officers
		self.assertEqual([(row.user, row.reports_to) for row in rows], [(self.l1, None)])

	def test_first_line_routing_skips_the_l2_officer(self):
		from oan_grievance_service.services import routing

		name = create_assignment(
			service_category=self.category,
			department=self.department,
			l1_officer=self.l1,
			l2_officer=self.l2,
			sla_days=7,
		)["data"]["assignment"]["name"]
		desk = frappe.get_doc("Grievance RBAC Assignment", name)
		desk.routing_strategy = "Round Robin"
		desk.save()
		picked = {routing.pick_officer_by_strategy(desk) for _ in range(4)}
		self.assertEqual(picked, {self.l1})

	def test_tied_category_desks_route_to_the_oldest(self):
		from oan_grievance_service.services import routing

		other = _department("STG404 Tie Agency", "S404T")
		first = self._assignment()
		second = self._assignment(department=other)
		frappe.db.set_value("Grievance RBAC Assignment", first, "creation", "2026-01-02 00:00:00")
		frappe.db.set_value("Grievance RBAC Assignment", second, "creation", "2026-01-01 00:00:00")
		matched = routing.find_matching_assignment({"service_category": self.category})
		self.assertEqual(matched.name, second)
		frappe.db.set_value("Grievance RBAC Assignment", first, "creation", "2026-01-01 00:00:00")
		matched = routing.find_matching_assignment({"service_category": self.category})
		self.assertEqual(matched.name, min(first, second))

	def test_changes_are_tracked_and_attributed(self):
		# Frappe skips Version rows while tests run, so check what produces them in production.
		for doctype in ("Grievance RBAC Assignment", "Grievance SLA Rule"):
			self.assertTrue(frappe.get_meta(doctype).track_changes, msg=doctype)
		name = self._assignment()
		frappe.db.set_value("Grievance RBAC Assignment", name, "assigned_by", None)
		frappe.set_user("Administrator")
		update_assignment(name, sla_days=11)
		self.assertEqual(
			frappe.db.get_value("Grievance RBAC Assignment", name, "assigned_by"), "Administrator"
		)

	def test_unknown_list_filters_are_rejected(self):
		self._assignment()
		with _keep_transaction():
			result = list_assignments(departmnt=self.department)
		self.assertEqual(result["code"], "VALIDATION_ERROR", msg=result)
		self.assertIn("departmnt", result["details"])

	def _desk_with_officers(self, officers: list[tuple[str, str, int]]) -> str:
		"""A category desk holding exactly these (user, role level, is_primary) rows, in this order."""
		name = self._assignment()
		desk = frappe.get_doc("Grievance RBAC Assignment", name)
		desk.set("officers", [])
		for user, role_level, is_primary in officers:
			desk.append(
				"officers", {"user": user, "role_level": role_level, "is_primary": is_primary, "active": 1}
			)
		desk.save()
		return name

	def _assignment(self, category=None, department=None):
		created = create_assignment(
			service_category=category or self.category,
			department=department or self.department,
			l1_officer=self.l1,
			sla_days=7,
		)
		self.assertEqual(created["status"], "success", msg=created)
		return created["data"]["assignment"]["name"]


def _desk_users(name) -> set[str]:
	return {row.user for row in frappe.get_doc("Grievance RBAC Assignment", name).officers}


@contextmanager
def _keep_transaction():
	"""API errors call frappe.db.rollback(), which would erase this test's fixtures.

	A rollback to a savepoint still runs: it undoes only the failed statement, and on PostgreSQL
	it is what keeps the transaction usable after one.
	"""
	original = frappe.db.rollback

	def rollback(*args, save_point=None, **kwargs):
		if save_point:
			original(save_point=save_point, **kwargs)

	frappe.db.rollback = rollback
	try:
		yield
	finally:
		frappe.db.rollback = original


def _user(email, full_name, role="Grievance Officer", enabled=1):
	if frappe.db.exists("User", email):
		frappe.db.set_value("User", email, "enabled", enabled, update_modified=False)
		return email
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": full_name,
			"enabled": enabled,
			"send_welcome_email": 0,
			"roles": [{"role": role}],
		}
	).insert(ignore_permissions=True)
	return email


def _category(name, code):
	if frappe.db.exists("Grievance Service Category", name):
		return name
	frappe.get_doc(
		{
			"doctype": "Grievance Service Category",
			"category_name": name,
			"code": code,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)
	return name


def _role_level(level, order):
	if frappe.db.exists("Grievance Role Level", level):
		return level
	frappe.get_doc(
		{
			"doctype": "Grievance Role Level",
			"level_code": level,
			"level_name": level,
			"level_order": order,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)
	return level


def _department(name, short_name="S404", **kwargs):
	if frappe.db.exists("Grievance Department", name):
		return name
	frappe.get_doc(
		{
			"doctype": "Grievance Department",
			"dept_name": name,
			"short_name": short_name,
		}
	).insert(ignore_permissions=True)
	return name
