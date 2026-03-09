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


def _get(data: Dict[str, Any], dotted_path: str, default: Any = None) -> Any:
    """Safely get a nested value via dot notation."""

    cursor: Any = data
    for part in dotted_path.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return default
        cursor = cursor[part]
    return cursor


def _require(data: Dict[str, Any], dotted_path: str) -> Any:
    value = _get(data, dotted_path)
    if value in (None, ""):
        raise TestDataError(f"Missing required test data: '{dotted_path}'")
    return value


def _api_config(data: Dict[str, Any]) -> Optional[ApiConfig]:
    base_url = _get(data, "api.base_url", "").strip()
    order_status_path = _get(data, "api.order_status_path", "").strip()

    if not base_url or not order_status_path:
        return None

    token = _env("API_TOKEN", _get(data, "api.token", ""))
    return ApiConfig(base_url=base_url.rstrip("/"), token=token, order_status_path=order_status_path)


def _db_config(data: Dict[str, Any]) -> Optional[DbConfig]:
    host = _get(data, "db.host", "").strip()
    database = _get(data, "db.database", "").strip()
    user = _get(data, "db.user", "").strip()

    if not host or not database or not user:
        return None

    return DbConfig(
        host=host,
        port=int(_get(data, "db.port", 5432)),
        database=database,
        user=user,
        password=_env("DB_PASSWORD", _get(data, "db.password", "")),
    )


def _safe_get_json(url: str, headers: Dict[str, str], timeout_s: int = 15) -> Dict[str, Any]:
    """HTTP GET with basic retry and clear error context."""

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout_s)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - provide richer context
            last_error = exc
            time.sleep(attempt)

    raise RuntimeError(f"API GET failed after retries: {url} ({last_error})")


def _fetch_order_status(api: ApiConfig, order_id: str) -> Dict[str, Any]:
    url = f"{api.base_url}{api.order_status_path.format(order_id=order_id)}"
    headers = {"Accept": "application/json"}
    if api.token:
        headers["Authorization"] = f"Bearer {api.token}"
    return _safe_get_json(url, headers=headers)


@contextmanager
def _db_conn(db: DbConfig) -> Generator[Any, None, None]:
    """PostgreSQL connection context manager.

    psycopg2 is imported lazily to avoid import errors in environments without DB deps.
    """

    try:
        import psycopg2  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "psycopg2 is required for DB checks. Install it or remove DB config."
        ) from exc

    conn = None
    try:
        conn = psycopg2.connect(
            host=db.host,
            port=db.port,
            dbname=db.database,
            user=db.user,
            password=db.password,
        )
        yield conn
    finally:
        if conn is not None:
            conn.close()


def _extract_order_id(raw_text: str) -> Optional[str]:
    """Try to extract an order id from any text/URL."""

    if not raw_text:
        return None

    patterns = [
        r"order\s*#\s*(\w+)",
        r"order[_-]?id[=:]\s*(\w+)",
        r"/orders/(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    return None
