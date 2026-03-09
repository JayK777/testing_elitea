"""E2E automation for Jira EP-25 payment pipeline (Automation-tagged cases).

Covered scenarios (from Excel):
- TC_01: Happy path credit-card payment success.
- TC_03: Pending payment resolves to final state (no duplicate charge).
- TC_05: Order cancellation updates payment status correctly.

Tech scope: Python + Playwright (UI) + requests (API) + PostgreSQL.

Notes:
- This test suite is data-driven via `test_data.json` in the same folder.
- Secrets/overrides should be provided via environment variables.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, Optional, Tuple

import pytest
import requests
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright


DATA_FILE = Path(__file__).with_name("test_data.json")
DEFAULT_TIMEOUT_MS = 30_000


class TestDataError(RuntimeError):
    """Raised when test data is missing or invalid."""


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    token: str
    order_status_path: str


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


def _load_test_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        raise TestDataError(f"Missing test data file: {DATA_FILE}")

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TestDataError(f"Invalid JSON in {DATA_FILE}: {exc}") from exc


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()
