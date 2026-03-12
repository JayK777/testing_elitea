"""Playwright E2E tests for Food Delivery App - Payments.

Automates scenarios tagged 'A' from pipeline_testcase_v2.xlsx:
- TC_01: Complete payment successfully using Credit/Debit Card (happy path)
- TC_02: Show clear error for invalid/expired card details without app crash
- TC_04: Refund/Cancellation updates payment status correctly and notifies the user

Execution (example):
  python -m pytest -q E2E_tc_automation_v4_check/test_payments.py

Prerequisites:
- Python 3.10+
- playwright installed and browsers installed:
    pip install pytest playwright requests psycopg2-binary
    playwright install

Configuration:
- Update E2E_tc_automation_v4_check/test_data.json with environment URLs/selectors.
- Optionally set env vars:
    HEADLESS=1|0
    BROWSER=chromium|firefox|webkit
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import requests
from playwright.sync_api import Browser, Error, Page, Playwright, sync_playwright

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover
    psycopg2 = None
    RealDictCursor = None


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    token: str


@dataclass(frozen=True)
class DbConfig:
    dsn: str


def load_test_data() -> Dict[str, Any]:
    """Load test data from the adjacent JSON file."""
    data_path = Path(__file__).with_name("test_data.json")
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing test data file: {data_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in test data file: {data_path}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _get_browser_name() -> str:
    return os.getenv("BROWSER", "chromium").strip().lower()


def _now_ms() -> int:
    return int(time.time() * 1000)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def safe_screenshot(page: Page, name: str) -> Optional[str]:
    """Capture screenshot; never raise from this helper."""
    try:
        out_dir = Path.cwd() / "playwright-artifacts"
        out_dir.mkdir(exist_ok=True)
        file_path = out_dir / f"{_now_ms()}_{name}.png"
        page.screenshot(path=str(file_path), full_page=True)
        LOGGER.info("Saved screenshot: %s", file_path)
        return str(file_path)
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Failed to capture screenshot: %s", exc)
        return None


def safe_click(page: Page, selector: str, timeout_ms: int) -> None:
    try:
        page.locator(selector).click(timeout=timeout_ms)
    except Error as exc:
        raise AssertionError(f"Failed to click selector: {selector}") from exc


def safe_fill(page: Page, selector: str, value: str, timeout_ms: int) -> None:
    try:
        page.locator(selector).fill(value, timeout=timeout_ms)
    except Error as exc:
        raise AssertionError(f"Failed to fill selector: {selector}") from exc


def get_api_config(test_data: Dict[str, Any]) -> Optional[ApiConfig]:
    api = test_data.get("api") or {}
    if not api.get("enabled"):
        return None
    base_url = str(api.get("base_url") or "").strip()
    token = str(api.get("token") or "").strip()
    if not base_url:
        raise RuntimeError("api.enabled is true but api.base_url is empty")
    return ApiConfig(base_url=base_url, token=token)


def get_db_config(test_data: Dict[str, Any]) -> Optional[DbConfig]:
    db = test_data.get("db") or {}
    if not db.get("enabled"):
        return None
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is required when db.enabled is true")
    dsn = str(db.get("dsn") or "").strip()
    if not dsn:
        raise RuntimeError("db.enabled is true but db.dsn is empty")
    return DbConfig(dsn=dsn)


def api_get_order_status(api_cfg: ApiConfig, order_id: str, timeout_s: int) -> Dict[str, Any]:
    url = f"{api_cfg.base_url.rstrip('/')}/orders/{order_id}"
    headers = {"Authorization": f"Bearer {api_cfg.token}"} if api_cfg.token else {}
    response = requests.get(url, headers=headers, timeout=timeout_s)
    response.raise_for_status()
    return response.json()


def db_get_payment_status(db_cfg: DbConfig, order_id: str) -> Optional[str]:
    query = "SELECT payment_status FROM orders WHERE order_id = %s"
    with psycopg2.connect(db_cfg.dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (order_id,))
            row = cur.fetchone()
            if not row:
                return None
            return str(row.get("payment_status"))


@pytest.fixture(scope="session", autouse=True)
def _logging() -> None:
    configure_logging()


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return load_test_data()


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Browser:
    headless = _env_bool("HEADLESS", True)
    browser_name = _get_browser_name()

    browser_type = getattr(playwright_instance, browser_name, None)
    if browser_type is None:
        raise RuntimeError(
            f"Unsupported BROWSER={browser_name}. Use chromium, firefox, or webkit."
        )

    browser = browser_type.launch(headless=headless)
    yield browser
    browser.close()


@pytest.fixture()
def page(browser: Browser, test_data: Dict[str, Any]) -> Page:
    context = browser.new_context(viewport=test_data.get("viewport"))
    page = context.new_page()
    yield page
    context.close()


class TestCheckoutPayments:
    """Automation scenarios for payment flow (Card + Refund)."""

    def _login_and_open_checkout(self, page: Page, test_data: Dict[str, Any]) -> None:
        selectors = test_data["selectors"]
        timeout_ms = int(test_data.get("timeout_ms", 15000))

        page.goto(test_data["base_url"], wait_until="domcontentloaded")
        safe_click(page, selectors["go_to_login"], timeout_ms)
        safe_fill(page, selectors["username"], test_data["credentials"]["username"], timeout_ms)
        safe_fill(page, selectors["password"], test_data["credentials"]["password"], timeout_ms)
        safe_click(page, selectors["login_submit"], timeout_ms)

        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        safe_click(page, selectors["open_checkout"], timeout_ms)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    def _pay_with_card(self, page: Page, test_data: Dict[str, Any], card: Dict[str, str]) -> None:
        selectors = test_data["selectors"]
        timeout_ms = int(test_data.get("timeout_ms", 15000))

        safe_click(page, selectors["payment_method_card"], timeout_ms)
        safe_fill(page, selectors["card_number"], card["number"], timeout_ms)
        safe_fill(page, selectors["card_expiry"], card["expiry"], timeout_ms)
        safe_fill(page, selectors["card_cvv"], card["cvv"], timeout_ms)
        safe_click(page, selectors["pay_button"], timeout_ms)

    def _wait_for_status_text(self, page: Page, selector: str, expected: str, timeout_ms: int) -> None:
        locator = page.locator(selector)
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            page.wait_for_function(
                "(el, expected) => el && el.innerText && el.innerText.includes(expected)",
                locator,
                expected,
                timeout=timeout_ms,
            )
        except Error as exc:
            actual = locator.inner_text() if locator.count() else "<missing>"
            raise AssertionError(
                f"Expected text '{expected}' not found. Actual: {actual}"
            ) from exc

    def _extract_order_id(self, page: Page, test_data: Dict[str, Any]) -> str:
        selectors = test_data["selectors"]
        timeout_ms = int(test_data.get("timeout_ms", 15000))

        locator = page.locator(selectors["order_id"]) 
        locator.wait_for(state="visible", timeout=timeout_ms)
        order_id = locator.inner_text().strip()
        if not order_id:
            raise AssertionError("Order id is empty on confirmation screen")
        return order_id

    def test_tc01_card_payment_happy_path(self, page: Page, test_data: Dict[str, Any]) -> None:
        """TC_01: Complete payment successfully using Credit/Debit Card (happy path)."""
        selectors = test_data["selectors"]
        timeout_ms = int(test_data.get("timeout_ms", 15000))
        api_cfg = get_api_config(test_data)
        db_cfg = get_db_config(test_data)

        try:
            self._login_and_open_checkout(page, test_data)
            self._pay_with_card(page, test_data, test_data["card_valid"])

            self._wait_for_status_text(
                page,
                selectors["payment_status"],
                expected=test_data["expected"]["payment_success_text"],
                timeout_ms=timeout_ms,
            )

            order_id = self._extract_order_id(page, test_data)
            LOGGER.info("Paid order id: %s", order_id)

            if api_cfg is not None:
                payload = api_get_order_status(
                    api_cfg,
                    order_id,
                    timeout_s=int(test_data.get("api_timeout_s", 15)),
                )
                assert payload.get("payment_status") in {"Paid", "Success"}

            if db_cfg is not None:
                status = db_get_payment_status(db_cfg, order_id)
                assert status in {"Paid", "Success"}

        except Exception:
            safe_screenshot(page, "tc01_card_payment_happy_path")
            raise

    def test_tc02_invalid_or_expired_card_shows_error(self, page: Page, test_data: Dict[str, Any]) -> None:
        """TC_02: Show clear error for invalid/expired card details without app crash."""
        selectors = test_data["selectors"]
        timeout_ms = int(test_data.get("timeout_ms", 15000))

        try:
            self._login_and_open_checkout(page, test_data)
            self._pay_with_card(page, test_data, test_data["card_invalid"])

            self._wait_for_status_text(
                page,
                selectors["payment_error"],
                expected=test_data["expected"]["payment_error_text"],
                timeout_ms=timeout_ms,
            )

            assert page.url, "Page URL should remain valid after failed payment"

        except Exception:
            safe_screenshot(page, "tc02_invalid_or_expired_card")
            raise

    def test_tc04_refund_cancellation_updates_status(self, page: Page, test_data: Dict[str, Any]) -> None:
        """TC_04: Refund/Cancellation updates payment status correctly and notifies the user."""
        selectors = test_data["selectors"]
        timeout_ms = int(test_data.get("timeout_ms", 15000))
        api_cfg = get_api_config(test_data)
        db_cfg = get_db_config(test_data)

        try:
            self._login_and_open_checkout(page, test_data)
            self._pay_with_card(page, test_data, test_data["card_valid"])

            self._wait_for_status_text(
                page,
                selectors["payment_status"],
                expected=test_data["expected"]["payment_success_text"],
                timeout_ms=timeout_ms,
            )
            order_id = self._extract_order_id(page, test_data)

            safe_click(page, selectors["go_to_orders"], timeout_ms)
            safe_click(page, selectors["open_order_by_id"].format(order_id=order_id), timeout_ms)

            safe_click(page, selectors["cancel_or_refund"], timeout_ms)
            safe_click(page, selectors["confirm_cancel_or_refund"], timeout_ms)

            self._wait_for_status_text(
                page,
                selectors["refund_status"],
                expected=test_data["expected"]["refund_success_text"],
                timeout_ms=timeout_ms,
            )
            self._wait_for_status_text(
                page,
                selectors["notification"],
                expected=test_data["expected"]["refund_notification_text"],
                timeout_ms=timeout_ms,
            )

            if api_cfg is not None:
                payload = api_get_order_status(
                    api_cfg,
                    order_id,
                    timeout_s=int(test_data.get("api_timeout_s", 15)),
                )
                assert payload.get("payment_status") in {"Refunded", "Cancelled"}

            if db_cfg is not None:
                status = db_get_payment_status(db_cfg, order_id)
                assert status in {"Refunded