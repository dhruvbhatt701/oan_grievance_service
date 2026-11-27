# Copyright (c) 2026, COSS - Centre for Open Societal Systems and Contributors
# See license.txt

"""Acceptance tests for Grievance Change Request.

Covers:
- Submitter change request restriction
- Department and officer reassignment with hierarchy routing & auto-approval
- SLA deferral request validation (+days) and approval
- Hierarchy forwarding on timeout and admin queue fallback
- Immutability of frozen fields and stale state prevention
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, now_datetime, today

from oan_grievance_service.services import constants as C
from oan_grievance_service.services import lifecycle
from oan_grievance_service.setup.install import seed_notifications, seed_role_levels, seed_workflow
from oan_grievance_service.tests.fixtures import (
	a_department,
	a_grievance,
	a_leaf_area,
	discard_grievance,
)


def _ensure_test_user(email, role="Grievance Officer", first_name=None):
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first_name or email.split("@")[0],
				"send_welcome_email": 0,
				"roles": [{"role": role}],
			}
		).insert(ignore_permissions=True)
	else:
		doc = frappe.get_doc("User", email)
	return doc.email


from oan_grievance_service.api.router import ensure_routes_registered


class TestGrievanceChangeRequest(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_routes_registered()
		seed_role_levels()
		seed_workflow()
		seed_notifications()

	def setUp(self):
		frappe.set_user("Administrator")
		self.area = a_leaf_area()
		dept_name = f"Primary Dept {frappe.generate_hash(length=6)}"
		self.dept = (
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": dept_name,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

		self.officer1 = _ensure_test_user("cr_officer1@example.com", "Grievance Officer", "Officer One")
		self.officer2 = _ensure_test_user("cr_officer2@example.com", "Grievance Officer", "Officer Two")
		self.senior = _ensure_test_user("cr_senior@example.com", "Grievance Officer", "Senior Nodal")
		self.head = _ensure_test_user("cr_head@example.com", "Grievance Officer", "Dept Head")
		self.head2 = _ensure_test_user("cr_head2@example.com", "Grievance Officer", "Dept 2 Head")
		self.submitter_user = _ensure_test_user(
			"cr_submitter@example.com", "Grievance Submitter", "Submitter"
		)

		# Ensure second department for reassignment tests (distinct from self.dept)
		dept2_name = f"Secondary Dept {frappe.generate_hash(length=6)}"
		self.dept2 = (
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": dept2_name,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

		# Setup RBAC hierarchy
		# officer1 (nodal_officer) -> senior (senior_nodal_officer) -> head (department_head)
		self.assignment1 = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": self.dept,
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": today(),
				"officers": [
					{
						"user": self.officer1,
						"role_level": "nodal_officer",
						"reports_to": self.senior,
						"is_primary": 1,
						"active": 1,
					},
					{
						"user": self.officer2,
						"role_level": "nodal_officer",
						"reports_to": self.senior,
						"is_primary": 0,
						"active": 1,
					},
					{
						"user": self.senior,
						"role_level": "senior_nodal_officer",
						"reports_to": self.head,
						"is_primary": 0,
						"active": 1,
					},
					{
						"user": self.head,
						"role_level": "department_head",
						"is_primary": 0,
						"active": 1,
					},
				],
			}
		).insert(ignore_permissions=True)

		self.assignment2 = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": self.dept2,
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": today(),
				"officers": [
					{
						"user": self.officer2,
						"role_level": "nodal_officer",
						"reports_to": self.head2,
						"is_primary": 1,
						"active": 1,
					},
					{
						"user": self.head2,
						"role_level": "department_head",
						"is_primary": 0,
						"active": 1,
					},
				],
			}
		).insert(ignore_permissions=True)

		self.grievance = a_grievance(
			assigned_dept=self.dept,
			assigned_to=self.officer1,
		)
		lifecycle.transition(self.grievance, "Assign")
		lifecycle.transition(self.grievance, "In Progress", reason="Investigation commenced.")

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		if hasattr(self, "assignment1") and frappe.db.exists(
			"Grievance RBAC Assignment", self.assignment1.name
		):
			frappe.delete_doc(
				"Grievance RBAC Assignment",
				self.assignment1.name,
				force=True,
				ignore_permissions=True,
				delete_permanently=True,
			)
		if hasattr(self, "assignment2") and frappe.db.exists(
			"Grievance RBAC Assignment", self.assignment2.name
		):
			frappe.delete_doc(
				"Grievance RBAC Assignment",
				self.assignment2.name,
				force=True,
				ignore_permissions=True,
				delete_permanently=True,
			)
		if hasattr(self, "grievance"):
			for cr in frappe.get_all(
				"Grievance Change Request", filters={"grievance": self.grievance.name}, pluck="name"
			):
				frappe.db.delete("Grievance Change Request", {"name": cr})
				frappe.db.delete("Grievance Change Request Item", {"parent": cr})
		if hasattr(self, "dept2") and frappe.db.exists("Grievance Department", self.dept2):
			frappe.delete_doc(
				"Grievance Department",
				self.dept2,
				force=True,
				ignore_permissions=True,
				delete_permanently=True,
			)
		if hasattr(self, "grievance") and frappe.db.exists("Grievance", self.grievance.name):
			discard_grievance(self.grievance.name)
		frappe.db.rollback()

	# ---------------------------------------------------------
	# Submitter Change Request Tests
	# ---------------------------------------------------------

	def test_submitter_cannot_request_change_requests(self):
		"""Submitters cannot request changes on a grievance."""
		frappe.set_user(self.submitter_user)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Grievance Change Request",
					"grievance": self.grievance.name,
					"subject": "Illegal field change",
					"changes": [{"fieldname": "assigned_dept", "new_value": self.dept2}],
				}
			).insert(ignore_permissions=True)

	# ---------------------------------------------------------
	# Reassignment Request Tests
	# ---------------------------------------------------------

	def test_officer_reassignment_within_dept_routes_to_dept_head(self):
		"""Officer requesting reassignment within department routes to dept head; grievance unchanged."""
		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassign to secondary officer",
				"reason": "Workload balancing",
				"changes": [{"fieldname": "assigned_to", "new_value": self.officer2}],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Pending")
		self.assertEqual(cr.pending_with, self.head)
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_to, self.officer1)

	def test_senior_reassignment_to_own_report_auto_approves(self):
		"""A senior handing a case to an officer who reports to them needs no approval."""
		frappe.set_user(self.senior)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassign by senior",
				"reason": "Rebalance",
				"changes": [{"fieldname": "assigned_to", "new_value": self.officer2}],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Approved")
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_to, self.officer2)

	def test_dept_head_reassignment_within_dept_auto_approves(self):
		"""Department head reassigning within dept is approved on insert and applied immediately."""
		frappe.set_user(self.head)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Direct reassignment by head",
				"reason": "Urgent reassignment",
				"changes": [{"fieldname": "assigned_to", "new_value": self.officer2}],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Approved")
		self.assertIsNone(cr.pending_with)
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_to, self.officer2)

	def test_officer_reassignment_to_other_dept_routes_to_target_dept_head(self):
		"""Officer moving case to another dept routes to target dept head, not source head."""
		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassign to secondary department",
				"reason": "Case requires secondary department handling",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Pending")
		self.assertEqual(cr.pending_with, self.head2)
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept)
		self.assertEqual(self.grievance.assigned_to, self.officer1)

	def test_source_dept_head_reassignment_to_other_dept_routes_to_target_dept_head(self):
		"""Source dept head cannot push case to target dept alone; routes to target head."""
		frappe.set_user(self.head)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Handoff to dept 2",
				"reason": "Wrong jurisdiction",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Pending")
		self.assertEqual(cr.pending_with, self.head2)

	def test_target_dept_head_approves_or_rejects_reassignment(self):
		"""Target head approving applies reassignment with timeline & notification; rejecting leaves unchanged."""
		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassign to dept 2",
				"reason": "Transferred",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		# Reject test
		frappe.set_user(self.head2)
		cr.status = "Rejected"
		cr.decision_note = "Cannot accept case"
		cr.save(ignore_permissions=True)

		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept)
		self.assertEqual(self.grievance.assigned_to, self.officer1)

		# New request to test approve
		frappe.set_user(self.officer1)
		cr2 = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassign to dept 2 retry",
				"reason": "Transferred retry",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		frappe.set_user(self.head2)
		cr2.status = "Approved"
		cr2.decision_note = "Accepted"
		cr2.save(ignore_permissions=True)

		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept2)
		self.assertEqual(self.grievance.assigned_to, self.officer2)

		# Timeline entry verification
		timeline_entries = frappe.get_all(
			"Grievance Timeline",
			filters={"grievance": self.grievance.name},
			fields=["body", "entry_type"],
		)
		self.assertTrue(any("Reassign to dept 2 retry" in t.body for t in timeline_entries))

		# Notification queued for new officer
		logs = frappe.get_all(
			"Grievance Notification Log",
			filters={
				"grievance": self.grievance.name,
				"event": C.EVENT_ASSIGNED_MANUAL,
				"recipient": self.officer2,
			},
		)
		self.assertTrue(len(logs) > 0)

	def test_admin_reassignment_auto_approved(self):
		"""Administrator moves case to dept2: Approved on insert."""
		frappe.set_user("Administrator")
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Admin reassignment",
				"reason": "Administrative move",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Approved")
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept2)

	def test_reassignment_to_dept_without_head_routes_to_admin_queue(self):
		"""Target department without head routes to admin queue (pending_with is None)."""
		dept3 = (
			frappe.get_doc(
				{
					"doctype": "Grievance Department",
					"dept_name": f"Headless Dept {frappe.generate_hash(length=4)}",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

		desk3 = frappe.get_doc(
			{
				"doctype": "Grievance RBAC Assignment",
				"department_scope": dept3,
				"category_scope": "Inputs",
				"active": 1,
				"effective_from": today(),
				"officers": [
					{
						"user": self.officer2,
						"role_level": "nodal_officer",
						"is_primary": 1,
						"active": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		try:
			frappe.set_user(self.officer1)
			cr = frappe.get_doc(
				{
					"doctype": "Grievance Change Request",
					"grievance": self.grievance.name,
					"subject": "Move to headless dept",
					"reason": "Routing",
					"changes": [
						{"fieldname": "assigned_dept", "new_value": dept3},
						{"fieldname": "assigned_to", "new_value": self.officer2},
					],
				}
			).insert(ignore_permissions=True)

			self.assertEqual(cr.status, "Pending")
			self.assertIsNone(cr.pending_with)
		finally:
			frappe.delete_doc("Grievance RBAC Assignment", desk3.name, force=True, ignore_permissions=True)
			frappe.delete_doc("Grievance Department", dept3, force=True, ignore_permissions=True)

	def test_notification_queued_for_approving_head_when_pending(self):
		"""Notification row is queued for the approving head when a request goes pending."""
		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassign to secondary officer",
				"reason": "Workload",
				"changes": [{"fieldname": "assigned_to", "new_value": self.officer2}],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Pending")
		self.assertEqual(cr.pending_with, self.head)

		logs = frappe.get_all(
			"Grievance Notification Log",
			filters={
				"grievance": self.grievance.name,
				"event": C.EVENT_REASSIGNMENT_REQUESTED,
				"recipient": self.head,
			},
		)
		self.assertTrue(len(logs) > 0)

	def test_reassignment_requires_approval_flag_off_auto_approves(self):
		"""When reassignment_requires_approval is 0 on source desk, reassignment auto-approves."""
		self.assignment1.db_set("reassignment_requires_approval", 0)
		self.assignment1.reload()

		frappe.set_user(self.officer1)
		# 1a. A colleague at the same level is out of line: it waits for the head, flag or not.
		sideways = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Sideways within dept",
				"reason": "Sideways",
				"changes": [{"fieldname": "assigned_to", "new_value": self.officer2}],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(sideways.status, "Pending")
		self.assertEqual(sideways.pending_with, self.head)
		frappe.set_user("Administrator")
		sideways.decide("Rejected", "Administrator")
		frappe.set_user(self.officer1)

		# 1b. The next level up is in line: with the flag off it applies directly.
		cr1 = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Direct move within dept",
				"reason": "Direct",
				"changes": [{"fieldname": "assigned_to", "new_value": self.senior}],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(cr1.status, "Approved")
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_to, self.senior)

		# 2. To dept2, by the senior, with an officer below them who also sits on dept2's desk.
		frappe.set_user(self.senior)
		cr2 = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Direct move to dept2",
				"reason": "Direct across dept",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(cr2.status, "Approved")
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept2)

		# Restore
		self.assignment1.db_set("reassignment_requires_approval", 1)

	def test_source_flag_on_target_flag_off_and_subsequent_handoff(self):
		"""Moving from desk with approval ON to OFF is Pending; once moved, handoff in target desk is direct."""
		self.assignment1.db_set("reassignment_requires_approval", 1)
		self.assignment2.db_set("reassignment_requires_approval", 0)

		# Step 1: officer1 moving dept -> dept2 is Pending with head2
		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Move to dept2",
				"reason": "Move",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Pending")
		self.assertEqual(cr.pending_with, self.head2)

		# Step 2: head2 approves
		frappe.set_user(self.head2)
		cr.status = "Approved"
		cr.save(ignore_permissions=True)
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept2)

		# Step 3: Now that case sits on dept2 (which has flag OFF), handoff within dept2 is auto-approved
		frappe.set_user(self.officer2)
		cr_internal = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Handoff within dept2",
				"reason": "Direct handoff",
				"changes": [{"fieldname": "assigned_to", "new_value": self.head2}],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr_internal.status, "Approved")
		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_to, self.head2)

	def test_no_desk_found_for_source_requires_approval(self):
		"""When no source desk is found, safe default is that approval is required."""
		self.assignment1.db_set("active", 0)

		try:
			frappe.set_user(self.officer1)
			cr = frappe.get_doc(
				{
					"doctype": "Grievance Change Request",
					"grievance": self.grievance.name,
					"subject": "Move with no source desk",
					"reason": "Move",
					"changes": [
						{"fieldname": "assigned_dept", "new_value": self.dept2},
						{"fieldname": "assigned_to", "new_value": self.officer2},
					],
				}
			).insert(ignore_permissions=True)

			self.assertEqual(cr.status, "Pending")
			self.assertEqual(cr.pending_with, self.head2)
		finally:
			self.assignment1.db_set("active", 1)

	def test_change_request_rejection_leaves_grievance_unchanged(self):
		"""Rejecting a routed change request (SLA extension) keeps existing values."""
		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 10)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Request SLA Extension",
				"reason": "Need more time",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		frappe.set_user(self.senior)
		cr.status = "Rejected"
		cr.decision_note = "Maintain original deadline"
		cr.save(ignore_permissions=True)

		self.grievance.reload()
		self.assertEqual(str(self.grievance.sla_due_date)[:10], str(initial_deadline)[:10])

	# ---------------------------------------------------------
	# SLA Deferral Request Tests
	# ---------------------------------------------------------

	def test_sla_deferral_request_and_approval(self):
		"""Officer requests SLA deferral (+5 days), supervisor approves."""
		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 10)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Extend SLA Deadline",
				"reason": "Awaiting soil lab sample results",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		frappe.set_user(self.senior)
		cr.status = "Approved"
		cr.decision_note = "Approved SLA extension"
		cr.save(ignore_permissions=True)

		self.grievance.reload()
		self.assertEqual(str(self.grievance.sla_due_date)[:10], str(new_deadline)[:10])

	def test_sla_deferral_cannot_move_deadline_earlier(self):
		"""Deferral earlier than current due date is rejected on validation."""
		initial_deadline = add_days(today(), 10)
		earlier_deadline = add_days(today(), 5)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Grievance Change Request",
					"grievance": self.grievance.name,
					"subject": "Invalid Deferral",
					"reason": "Test",
					"changes": [{"fieldname": "sla_due_date", "new_value": str(earlier_deadline)}],
				}
			).insert(ignore_permissions=True)

	def test_sla_deferral_cannot_exceed_max_days(self):
		"""Deferral beyond max allowed ceiling (30 days) is rejected."""
		initial_deadline = add_days(today(), 5)
		excessive_deadline = add_days(today(), 40)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Grievance Change Request",
					"grievance": self.grievance.name,
					"subject": "Excessive Deferral",
					"reason": "Too far",
					"changes": [{"fieldname": "sla_due_date", "new_value": str(excessive_deadline)}],
				}
			).insert(ignore_permissions=True)

	def _raise_deferral(self, user, days, subject="Extend SLA Deadline"):
		"""A deferral of `days` from the case's current deadline, raised as `user`."""
		frappe.set_user(user)
		self.grievance.reload()
		new_deadline = add_days(self.grievance.sla_due_date, days)
		return frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": subject,
				"reason": "Awaiting soil lab sample results",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

	def _decide(self, request, user, status="Approved"):
		frappe.set_user(user)
		request.reload()
		request.status = status
		request.save(ignore_permissions=True)
		return request

	def test_only_the_l1_nodal_officer_may_defer_the_sla(self):
		"""STG-516: the L1's supervisor (L2) and the department head cannot defer, even on their own authority."""
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		from oan_grievance_service.grievance_masters.doctype.grievance_role_level.grievance_role_level import (
			GrievanceRoleLevel,
		)

		first = GrievanceRoleLevel.get_chain()[0]
		for senior in (self.senior, self.head):
			with self.subTest(senior=senior), self.assertRaises(frappe.PermissionError) as raised:
				self._raise_deferral(senior, 5)
			# The rung's own label, as configured, names who may defer.
			self.assertIn(first.level_name or first.name, str(raised.exception))
		self.assertFalse(frappe.db.exists("Grievance Change Request", {"grievance": self.grievance.name}))

		request = self._raise_deferral(self.officer1, 5)
		self.assertEqual(request.status, "Pending")
		self.assertEqual(request.pending_with, self.senior)

	def test_admin_may_still_defer_the_sla(self):
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		request = self._raise_deferral("Administrator", 5)
		self.assertEqual(request.status, "Approved")

	def test_defer_sla_endpoint_refuses_a_supervisor(self):
		"""The backend refuses it on the API too, not only when the UI hides the button."""
		import frappe.api

		from oan_grievance_service.tests.test_category_assignment import _keep_transaction
		from oan_grievance_service.tests.test_router import make_test_request

		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		frappe.set_user(self.senior)
		with _keep_transaction():
			res = frappe.api.handle(
				make_test_request(
					f"/api/v1/grievances/{self.grievance.ticket_number}/defer-sla",
					method="POST",
					data={"additional_days": 5, "reason": "Waiting for the soil laboratory to send results"},
				)
			)
		self.assertEqual(res.status_code, 403, res.get_data(as_text=True))
		self.assertFalse(frappe.db.exists("Grievance Change Request", {"grievance": self.grievance.name}))

	def test_no_further_deferral_once_one_is_applied(self):
		"""STG-516: after a 7-day deferral is applied, a 30-day one can be neither raised nor approved."""
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		self._decide(self._raise_deferral(self.officer1, 7), self.senior)

		with self.assertRaises(frappe.ValidationError) as raised:
			self._raise_deferral(self.officer1, 30)
		self.assertIn("already been deferred", str(raised.exception))

	def test_no_second_deferral_while_one_is_pending(self):
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		self._raise_deferral(self.officer1, 7)
		with self.assertRaises(frappe.ValidationError) as raised:
			self._raise_deferral(self.officer1, 3)
		self.assertIn("already awaiting a decision", str(raised.exception))

	def test_pending_deferral_cannot_be_approved_after_another_was_applied(self):
		"""Two requests raised before this rule existed: approving the second is refused."""
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		first = self._raise_deferral(self.officer1, 7)
		# Park the first so the second can be raised, as data from before the rule would be.
		frappe.db.set_value("Grievance Change Request", first.name, "status", "Rejected")
		second = self._raise_deferral(self.officer1, 7)
		frappe.db.set_value("Grievance Change Request", first.name, "status", "Pending")

		self._decide(first, self.senior)
		with self.assertRaises(frappe.ValidationError) as raised:
			self._decide(second, self.senior)
		self.assertIn("already been deferred", str(raised.exception))

	def test_reopened_case_may_be_deferred_again(self):
		"""A reopen starts a fresh SLA window, which has its own one deferral."""
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		self._decide(self._raise_deferral(self.officer1, 7), self.senior)
		self.grievance.db_set("sla_start_at", add_to_date(now_datetime(), seconds=1), update_modified=False)
		frappe.db.set_value(
			"Grievance Change Request",
			{"grievance": self.grievance.name},
			"decided_at",
			add_to_date(now_datetime(), minutes=-5),
		)
		self.assertEqual(self._raise_deferral(self.officer1, 7).status, "Pending")

	def test_deferral_length_is_capped_by_the_policy_ceiling(self):
		"""STG-516: the policy's max_deferral_days is the one limit, whatever it is set to."""
		self.grievance.db_set("sla_due_date", add_days(today(), 5), update_modified=False)
		frappe.db.set_single_value("Grievance Deferral Policy", "max_deferral_days", 10)
		frappe.clear_document_cache("Grievance Deferral Policy", "Grievance Deferral Policy")
		try:
			with self.assertRaises(frappe.ValidationError) as raised:
				self._raise_deferral(self.officer1, 11)
			self.assertIn("may not exceed 10 days", str(raised.exception))
			self.assertEqual(self._raise_deferral(self.officer1, 10).status, "Pending")
		finally:
			frappe.db.rollback()
			frappe.clear_document_cache("Grievance Deferral Policy", "Grievance Deferral Policy")

	# ---------------------------------------------------------
	# Forwarding & Escalation Tests
	# ---------------------------------------------------------

	def test_forwarding_up_the_hierarchy(self):
		"""forward() hands the request one step up the chain; reaching the top lands in admin queue."""
		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 10)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Request SLA Extension",
				"reason": "Complex investigation",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		# Initially with senior
		self.assertEqual(cr.pending_with, self.senior)

		# Forward 1: senior -> head
		cr.forward()
		cr.reload()
		self.assertEqual(cr.pending_with, self.head)

		# Forward 2: head -> None (admin queue)
		cr.forward()
		cr.reload()
		self.assertIsNone(cr.pending_with)

	def test_stale_scheduled_forwarding(self):
		"""forward_stale_change_requests() identifies overdue change requests and forwards them."""
		from oan_grievance_service.tasks import forward_stale_change_requests

		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 10)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Request SLA Extension",
				"reason": "Complex investigation",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		# Set pending_since in the past beyond escalation hours
		frappe.db.set_value(
			"Grievance Change Request",
			cr.name,
			"pending_since",
			add_to_date(now_datetime(), hours=-60),
			update_modified=False,
		)

		count = forward_stale_change_requests()
		self.assertGreaterEqual(count, 1)

		cr.reload()
		self.assertEqual(cr.pending_with, self.head)

	# ---------------------------------------------------------
	# Immutability & Validation Guards
	# ---------------------------------------------------------

	def test_cannot_raise_change_request_on_closed_case(self):
		"""Change requests can only be raised on open cases."""
		self.grievance.db_set("workflow_state", "Closed", update_modified=False)
		frappe.set_user(self.officer1)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Grievance Change Request",
					"grievance": self.grievance.name,
					"subject": "Invalid request on closed case",
					"changes": [{"fieldname": "assigned_to", "new_value": self.officer2}],
				}
			).insert(ignore_permissions=True)

	def test_requester_cannot_approve_their_own_request(self):
		"""Officer cannot decide their own change request unless unrestricted."""
		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 10)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Request SLA Extension",
				"reason": "Need more time",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		self.assertEqual(cr.status, "Pending")
		self.assertEqual(cr.pending_with, self.senior)

		# Officer 1 attempts to self-approve
		cr.status = "Approved"
		with self.assertRaises(frappe.PermissionError):
			cr.save(ignore_permissions=True)

	def test_already_decided_request_cannot_be_redecided(self):
		"""Cannot approve or reject an already decided request."""
		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 10)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Request SLA Extension",
				"reason": "Need more time",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		frappe.set_user(self.senior)
		cr.status = "Approved"
		cr.save(ignore_permissions=True)

		# Attempt to change decision
		cr.status = "Rejected"
		with self.assertRaises(frappe.ValidationError):
			cr.save(ignore_permissions=True)

	def test_change_request_api_list_pagination_and_serialization(self):
		"""Test API v1 change request list with pagination envelope and detail lookup."""
		from oan_grievance_service.api.v1.change_request import decide, get_request, list_requests

		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 8)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "API Deferral Request",
				"reason": "Rain delay",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		# List requests as senior
		frappe.set_user(self.senior)
		list_res = list_requests(scope="pending_with_me", page=1, page_size=10)
		self.assertEqual(list_res["status"], "success")
		self.assertIn("items", list_res["data"])
		self.assertIn("pagination", list_res["data"])
		self.assertGreaterEqual(list_res["data"]["pagination"]["total_count"], 1)
		self.assertTrue(any(i["name"] == cr.name for i in list_res["data"]["items"]))

		# Get request detail
		get_res = get_request(name=cr.name)
		self.assertEqual(get_res["status"], "success")
		self.assertEqual(get_res["data"]["name"], cr.name)
		self.assertEqual(get_res["data"]["subject"], "API Deferral Request")
		self.assertEqual(len(get_res["data"]["changes"]), 1)

		# Decide request
		decide_res = decide(name=cr.name, decision="Approved", note="Approved via API")
		self.assertEqual(decide_res["status"], "success")
		self.assertEqual(decide_res["data"]["change_request"]["status"], "Approved")

	def test_change_request_api_not_found_handling(self):
		"""Missing change request ID returns standard error envelope (mapped from DoesNotExistError)."""
		from oan_grievance_service.api.v1.change_request import decide, get_request

		res1 = get_request(name="NON_EXISTENT_CR_123")
		self.assertEqual(res1["status"], "error")

		res2 = decide(name="NON_EXISTENT_CR_123", decision="Approved")
		self.assertEqual(res2["status"], "error")

	def test_decide_reassignment_endpoint_works_for_target_dept_head(self):
		"""Target department head (who has read-only access on source case) can decide reassignment."""
		import json

		import frappe.api

		from oan_grievance_service.tests.test_router import make_test_request

		frappe.set_user(self.officer1)
		frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Move to Dept 2 for Specialised Review",
				"reason": "Expertise required",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
				],
			}
		).insert(ignore_permissions=True)

		# Target dept head decides via API
		frappe.set_user(self.head2)
		req = make_test_request(
			f"/api/v1/grievances/{self.grievance.ticket_number}/reassign/decide",
			method="POST",
			data={"decision": "Approved", "note": "Accepted transfer to Dept 2"},
		)
		res = frappe.api.handle(req)
		self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
		body = json.loads(res.get_data(as_text=True))
		self.assertEqual(body["status"], "success")

		self.grievance.reload()
		self.assertEqual(self.grievance.assigned_dept, self.dept2)

	def test_timeline_shows_change_request_note_with_names(self):
		"""The timeline reads a change request's note with labels and names, not ids and emails."""
		import json

		import frappe.api

		from oan_grievance_service.tests.fixtures import a_grievance_type
		from oan_grievance_service.tests.test_router import make_test_request

		old_type = self.grievance.grievance_type
		new_type = frappe.get_doc(
			{
				"doctype": "Grievance Type",
				"type_name": f"Loan Issue {frappe.generate_hash(length=6)}",
				"service_category": self.grievance.service_category,
				"is_active": 1,
			}
		).insert(ignore_permissions=True)
		self.assertEqual(old_type, a_grievance_type())

		frappe.set_user(self.officer1)
		cr = frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "Reassignment",
				"reason": "please review",
				"changes": [
					{"fieldname": "assigned_dept", "new_value": self.dept2},
					{"fieldname": "assigned_to", "new_value": self.officer2},
					{"fieldname": "grievance_type", "new_value": new_type.name},
				],
			}
		).insert(ignore_permissions=True)
		frappe.set_user(self.head2)
		cr.reload()
		cr.status = "Approved"
		cr.save(ignore_permissions=True)

		frappe.set_user("Administrator")
		req = make_test_request(
			f"/api/v1/grievances/{self.grievance.ticket_number}/timeline?limit=50", method="GET"
		)
		res = frappe.api.handle(req)
		self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
		events = json.loads(res.get_data(as_text=True))["data"]["timeline"]
		notes = [e for e in events if e["entry_type"] == "note" and e["is_internal"]]
		self.assertEqual(len(notes), 1)
		old_type_name = frappe.db.get_value("Grievance Type", old_type, "type_name")

		self.assertEqual(
			notes[0]["body"],
			"Reassignment (approved)\n"
			f"Department: {self.dept} -> {self.dept2}\n"
			"Assigned Officer: Officer One -> Officer Two\n"
			f"Grievance Type: {old_type_name} -> {new_type.type_name}\n"
			"Reason: please review",
		)
		self.assertEqual(
			[(c["fieldname"], c["old_display"], c["new_display"]) for c in notes[0]["changes"]],
			[
				("assigned_dept", self.dept, self.dept2),
				("assigned_to", "Officer One", "Officer Two"),
				("grievance_type", old_type_name, new_type.type_name),
			],
		)
		self.assertEqual(notes[0]["changes"][1]["old_value"], self.officer1)
		self.assertEqual(notes[0]["changes"][2]["new_value"], new_type.name)

		# The stored note is the audit record: it keeps the raw values.
		stored = frappe.db.get_value(
			"Grievance Timeline",
			{"grievance": self.grievance.name, "ref_docname": cr.name, "is_internal": 1},
			"body",
		)
		self.assertIn(f"assigned_to: {self.officer1} -> {self.officer2}", stored)
		self.assertIn(f"grievance_type: {old_type} -> {new_type.name}", stored)

		# The public line has no changes to show.
		public = [e for e in events if e["entry_type"] == "assignment"]
		self.assertEqual(len(public), 1)
		self.assertNotIn("changes", public[0])

	def test_decide_deferral_endpoint_works_for_approver(self):
		"""Designated approver can decide SLA deferral request via decide_deferral endpoint."""
		import json

		import frappe.api

		from oan_grievance_service.tests.test_router import make_test_request

		initial_deadline = add_days(today(), 5)
		new_deadline = add_days(today(), 9)
		self.grievance.db_set("sla_due_date", initial_deadline, update_modified=False)

		frappe.set_user(self.officer1)
		frappe.get_doc(
			{
				"doctype": "Grievance Change Request",
				"grievance": self.grievance.name,
				"subject": "SLA Deferral: +4 days",
				"reason": "Lab sample testing delays",
				"changes": [{"fieldname": "sla_due_date", "new_value": str(new_deadline)}],
			}
		).insert(ignore_permissions=True)

		# Approver decides via API
		frappe.set_user(self.senior)
		req = make_test_request(
			f"/api/v1/grievances/{self.grievance.ticket_number}/defer-sla/decide",
			method="POST",
			data={"decision": "Approved", "note": "Extension granted for lab analysis"},
		)
		res = frappe.api.handle(req)
		self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
		body = json.loads(res.get_data(as_text=True))
		self.assertEqual(body["status"], "success")

		self.grievance.reload()
		self.assertEqual(str(self.grievance.sla_due_date).split(" ")[0], str(new_deadline).split(" ")[0])
