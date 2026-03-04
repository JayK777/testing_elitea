# E2E Pipeline v3 - Automation Suite (Playwright + Requests + PostgreSQL)

This folder contains Python automation scripts generated from `pipeline_testcase.xlsx` (Automation-tagged scenarios only).

## Tech stack
- Language: Python
- Web: Playwright
- API: `requests`
- DB: PostgreSQL (`psycopg` or `psycopg2`)

## Setup
1. Create/activate a virtualenv
2. Install dependencies:
   - `pip install pytest playwright requests psycopg[binary]`  (or `psycopg2-binary`)
   - `python -m playwright install`
3. Update `test_data.json` with your environment URLs, selectors, credentials, and (optionally) API/DB settings.

## Run
- `pytest -q e2e_pipeline_v3`

## Notes
- Some tests require environment-specific selectors/endpoints. If required config is missing, tests will be skipped with a clear reason.
