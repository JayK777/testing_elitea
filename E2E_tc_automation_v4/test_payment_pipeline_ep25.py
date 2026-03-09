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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--headed", action="store_true", help="Run Playwright in headed mode")


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return _load_test_data()


@pytest.fixture(scope="session")
def browser(request: pytest.FixtureRequest) -> Generator[Browser, None, None]:
    headed = bool(request.config.getoption("--headed"))

    with sync_playwright() as pw:
        browser_instance = pw.chromium.launch(headless=not headed)
        yield browser_instance
        browser_instance.close()


@pytest.fixture()
def context(browser: Browser) -> Generator[BrowserContext, None, None]:
    context_instance = browser.new_context()
    yield context_instance
    context_instance.close()


@pytest.fixture()
def page(
    request: pytest.FixtureRequest,
    context: BrowserContext,
    test_data: Dict[str, Any],
) -> Generator[Page, None, None]:
    page_instance = context.new_page()
    page_instance.set_default_timeout(int(_get(test_data, "ui.timeout_ms", DEFAULT_TIMEOUT_MS)))

    # Make the page available for failure screenshots.
    request.node._page = page_instance  # type: ignore[attr-defined]

    yield page_instance
    page_instance.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, None, None]:
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or report.passed:
        return

    page_instance = getattr(item, "_page", None)
    if page_instance is None:
        return

    try:
        artifacts_dir = Path(os.getenv("PYTEST_ARTIFACTS", "pytest_artifacts"))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = artifacts_dir / f"{item.name}.png"
        page_instance.screenshot(path=str(screenshot_path), full_page=True)
        report.sections.append(("screenshot", str(screenshot_path)))
    except Exception:
        # Never mask the real test failure.
        return


class PaymentApp:
    """Minimal, data-driven UI actions for payment flows."""

    def __init__(self, page: Page, data: Dict[str, Any]):
        self.page = page
        self.data = data
        self.base_url = str(_require(data, "ui.base_url")).rstrip("/")
        self.selectors = _require(data, "ui.selectors")
        if not isinstance(self.selectors, dict):
            raise TestDataError("ui.selectors must be a JSON object")

    def _sel(self, key: str) -> str:
        selector = self.selectors.get(key, "")
        if not selector:
            raise TestDataError(f"Missing selector key: ui.selectors.{key}")
        return str(selector)

    def goto(self, path: str) -> None:
        url = f"{self.base_url}{path}"
        self.page.goto(url, wait_until="domcontentloaded")

    def login(self) -> None:
        self.goto(str(_require(self.data, "ui.paths.login")))

        username = _env("APP_USERNAME", str(_require(self.data, "credentials.username")))
        password = _env("APP_PASSWORD", str(_require(self.data, "credentials.password")))

        self.page.locator(self._sel("login_username")).fill(username)
        self.page.locator(self._sel("login_password")).fill(password)
        self.page.locator(self._sel("login_submit")).click()

        logged_in_indicator = self.selectors.get("logged_in_indicator")
        if logged_in_indicator:
            expect(self.page.locator(str(logged_in_indicator))).to_be_visible()

    def add_item_to_cart(self) -> None:
        self.goto(str(_require(self.data, "ui.paths.catalog")))
        self.page.locator(self._sel("add_to_cart")).first.click()

    def proceed_to_checkout(self) -> None:
        self.page.locator(self._sel("cart_open")).click()
        self.page.locator(self._sel("checkout_start")).click()

    def select_payment_method(self, selector_key: str) -> None:
        self.page.locator(self._sel(selector_key)).click()

    def pay_by_credit_card(self) -> None:
        self.select_payment_method("payment_method_credit_card")

        card = _require(self.data, "payment.credit_card")
        if not isinstance(card, dict):
            raise TestDataError("payment.credit_card must be a JSON object")

        self.page.locator(self._sel("card_number")).fill(str(_require(card, "number")))
        self.page.locator(self._sel("card_expiry")).fill(str(_require(card, "expiry")))
        self.page.locator(self._sel("card_cvv")).fill(str(_require(card, "cvv")))
        if self.selectors.get("card_name"):
            self.page.locator(self._sel("card_name")).fill(str(_get(card, "name", "Test User")))

        self.page.locator(self._sel("pay_submit")).click()

    def pay_by_wallet_or_netbanking(self) -> None:
        self.select_payment_method("payment_method_wallet")
        self.page.locator(self._sel("pay_submit")).click()

    def wait_for_payment_status(
        self, expected: Tuple[str, ...], timeout_ms: int = DEFAULT_TIMEOUT_MS
    ) -> str:
        status_locator = self.page.locator(self._sel("payment_status"))
        deadline = time.time() + (timeout_ms / 1000.0)

        last_seen = ""
        while time.time() < deadline:
            try:
                last_seen = status_locator.inner_text().strip()
            except Exception:
                last_seen = ""

            for status in expected:
                if status.lower() in last_seen.lower():
                    return status

            time.sleep(1)

        raise AssertionError(
            f"Payment status not reached. Expected one of {expected}, last seen='{last_seen}'"
        )

    def get_order_id(self) -> Optional[str]:
        order_id_sel = self.selectors.get("order_id")
        candidates = []

        if order_id_sel:
            try:
                candidates.append(self.page.locator(str(order_id_sel)).inner_text())
            except Exception:
                pass

        candidates.append(self.page.url)

        for text in candidates:
            order_id = _extract_order_id(text)
            if order_id:
                return order_id

        return None

    def cancel_order(self) -> None:
        self.page.locator(self._sel("order_cancel")).click()
        confirm = self.selectors.get("order_cancel_confirm")
        if confirm:
            self.page.locator(str(confirm)).click()


