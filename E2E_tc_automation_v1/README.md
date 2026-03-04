# E2E Pipeline Payment Automation (v1)

This folder contains Playwright + Pytest E2E automation scripts generated from `pipeline_testcase.xlsx` (Automation-tagged scenarios only).

## Tech stack
- Language: Python
- Web automation: Playwright
- API: `requests`
- Database: PostgreSQL (optional validations)

## Setup
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -U pip
pip install pytest playwright requests psycopg2-binary
playwright install
```

## Configuration
Set the following environment variables (or adapt in `test_data.json`):
- `BASE_URL` (e.g., `https://your-app.example.com`)
- `E2E_USERNAME`, `E2E_PASSWORD` (optional if login is required)

Optional DB validation:
- `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASSWORD`

## Run
```bash
pytest -q E2E_tc_automation_v1
```

## Notes
- The scripts are written to be **environment-agnostic**. Update selectors and URLs in `test_data.json` to match your AUT.
- Negative gateway scenarios (decline/timeout/5xx) assume you can simulate these in your test environment (e.g., using test card numbers, a stubbed gateway, or feature flags).
