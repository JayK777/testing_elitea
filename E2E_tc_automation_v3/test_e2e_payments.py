"""E2E payment flow automation (Playwright + optional API/DB checks).

Scenarios automated (tagged A in pipeline_testcase_v1.xlsx):
- TC_01: Happy path payment success
- TC_02: Payment failure with retry/alternate method
- TC_04: Refund/cancellation updates status and notifies user

How to run (example):
  pip install playwright pytest requests psycopg2-binary
  playwright install
  pytest -q E2E_tc_automation_v3/test_e2e_payments.py

Configuration:
- Update E2E_tc_automation_v3/test_data.json for selectors and test data.
- Optionally set env vars for API/DB integrations.

Environment variables (optional):
- BASE_URL: overrides test_data.json base_url
- API_BASE_URL: base URL for backend API checks
- API_TOKEN: bearer token for API calls
- PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD: PostgreSQL connection
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    api_base_url: Optional[str]
    api_token: Optional[str]
    pg_dsn: Optional[str]


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    if not data_path.exists():
        raise FileNotFoundError(f"Missing test data file: {data_path}")

    with data_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_pg_dsn_from_env() -> Optional[str]:
    host = os.getenv("PGHOST")
    db = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    port = os.getenv("PGPORT", "5432")

    if not all([host, db, user, password]):
        return None

    # psycopg2 DSN format
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _get_runtime_config(test_data: Dict[str, Any]) -> RuntimeConfig:
    base_url = os.getenv("BASE_URL") or str(test_data.get("base_url", "")).strip()
    if not base_url:
        raise ValueError("base_url is required (set in test_data.json or BASE_URL env var)")

    return RuntimeConfig(
        base_url=base_url.rstrip("/"),
        api_base_url=os.getenv("API_BASE_URL") or test_data.get("api_base_url"),
        api_token=os.getenv("API_TOKEN") or test_data.get("api_token"),
        pg_dsn=_build_pg_dsn_from_env() or test_data.get("pg_dsn"),
    )
