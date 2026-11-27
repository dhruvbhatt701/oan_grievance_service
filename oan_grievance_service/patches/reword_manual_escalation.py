# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt
"""Reword the Manual Escalation notification for the officer it is sent to.

It said "escalated by the submitter", but a submitter cannot escalate: the event is sent to the
officer a case is escalated to by `POST /grievances/<ticket>/escalate`. It was never sent before
that endpoint existed.

`seed_notifications` skips a Notification that already exists, so the existing records are
rewritten here, and only while their message is still the original seeded text: a record an
admin has reworded is left alone.
"""

import frappe


def execute():
	from oan_grievance_service.services import constants as C
	from oan_grievance_service.services.notifications import template_context
	from oan_grievance_service.setup.install import NOTIFICATION_EVENTS, _translatable, _translatable_expr

	source = next(
		(
			(source, args)
			for code, _title, _role, _channels, _trigger, source, args in NOTIFICATION_EVENTS
			if code == C.EVENT_MANUAL_ESCALATION
		),
		None,
	)
	if not source:
		return

	for name, channel in frappe.get_all(
		"Notification",
		filters={"document_type": "Grievance", "method": C.EVENT_MANUAL_ESCALATION},
		fields=["name", "channel"],
		as_list=True,
	):
		context_key = template_context(C.EVENT_MANUAL_ESCALATION, channel)
		original = _translatable_expr(
			"Grievance {0} has been escalated by the submitter.", ("doc.ticket_number",), context_key
		)
		if frappe.db.get_value("Notification", name, "message") == original:
			frappe.db.set_value(
				"Notification", name, "message", _translatable(*source, context_key), update_modified=False
			)
