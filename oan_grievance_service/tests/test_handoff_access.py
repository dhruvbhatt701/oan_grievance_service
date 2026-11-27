# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""An officer who hands a case to someone outside their reporting chain still reads it.

The case leaves their queue, but opening the ticket they just reassigned must not answer
Forbidden. Reading is all that is kept: they cannot act on it.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from oan_grievance_service import permissions
from oan_grievance_service.api.v1 import grievance as grievance_api
from oan_grievance_service.services import lifecycle
from oan_grievance_service.setup.install import seed_role_levels, seed_workflow
from oan_grievance_service.tests.fixtures import (
	a_desk,
	a_grievance,
	a_service_category,
	a_unique_department,
	a_user_with_role,
	discard_grievance,
)


class TestHandoffAccess(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		seed_role_levels()
		seed_workflow()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.savepoint = "handoff_access"
		frappe.db.savepoint(self.savepoint)

		src_dept, dst_dept = a_unique_department("src"), a_unique_department("dst")
		category = a_service_category()
		self.nodal = a_user_with_role("Grievance Officer", "nodal")
		self.receiver = a_user_with_role("Grievance Officer", "receiver")
		self.bystander = a_user_with_role("Grievance Officer", "bystander")
		desks = [
			a_desk(src_dept, category, "nodal_officer", (self.nodal, None)),
			a_desk(dst_dept, category, "nodal_officer", (self.receiver, None)),
		]
		for desk in desks:
			frappe.db.set_value("Grievance RBAC Assignment", desk, "reassignment_requires_approval", 0)
		self.dst_dept = dst_dept
		self.case = a_grievance(assigned_dept=src_dept, service_category=category, assigned_to=self.nodal)
		lifecycle.transition(self.case, "Assign")

	def tearDown(self):
		frappe.set_user("Administrator")
		discard_grievance(self.case.name)
		frappe.db.rollback(save_point=self.savepoint)
		super().tearDown()

	def _hand_off(self):
		"""The nodal officer asks; another department's officer is out of their line, so the move
		waits for a ruling (here an administrator's, as the receiving desk has no head)."""
		frappe.set_user(self.nodal)
		grievance_api.reassign(
			ticket_number=self.case.ticket_number,
			target_department=self.dst_dept,
			target_officer=self.receiver,
		)
		frappe.set_user("Administrator")
		self.case.reload()
		self.assertEqual(self.case.assigned_to, self.nodal)
		res = grievance_api.decide_reassignment(ticket_number=self.case.ticket_number, decision="Approved")
		self.assertEqual(res["status"], "success", res)
		self.case.reload()
		self.assertEqual(self.case.assigned_to, self.receiver)

	def test_officer_who_handed_the_case_on_can_still_open_it(self):
		self._hand_off()
		frappe.set_user(self.nodal)
		self.assertEqual(grievance_api._load(self.case.ticket_number).name, self.case.name)

	def test_handing_it_on_grants_read_only(self):
		self._hand_off()
		self.assertTrue(permissions.has_grievance_permission(self.case, "read", self.nodal))
		self.assertFalse(permissions.has_grievance_permission(self.case, "write", self.nodal))

	def test_handed_case_leaves_the_officers_queue(self):
		self._hand_off()
		queue = frappe.get_list("Grievance", filters={"name": self.case.name}, user=self.nodal, pluck="name")
		self.assertEqual(queue, [])

	def test_other_officers_still_cannot_open_it(self):
		self._hand_off()
		self.assertFalse(permissions.has_grievance_permission(self.case, "read", self.bystander))
