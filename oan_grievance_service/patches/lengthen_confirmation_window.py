# Copyright (c) 2026, COSS - Centre for Open Societal Systems and contributors
# For license information, please see license.txt
"""Give the Resolved state's confirmation window 30 days instead of 7.

The submitter may reopen (appeal) a decision for 30 days (Routing Matrix, "Appeal and
reinvestigation"); the seeded default was 7. Only a timer still at the old default is moved:
an administrator who set another length keeps it. Cases already Resolved keep the deadline they
were given; the new length applies from the next resolution.
"""

import frappe

OLD_HOURS = 7 * 24


def execute():
	from oan_grievance_service.services import constants as C

	new_hours = next(hours for state, hours, _expiry in C.DEFAULT_STATE_TIMERS if state == C.STATE_RESOLVED)
	frappe.db.set_value(
		"Grievance State Timer",
		{
			"parenttype": "Grievance Deferral Policy",
			"parent": "Grievance Deferral Policy",
			"workflow_state": C.STATE_RESOLVED,
			"hours": OLD_HOURS,
		},
		"hours",
		new_hours,
		update_modified=False,
	)
