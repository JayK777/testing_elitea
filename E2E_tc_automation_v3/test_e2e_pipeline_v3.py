"""E2E payment checkout automation (Automation-tagged scenarios only).

Covers:
- TC_01: Happy path card payment success
- TC_02: Card payment declined -> error + retry/change method
- TC_05: Cancel/refund after success -> status updated + idempotent

Tech:
- Python + Playwright (web)
- requests (API verification)
- PostgreSQL (optional DB verification)

Execution examples:
- pytest -q E2E_tc_automation_v3/test_e2e_pipeline_v3.py
- python E2E_tc_automation_v3/test_e2e_pipeline_v3.py

Environment overrides (optional):
- E2E_BASE_URL, E2E_API_BASE_URL
- E2E_DB_HOST, E2E_DB_PORT, E2E_DB_NAME, E2E_DB_USER, E2E_DB_PASSWORD, E2E_DB_SSLMODE
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


LOGGER = logging.getLogger(__name__)


DATA_FILE = Path(__file__).with_name("test_data.json")
ARTIFACTS_DIR = Path(__file__).with_suffix("").with_name("artifacts")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        stream=sys.stdout,
    )


def load_test_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Missing test data file: {DATA_FILE}")
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def env_override(data: Dict[str, Any]) -> Dict[str, Any]:
    """Override selected config values from env vars (safe for CI)."""
    data = dict(data)
    web = dict(data.get("web", {}))
    api = dict(data.get("api", {}))
    db = dict(data.get("db", {}))

    web["base_url"] = os.getenv("E2E_BASE_URL", web.get("base_url"))
    api["base_url"] = os.getenv("E2E_API_BASE_URL", api.get("base_url"))

    db["host"] = os.getenv("E2E_DB_HOST", db.get("host"))
    db["port"] = int(os.getenv("E2E_DB_PORT", str(db.get("port", 5432))))
    db["database"] = os.getenv("E2E_DB_NAME", db.get("database"))
    db["user"] = os.getenv("E2E_DB_USER", db.get("user"))
    db["password"] = os.getenv("E2E_DB_PASSWORD", db.get("password"))
    db["sslmode"] = os.getenv("E2E_DB_SSLMODE", db.get("sslmode", "prefer"))

    data["web"] = web
    data["api"] = api
    data["db"] = db
    return data


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: str = "prefer"


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    timeout_ms: int
    username: str
    password: str
    api_base_url: str
    api_order_status_endpoint: str
    api_refund_endpoint: str
    db: DbConfig


def build_config(data: Dict[str, Any]) -> AppConfig:
    web = data["web"]
    creds = data["credentials"]
    api = data["api"]
    db = data["db"]

    return AppConfig(
        base_url=str(web["base_url"]).rstrip("/"),
        timeout_ms=int(web.get("timeout_ms", 30000)),
        username=str(creds["username"]),
        password=str(creds["password"]),
        api_base_url=str(api["base_url"]).rstrip("/"),
        api_order_status_endpoint=str(api["order_status_endpoint"]),
        api_refund_endpoint=str(api["refund_endpoint"]),
        db=DbConfig(
            host=str(db["host"]),
            port=int(db.get("port", 5432)),
            database=str(db["database"]),
            user=str(db["user"]),
            password=str(db["password"]),
            sslmode=str(db.get("sslmode", "prefer")),
        ),
    )
