"""End-to-end payment automation (UI + optional API/DB validation).

Covers Automation-tagged scenarios from pipeline_testcase_v2.xlsx:
- TC_01: Complete payment successfully using Credit/Debit Card (happy path)
- TC_02: Show clear error for invalid/expired card details without app crash
- TC_04: Refund/Cancellation updates payment status correctly and notifies the user

Tech stack: Python + Playwright + requests + (optional) PostgreSQL.

Notes:
- Update selectors/URLs/credentials in test_data.json to match your AUT.
- DB validation is optional and can be disabled via test_data.json.
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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TestConfig:
    web: Dict[str, Any]
    credentials: Dict[str, str]
    card_valid: Dict[str, str]
    card_invalid: Dict[str, str]
    api: Dict[str, Any]
    db: Dict[str, Any]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class ApiClient:
    def __init__(self, base_url: str, token: str, timeout_s: int = 30) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()
        if token:
            self._session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout_s)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            raise RuntimeError(f"API GET failed: {url} ({exc})") from exc


class DbClient:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self._cfg = cfg
        self._enabled = bool(cfg.get("enabled"))
        self._psycopg = None

        if not self._enabled:
            return

        try:
            import psycopg2  # type: ignore

            self._psycopg = psycopg2
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "DB validation is enabled but psycopg2 is not installed. "
                "Disable db.enabled or add psycopg2 to your environment."
            ) from exc

    def fetch_one(self, query: str, params: tuple[Any, ...]) -> Optional[tuple[Any, ...]]:
        if not self._enabled:
            return None

        conn = None
        try:
            conn = self._psycopg.connect(
                host=self._cfg.get("host"),
                port=self._cfg.get("port"),
                dbname=self._cfg.get("dbname"),
                user=self._cfg.get("user"),
                password=self._cfg.get("password"),
                sslmode=self._cfg.get("sslmode", "prefer"),
            )
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()
        except Exception as exc:
            raise RuntimeError(f"DB query failed: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()


class PaymentsUi:
    def __init__(self, page, cfg: TestConfig) -> None:
        self._page = page
        self._cfg = cfg
        self._selectors = cfg.web.get("selectors", {})

    def _sel(self, key: str) -> str:
        value = self._selectors.get(key)
        if not value:
            raise ConfigError(f"Missing selector: web.selectors.{key}")
        return value

    def goto_login(self) -> None:
        base_url = self._cfg.web.get("base_url")
        login_path = self._cfg.web.get("login_path", "/login")
        if not base_url:
            raise ConfigError("Missing web.base_url in test_data.json")

        self._page.goto(f"{base_url.rstrip('/')}{login_path}")

    def login(self) -> None:
        self.goto_login()
        self._page.fill(self._sel("username"), self._cfg.credentials["username"])
        self._page.fill(self._sel("password"), self._cfg.credentials["password"])
        self._page.click(self._sel("login_button"))

    def goto_checkout(self) -> None:
        base_url = self._cfg.web["base_url"].rstrip("/")
        checkout_path = self._cfg.web.get("checkout_path", "/checkout")
        self._page.goto(f"{base_url}{checkout_path}")

    def pay_with_card(self, card: Dict[str, str]) -> None:
        self.goto_checkout()
        self._page.click(self._sel("card_option"))

        self._page.fill(self._sel("card_number"), card["number"])
        self._page.fill(self._sel("card_expiry"), card["expiry"])
        self._page.fill(self._sel("card_cvv"), card["cvv"])

        name_selector = self._selectors.get("card_name")
        if name_selector and card.get("name"):
            self._page.fill(name_selector, card["name"])

        self._page.click(self._sel("pay_button"))

    def read_order_id(self) -> str:
        locator = self._page.locator(self._sel("order_id"))
        try:
            locator.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Order id not visible after payment attempt") from exc

        order_id = locator.inner_text().strip()
        if not order_id:
            raise RuntimeError("Order id element is empty")
        return order_id

    def assert_payment_success(self) -> None:
        try:
            self._page.locator(self._sel("payment_success")).wait_for(
                state="visible",
                timeout=int(self._cfg.web.get("timeouts_ms", {}).get("payment_processing", 60_000)),
            )
        except PlaywrightTimeoutError as exc:
            raise AssertionError("Payment success message not shown") from exc

    def assert_payment_error(self) -> str:
        locator = self._page.locator(self._sel("payment_error"))
        try:
            locator.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise AssertionError("Payment error message not shown") from exc

        return locator.inner_text().strip()

    def cancel_order(self) -> None:
        self._page.click(self._sel("cancel_order_button"))
        self._page.click(self._sel("confirm_cancel_button"))

    def assert_refund_success(self) -> None:
        locator = self._page.locator(self._sel("refund_status"))
        try:
            locator.wait_for(state="visible", timeout=60_000)
        except PlaywrightTimeoutError as exc:
            raise AssertionError("Refund/Cancel success status not shown") from exc


def _load_test_config() -> TestConfig:
    data_path = Path(__file__).with_name("test_data.json")
    if not data_path.exists():
        raise ConfigError(f"Missing test data file: {data_path}")

    raw = json.loads(data_path.read_text(encoding="utf-8"))

    def _req(key: str) -> Any:
        if key not in raw:
            raise ConfigError(f"Missing required key in test_data.json: {key}")
        return raw[key]

    return TestConfig(
        web=_req("web"),
        credentials=_req("credentials"),
        card_valid=_req("card_valid"),
        card_invalid=_req("card_invalid"),
        api=_req("api"),
        db=_req("db"),
    )


@pytest.fixture(scope="session")
def cfg() -> TestConfig:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return _load_test_config()


@pytest.fixture()
def ui(cfg: TestConfig):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=bool(int(os.getenv("HEADLESS", "1"))))
        context = browser.new_context()
        page = context.new_page()
        try:
            yield PaymentsUi(page=page, cfg=cfg)
        except Exception:
            ts = int(time.time())
            try:
                page.screenshot(path=f"playwright_failure_{ts}.png", full_page=True)
            except Exception:
                LOGGER.exception("Failed to capture screenshot")
            raise
        finally:
            context.close()
            browser.close()


@pytest.fixture(scope="session")
def api(cfg: TestConfig) -> ApiClient:
    return ApiClient(
        base_url=str(cfg.api.get("base_url", "")),
        token=str(cfg.api.get("token", "")),
        timeout_s=int(cfg.api.get("timeout_s", 30)),
    )


@pytest.fixture(scope="session")
def db(cfg: TestConfig) -> DbClient:
    return DbClient(cfg.db)


class TestCardPayments:
    """Automation-tagged payment flows (TC_01, TC_02, TC_04)."""

    def test_card_payment_happy_path_and_refund(self, ui: PaymentsUi, api: ApiClient, db: DbClient) -> None:
        """TC_01 + TC_04 combined: pay successfully, then cancel/refund and verify status updates."""
        ui.login()

        ui.pay_with_card(ui._cfg.card_valid)
        ui.assert_payment_success()
        order_id = ui.read_order_id()

        api_status_path = ui._cfg.api.get("order_status_path")
        if api_status_path:
            resp = api.get(str(api_status_path), params={"order_id": order_id})
            payload = resp.json()
            expected_paid = set(map(str.lower, ui._cfg.api.get("expected_paid_status", ["paid", "success"])))
            actual = str(payload.get("payment_status", "")).lower()
            assert actual in expected_paid, f"Unexpected API payment_status={actual} payload={payload}"

        db_query = ui._cfg.db.get("payment_status_query")
        if db_query:
            row = db.fetch_one(str(db_query), (order_id,))
            if row is not None:
                expected_paid_db = str(ui._cfg.db.get("expected_paid_status", "paid")).lower()
                actual_db = str(row[0]).lower()
                assert actual_db == expected_paid_db, f"Unexpected DB status={actual_db}"

        ui.cancel_order()
        ui.assert_refund_success()

        api_refund_path = ui._cfg.api.get("refund_status_path")
        if api_refund_path:
            resp = api.get(str(api_refund_path), params={"order_id": order_id})
            payload = resp.json()
            expected_refunded = set(
                map(str.lower, ui._cfg.api.get("expected_refunded_status", ["refunded", "cancelled"]))
            )
            actual = str(payload.get("payment_status", "")).lower()
            assert actual in expected_refunded, f"Unexpected API refund status={actual} payload={payload}"

    def test_card_payment_invalid_details_shows_error(self, ui: PaymentsUi) -> None:
        """TC_02: invalid/expired card is declined with a clear error message and no crash."""
        ui.login()

        ui.pay_with_card(ui._cfg.card_invalid)
        error_text = ui.assert_payment_error()

        assert error_text, "Expected a non-empty payment error message"

        expected_keywords = ui._cfg.web.get(
            "expected_payment_error_keywords",
            ["invalid", "expired", "declined"],
        )
        normalized = error_text.lower()
        assert any(str(k).lower() in normalized for k in expected_keywords), (
            f"Error message not informative enough. error_text={error_text!r} "
            f"expected_keywords={expected_keywords}"
        )