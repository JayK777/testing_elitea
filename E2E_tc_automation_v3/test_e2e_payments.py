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


def _safe_mkdir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Failed to create directory {path}: {exc}") from exc


def _artifact_dir() -> Path:
    out_dir = Path(__file__).with_name("artifacts")
    _safe_mkdir(out_dir)
    return out_dir


def _save_failure_artifacts(page: Page, name: str) -> None:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)

    out_dir = _artifact_dir()
    screenshot_path = out_dir / f"{safe_name}_{timestamp}.png"
    html_path = out_dir / f"{safe_name}_{timestamp}.html"

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        LOGGER.info("Saved screenshot: %s", screenshot_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to save screenshot: %s", exc)

    try:
        html_path.write_text(page.content(), encoding="utf-8")
        LOGGER.info("Saved page HTML: %s", html_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to save page HTML: %s", exc)


def _require_key(dct: Dict[str, Any], key: str) -> Any:
    if key not in dct:
        raise KeyError(f"Missing required key in test_data.json: '{key}'")
    return dct[key]


def _selector(selectors: Dict[str, str], key: str) -> str:
    value = selectors.get(key)
    if not value:
        raise KeyError(f"Missing selector '{key}' in test_data.json")
    return value


def _goto(page: Page, base_url: str, path: str) -> None:
    url = f"{base_url}/{path.lstrip('/')}" if path else base_url
    LOGGER.info("Navigating to %s", url)
    page.goto(url, wait_until="domcontentloaded")


def _ui_login(page: Page, cfg: RuntimeConfig, test_data: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})
    creds = _require_key(test_data, "credentials")

    _goto(page, cfg.base_url, test_data.get("paths", {}).get("login", "/login"))
    page.fill(_selector(selectors, "login_username"), str(_require_key(creds, "username")))
    page.fill(_selector(selectors, "login_password"), str(_require_key(creds, "password")))

    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click(_selector(selectors, "login_submit"))

    assert page.url.startswith(cfg.base_url), "Unexpected redirect after login"


def _ui_add_item_to_cart(page: Page, cfg: RuntimeConfig, test_data: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})
    _goto(page, cfg.base_url, test_data.get("paths", {}).get("shop", "/"))

    page.click(_selector(selectors, "first_item"))
    page.click(_selector(selectors, "add_to_cart"))
    page.click(_selector(selectors, "go_to_cart"))


def _ui_proceed_to_checkout(page: Page, test_data: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})
    page.click(_selector(selectors, "proceed_to_checkout"))


def _ui_select_payment_method(page: Page, test_data: Dict[str, Any], method: str) -> None:
    selectors = test_data.get("selectors", {})

    # Method can be "card", "wallet", "net_banking" etc.
    mapping = {
        "card": "payment_card",
        "wallet": "payment_wallet",
        "net_banking": "payment_net_banking",
    }
    selector_key = mapping.get(method)
    if not selector_key:
        raise ValueError(f"Unsupported payment method: {method}")

    page.click(_selector(selectors, selector_key))


def _ui_pay_by_card(page: Page, test_data: Dict[str, Any], card: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})

    page.fill(_selector(selectors, "card_number"), str(_require_key(card, "number")))
    page.fill(_selector(selectors, "card_expiry"), str(_require_key(card, "expiry")))
    page.fill(_selector(selectors, "card_cvv"), str(_require_key(card, "cvv")))

    page.click(_selector(selectors, "pay_now"))


def _ui_wait_for_status(page: Page, test_data: Dict[str, Any]) -> Tuple[str, str]:
    selectors = test_data.get("selectors", {})
    timeout_ms = int(test_data.get("timeouts", {}).get("payment_status_ms", 60000))

    status_el = _selector(selectors, "payment_status")
    page.wait_for_selector(status_el, timeout=timeout_ms)

    status_text = page.inner_text(status_el).strip()

    order_id = ""
    order_id_selector = selectors.get("order_id")
    if order_id_selector:
        try:
            order_id = page.inner_text(order_id_selector).strip()
        except Exception:  # noqa: BLE001
            order_id = ""

    LOGGER.info("Payment status: %s, order_id: %s", status_text, order_id)
    return status_text, order_id


def _api_get_order_status(cfg: RuntimeConfig, order_id: str) -> Optional[Dict[str, Any]]:
    if not cfg.api_base_url or not cfg.api_token or not order_id:
        return None

    try:
        import requests

        url = f"{cfg.api_base_url.rstrip('/')}/orders/{order_id}"
        headers = {"Authorization": f"Bearer {cfg.api_token}"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("API order status check failed: %s", exc)
        return None


def _api_cancel_order(cfg: RuntimeConfig, order_id: str) -> bool:
    if not cfg.api_base_url or not cfg.api_token or not order_id:
        return False

    try:
        import requests

        url = f"{cfg.api_base_url.rstrip('/')}/orders/{order_id}/cancel"
        headers = {"Authorization": f"Bearer {cfg.api_token}"}
        resp = requests.post(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("API cancel/refund call failed: %s", exc)
        return False


def _db_fetch_payment_row(cfg: RuntimeConfig, order_id: str) -> Optional[Dict[str, Any]]:
    if not cfg.pg_dsn or not order_id:
        return None

    try:
        import psycopg2
        import psycopg2.extras

        query = """
            SELECT order_id, status, updated_at
            FROM payments
            WHERE order_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
        """

        with psycopg2.connect(cfg.pg_dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, (order_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("DB validation failed: %s", exc)
        return None