class TestEp25PaymentPipeline:
    """Automation-tagged EP-25 scenarios from the provided workbook."""

    def _optional_api_assert(self, data: Dict[str, Any], order_id: str) -> None:
        api = _api_config(data)
        if api is None:
            return

        status_json = _fetch_order_status(api, order_id)
        expected_key = _get(data, "api.expected_status_key", "status")
        expected_values = tuple(_get(data, "api.expected_paid_status_values", ["SUCCESS", "PAID"]))
        actual = str(status_json.get(expected_key, ""))
        assert any(v.lower() in actual.lower() for v in expected_values), (
            f"Unexpected API order status. key={expected_key} value={actual} json={status_json}"
        )

    def _optional_db_no_duplicates(self, data: Dict[str, Any], order_id: str) -> None:
        db = _db_config(data)
        query_template = _get(data, "db.duplicate_payment_count_query", "").strip()
        if db is None or not query_template:
            return

        query = query_template.format(order_id=order_id)

        with _db_conn(db) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                row = cur.fetchone()

        if not row:
            return

        count = int(row[0])
        assert count <= 1, f"Duplicate payments detected for order_id={order_id}. count={count}"

    def test_tc01_credit_card_success(self, page: Page, test_data: Dict[str, Any]) -> None:
        app = PaymentApp(page, test_data)
        app.login()
        app.add_item_to_cart()
        app.proceed_to_checkout()
        app.pay_by_credit_card()

        app.wait_for_payment_status(("Success", "SUCCESS", "Paid", "PAID"), timeout_ms=DEFAULT_TIMEOUT_MS)

        order_id = app.get_order_id()
        assert order_id, "Order id could not be determined from UI"

        self._optional_api_assert(test_data, order_id)
        self._optional_db_no_duplicates(test_data, order_id)

    def test_tc03_pending_resolves_no_duplicate(self, page: Page, test_data: Dict[str, Any]) -> None:
        app = PaymentApp(page, test_data)
        app.login()
        app.add_item_to_cart()
        app.proceed_to_checkout()
        app.pay_by_wallet_or_netbanking()

        possible = ("Pending", "PENDING", "Success", "SUCCESS", "Failure", "FAILURE")
        observed = app.wait_for_payment_status(
            possible, timeout_ms=int(_get(test_data, "ui.pending_timeout_ms", 90_000))
        )

        if observed.lower() == "pending":
            app.wait_for_payment_status(
                ("Success", "SUCCESS", "Failure", "FAILURE"),
                timeout_ms=int(_get(test_data, "ui.final_timeout_ms", 120_000)),
            )

        order_id = app.get_order_id()
        if order_id:
            self._optional_db_no_duplicates(test_data, order_id)

    def test_tc05_cancellation_updates_payment_status(self, page: Page, test_data: Dict[str, Any]) -> None:
        app = PaymentApp(page, test_data)

        # Case A: Paid order then cancellation -> Refunded/Cancelled
        app.login()
        app.add_item_to_cart()
        app.proceed_to_checkout()
        app.pay_by_credit_card()
        app.wait_for_payment_status(("Success", "SUCCESS", "Paid", "PAID"), timeout_ms=DEFAULT_TIMEOUT_MS)
        app.cancel_order()

        cancelled_statuses = tuple(_get(test_data, "ui.cancelled_status_texts", ["Refunded", "Cancelled"]))
        app.wait_for_payment_status(cancelled_statuses, timeout_ms=DEFAULT_TIMEOUT_MS)

        # Case B: Failed/unpaid order cancellation should not show Refunded.
        failure_flow = _get(test_data, "payment.failure", None)
        if not isinstance(failure_flow, dict):
            pytest.skip("No payment.failure config provided to validate failed/unpaid cancellation behavior")

        app.add_item_to_cart()
        app.proceed_to_checkout()

        method_key = str(_require(failure_flow, "method_selector_key"))
        app.select_payment_method(method_key)

        fields = _get(failure_flow, "fields", {})
        if not isinstance(fields, dict):
            raise TestDataError("payment.failure.fields must be a JSON object")

        for field_selector_key, value in fields.items():
            app.page.locator(app._sel(str(field_selector_key))).fill(str(value))

        app.page.locator(app._sel("pay_submit")).click()
        app.wait_for_payment_status(("Failure", "FAILURE"), timeout_ms=DEFAULT_TIMEOUT_MS)

        app.cancel_order()

        status_text = page.locator(app._sel("payment_status")).inner_text().strip().lower()
        assert "refund" not in status_text, (
            f"Failed/unpaid cancellation must not show refunded state. actual_status='{status_text}'"
        )
