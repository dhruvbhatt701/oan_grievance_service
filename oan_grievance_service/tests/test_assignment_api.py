# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""STG-337: assignment follows the Category+Department routing rule, and
reassignment uses that same rule with no approval step and no SLA reset.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service.api.v1.grievance import assign, reassign
from oan_grievance_service.services import assignment, lifecycle
from oan_grievance_service.services import constants as C
from oan_grievance_service.tests.fixtures import a_grievance, a_leaf_area, a_submitter_type


class TestAssignmentApi(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.category = self._category("STG337 Routing", "337")
		self.other_category = self._category("STG337 Other", "338")
		self.gtype = self._type(self.category)
		self.dept_a = self._department("STG337 Agriculture", "stg337.agriculture@example.com")
		self.dept_b = self._department("STG337 Livestock", "stg337.livestock@example.com")
		self.dept_c = self._department("STG337 Unrouted", "stg337.unrouted@example.com")
		self.holder = self._user("stg337.holder@example.com", ["Grievance Officer"])
		self.target = self._user("stg337.target@example.com", ["Grievance Officer"])
		self.stranger = self._user("stg337.stranger@example.com", ["Grievance Officer"])
		self.admin = self._user("stg337.admin@example.com", ["Grievance Admin"])
		self.submitter = self._user("stg337.submitter@example.com", ["Grievance Submitter"])
		self._role_level()
		# A site rule with an empty category matches every case and would steal
		# the assertion. Deactivate them for this transaction; rollback restores them.
		for name in frappe.get_all("Grievance Routing Rule", pluck="name"):
			frappe.db.set_value("Grievance Routing Rule", name, "active", 0, update_modified=False)
		self.rule_a = self._rule(self.category, self.dept_a, 1)
		self.rule_b = self._rule(self.category, self.dept_b, 2)
		self._desk(self.dept_a, self.category, self.holder)
		self._desk(self.dept_b, self.category, self.target)
		self._sla(self.category)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_initial_assignment_follows_the_category_department_rule(self):
		doc = self._case()
		res = assign(doc.ticket_number)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["data"]["assignment"]["department"], self.dept_a)
		self.assertEqual(res["data"]["assignment"]["assigned_to"], self.holder)
		self.assertEqual(res["data"]["assignment"]["routing_rule"], self.rule_a)
		self.assertEqual(res["data"]["status"], C.ASSIGNED)
		self.assertTrue(res["data"]["sla"]["sla_due_date"])

		saved = frappe.get_doc("Grievance", doc.name)
		self.assertEqual(saved.assigned_dept, self.dept_a)
		self.assertEqual(saved.assigned_to, self.holder)

	def test_reassignment_uses_the_same_rule_without_approval(self):
		doc = self._assigned()
		frappe.set_user(self.holder)
		res = reassign(
			doc.ticket_number,
			department=self.dept_b,
			reason="The case belongs with the livestock desk.",
		)
		self.assertEqual(res["status"], "success")
		data = res["data"]
		self.assertEqual(data["assignment"]["department"], self.dept_b)
		self.assertEqual(data["assignment"]["assigned_to"], self.target)
		self.assertEqual(data["assignment"]["routing_rule"], self.rule_b)
		self.assertEqual(data["audit"]["auto_routed"], 1)
		self.assertEqual(data["audit"]["sla_treatment"], "Continue")
		self.assertEqual(data["status"], C.ASSIGNED)

		request = frappe.get_doc("Grievance Reassignment Request", data["audit"]["name"])
		self.assertEqual(request.decision, "Approved")
		self.assertEqual(request.auto_routed, 1)
		self.assertEqual(request.sla_treatment, "Continue")
		self.assertEqual(request.prior_department, self.dept_a)
		self.assertEqual(request.target_department, self.dept_b)
		self.assertFalse(request.approver)

	def test_sla_clock_continues_across_reassignment(self):
		doc = self._assigned()
		lifecycle.accept(doc)
		due = frappe.db.get_value("Grievance", doc.name, "sla_due_date")
		started = frappe.db.get_value("Grievance", doc.name, "sla_start_at")
		doc.db_set("reminder_50_sent", 1, update_modified=False)
		doc.db_set("reminder_80_sent", 1, update_modified=False)

		frappe.set_user(self.holder)
		res = reassign(
			doc.ticket_number,
			department=self.dept_b,
			reason="Handing the case to livestock without restarting the clock.",
		)
		self.assertEqual(res.get("status"), "success", res)

		saved = frappe.get_doc("Grievance", doc.name)
		self.assertEqual(saved.status, C.ASSIGNED)
		self.assertEqual(str(saved.sla_due_date), str(due))
		self.assertEqual(str(saved.sla_start_at), str(started))
		self.assertEqual(saved.reminder_50_sent, 1)
		self.assertEqual(saved.reminder_80_sent, 1)

	def test_reassignment_audit_entry_is_immutable(self):
		doc = self._assigned()
		frappe.set_user(self.admin)
		res = reassign(
			doc.ticket_number,
			department=self.dept_b,
			reason="Administrator moves the case to the livestock desk.",
		)
		request = frappe.get_doc("Grievance Reassignment Request", res["data"]["audit"]["name"])
		request.reason = "A rewritten reason that must not stick."
		with self.assertRaises(frappe.ValidationError):
			request.save(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc(
				"Grievance Reassignment Request", request.name, force=True, ignore_permissions=True
			)

		entry = frappe.get_doc(
			"Grievance Timeline",
			{"grievance": doc.name, "entry_type": "assignment", "ref_docname": request.name},
		)
		entry.body = "Edited after the fact."
		with self.assertRaises(frappe.ValidationError):
			entry.save(ignore_permissions=True)

	def test_only_the_assigned_officer_or_an_admin_can_reassign(self):
		doc = self._assigned()

		frappe.set_user(self.stranger)
		with self.assertRaises(frappe.PermissionError):
			assignment.reassign(doc, self.dept_b, "A stranger must not move this case.")

		frappe.set_user(self.admin)
		res = reassign(
			doc.ticket_number,
			department=self.dept_b,
			reason="An administrator may reassign without holding the case.",
		)
		self.assertEqual(res["status"], "success")
		self.assertEqual(res["data"]["assignment"]["department"], self.dept_b)

	def test_api_denies_a_stranger(self):
		doc = self._assigned()
		frappe.set_user(self.stranger)
		denied = reassign(
			doc.ticket_number,
			department=self.dept_b,
			reason="The API must refuse a stranger as well.",
		)
		self.assertEqual(denied["status"], "error")
		self.assertEqual(denied["code"], "PERMISSION_DENIED")

	def test_api_denies_a_submitter(self):
		doc = self._assigned()
		frappe.set_user(self.submitter)
		denied = reassign(
			doc.ticket_number,
			department=self.dept_b,
			reason="A submitter must not reassign a case.",
		)
		self.assertEqual(denied["status"], "error")
		self.assertEqual(denied["code"], "PERMISSION_DENIED")

	def test_reassignment_rejects_a_department_the_rule_does_not_allow(self):
		doc = self._assigned()
		frappe.set_user(self.holder)

		with self.assertRaises(frappe.ValidationError):
			assignment.reassign(doc, "Missing Department", "Nowhere real to send it.")
		with self.assertRaises(frappe.ValidationError):
			assignment.reassign(doc, self.dept_c, "This department is not on the category rule.")
		with self.assertRaises(frappe.ValidationError):
			assignment.reassign(doc, self.dept_a, "Staying in the same department is not a reassignment.")

		outsider = self._user("stg337.outsider@example.com", ["Grievance Officer"])
		with self.assertRaises(frappe.ValidationError):
			assignment.reassign(
				doc,
				self.dept_b,
				"That person is not on the livestock desk.",
				officer=outsider,
			)

		saved = frappe.get_doc("Grievance", doc.name)
		self.assertEqual(saved.assigned_dept, self.dept_a)
		self.assertEqual(saved.assigned_to, self.holder)

	def test_initial_assignment_refuses_a_case_that_is_not_waiting(self):
		doc = self._assigned()
		again = assign(doc.ticket_number)
		self.assertEqual(again["code"], "VALIDATION_ERROR")

	def test_initial_assignment_refuses_when_no_rule_matches(self):
		waiting = self._case(self.other_category, self._type(self.other_category))
		missing = assign(waiting.ticket_number)
		self.assertEqual(missing["code"], "VALIDATION_ERROR")

	def _assigned(self):
		doc = self._case()
		assign(doc.ticket_number)
		return frappe.get_doc("Grievance", doc.name)

	def _case(self, category=None, gtype=None):
		return a_grievance(
			service_category=category or self.category,
			grievance_type=gtype or self.gtype,
			administrative_area=a_leaf_area(),
			submitter_type=a_submitter_type(),
		)

	def _category(self, name, code):
		if frappe.db.exists("Grievance Service Category", name):
			return name
		return (
			frappe.get_doc(
				{
					"doctype": "Grievance Service Category",
					"category_name": name,
					"code": code,
					"sort_order": 90,
					"is_active": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _type(self, category):
		existing = frappe.db.get_value("Grievance Type", {"service_category": category}, "name")
		if existing:
			return existing
		return (
			frappe.get_doc(
				{
					"doctype": "Grievance Type",
					"type_name": f"{category} type",
					"service_category": category,
					"is_active": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _department(self, name, email):
		if frappe.db.exists("Grievance Department", name):
			return name
		return (
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": name,
					"email_account": email,
					"active": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _user(self, email, roles):
		if frappe.db.exists("User", email):
			return email
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": email.split("@")[0],
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in roles],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _role_level(self):
		if frappe.db.exists("Grievance Role Level", "nodal_officer"):
			return
		frappe.get_doc(
			{
				"doctype": "Grievance Role Level",
				"level_code": "nodal_officer",
				"level_name": "Nodal Officer",
				"level_order": 10,
				"escalation_hours": 48,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)

	def _rule(self, category, department, precedence):
		return (
			frappe.get_doc(
				{
					"doctype": "Grievance Routing Rule",
					"rule_precedence": precedence,
					"active": 1,
					"service_category": category,
					"assigned_dept": department,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _desk(self, department, category, user):
		return (
			frappe.get_doc(
				{
					"doctype": "Grievance RBAC Assignment",
					"department_scope": department,
					"category_scope": category,
					"routing_strategy": "Primary First",
					"active": 1,
					"effective_from": frappe.utils.today(),
					"officers": [
						{
							"user": user,
							"role_level": "nodal_officer",
							"is_primary": 1,
							"active": 1,
						}
					],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _sla(self, category):
		if frappe.db.exists("Grievance SLA Configuration", {"service_category": category, "active": 1}):
			return
		frappe.get_doc(
			{
				"doctype": "Grievance SLA Configuration",
				"service_category": category,
				"sla_days": 10,
				"active": 1,
				"auto_escalate": 0,
			}
		).insert(ignore_permissions=True)
