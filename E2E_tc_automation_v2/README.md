# E2E Pipeline Automation (v2)

Automation scripts generated from `pipeline_testcase.xlsx` (Automation-tagged scenarios).

## Tech stack
- Language: Python
- UI: Playwright (sync)
- API: requests
- DB: PostgreSQL (psycopg2)

## Setup
1. Create and activate a virtualenv
2. Install dependencies:
   - `pip install -U pytest playwright requests psycopg2-binary`
   - `playwright install`
3. Configure environment variables as needed:
   - `BASE_URL` (UI)
   - `API_BASE_URL` (API)
   - `DB_DSN` (PostgreSQL connection string)

## Run
- UI tests: `pytest -q E2E_tc_automation_v2/test_payment_ui.py`
- API/DB tests: `pytest -q E2E_tc_automation_v2/test_refund_api_db.py`

## Notes
Selectors/endpoints/test data are in `test_data.json` and can be adjusted for your environment.
