# STG-337 Definition of Done

| Criterion                | Status  | Evidence                                                                                                                                                          |
| ------------------------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Code reviewed            | Pending | Assignment and reassignment API on `feature/STG-337`.                                                                                                             |
| Unit tests passed        | Done    | 10 tests in `oan_grievance_service.tests.test_assignment_api` passed on `erpnext16.local` after migrate. |
| Documentation updated    | Done    | `docs/api-development-standards.md` §4.3; Postman REST and RPC collections.                                                                                       |
| QA passed                | Pending | Cover rule-based initial assignment, reassignment with no approval, SLA clock unchanged, immutable audit row, and denial for a stranger and a submitter.         |
| No Critical/High defects | Pending | Routing rule selects the department; RBAC desk selects the officer; `auto_routed` rows skip the approval hook and cannot be edited.                              |

## Run DoD verification (WSL)

```bash
cd ~/frappe16-bench
bench --site erpnext16.local migrate
bench --site erpnext16.local run-tests --app oan_grievance_service --module oan_grievance_service.tests.test_assignment_api
```
