# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GrievanceReassignmentRequest(Document):
	def on_update(self):
		# Insert has no previous version. A later save of an auto-routed row would
		# let someone rewrite who the case moved to after the audit was written.
		if self.flags.in_insert:
			return
		before = self.get_doc_before_save()
		if before and before.auto_routed:
			frappe.throw(
				_("An auto-routed reassignment is an audit record and cannot be modified."),
				title=_("Immutable Record"),
			)

	def on_trash(self):
		if self.auto_routed and not frappe.flags.in_uninstall:
			frappe.throw(
				_("An auto-routed reassignment is an audit record and cannot be deleted."),
				title=_("Immutable Record"),
			)
