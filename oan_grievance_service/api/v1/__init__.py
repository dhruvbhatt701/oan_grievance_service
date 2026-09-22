"""Version 1 of the public API.

This package is frozen once released: additive changes only. A breaking change opens
`api/v2/` rather than editing anything here. See `oan_grievance_service.api` for the
policy and the reasoning.

Endpoints
---------
    v1.grievance.submit     FSD FR-02 / 4.1   lodge a grievance on any channel
    v1.grievance.track      FSD FR-04         status lookup by ticket number
    v1.grievance.reply      FSD Appendix C    answer a More Info Needed request
    v1.grievance.confirm    FSD FR-06 / UC-03 confirm the resolution
    v1.grievance.reopen     FSD FR-06         reopen with a mandatory reason
    v1.grievance.escalate   FSD FR-07         escalate once the SLA has elapsed
    v1.grievance.assign     FR-03 / STG-337   initial assignment by Category+Department rule
    v1.grievance.reassign   FR-03 / STG-337   reassign without approval; SLA clock continues


    v1.administrative_area.get_areas   FSD 3.2.2   the location cascade and search

    v1.draft.save           FSD 3.2.1 / 7      persist a partial submission (POST /api/v1/drafts)
    v1.draft.load           FSD 3.2.1 / 7      resume the caller's latest draft (GET /api/v1/drafts)
    v1.draft.discard        FSD 3.2.1 / 7      abandon a draft (DELETE /api/v1/drafts)
    v1.attachment.submit_document   FSD 3.2.1  upload evidence (POST /api/v1/grievances/<g>/attachments, POST /api/v1/drafts/attachments)
    v1.attachment.get_attachments   FSD 3.2.1  list evidence with scan verdicts (GET /api/v1/grievances/<g>/attachments)
    v1.attachment.download          FSD 3.2.1  a clean file's URL (GET /api/v1/attachments/<id>/download)
    v1.attachment.delete            FSD 3.2.1  remove evidence from an open case (DELETE /api/v1/attachments/<id>)
"""

VERSION = "v1"
