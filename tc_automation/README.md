# tc_automation — Login Functionality Test Suite
> **Jira Story**: [EP-2 — Login Functionality](https://epam-elitea-trial.atlassian.net/browse/EP-2)

## Overview

Automated Python test scripts for the **Login Functionality** user story (EP-2).
Covers UI (Playwright), API (requests), and DB (PostgreSQL) layers.

---

## Folder Structure

```
tc_automation/
├── happy_path.py                       # TC_01, TC_02, TC_13 (happy path + remember me)
├── field_validation.py                 # TC_03, TC_04, TC_05, TC_10, TC_12 (field validation + UI controls)
├── invalid_credentials_and_lockout.py  # TC_06, TC_07, TC_08, TC_09 (neg + lockout)
├── security_tests.py                   # TC_11, TC_12(CSRF), TC_14, TC_15 (security)
├── test_data.json                      # Centralised test data configuration
├── requirements.txt                    # Python dependencies
├── utils/
│   ├── __init__.py
│   ├── browser_helper.py               # Playwright reusable helpers
│   ├── api_helper.py                   # requests HTTP helpers
│   ├── db_helper.py                    # PostgreSQL helpers (psycopg2)
│   ├── config_loader.py                # JSON config loader
│   └── reporter.py                     # Test result reporting
└── README.md
```

---

## Setup

```bash
# 1. Install dependencies
pip install -r tc_automation/requirements.txt

# 2. Install Playwright browsers
playwright install chromium

# 3. Update test_data.json with your environment URLs and DB credentials
```

---

## Running Tests

```bash
# Run each script independently
python tc_automation/happy_path.py
python tc_automation/field_validation.py
python tc_automation/invalid_credentials_and_lockout.py
python tc_automation/security_tests.py
```

Reports are saved to `reports/` as timestamped JSON files.

---

## Test Coverage Summary

| Script | TC IDs | Type |
|--------|--------|------|
| `happy_path.py` | TC_01, TC_02, TC_13 | UI (Playwright) |
| `field_validation.py` | TC_03, TC_04, TC_05, TC_10, TC_12 | UI (Playwright) |
| `invalid_credentials_and_lockout.py` | TC_06, TC_07, TC_08, TC_09 | UI + API + DB |
| `security_tests.py` | TC_11, TC_12(CSRF), TC_14, TC_15 | API + UI |
