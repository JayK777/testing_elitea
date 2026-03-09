"""End-to-end payment pipeline tests (EP-24).

Covers:
- TC_01 (A, P0): Supported payment method happy path.
- TC_03 (A, P0): Gateway timeout/delayed response handling + safe retry.

Prerequisites (provided via environment variables):
- APP_BASE_URL: UI base URL (e.g., https://app.example.com)
- API_BASE_URL: API base URL (e.g., https://api.example.com)
- DB_DSN: PostgreSQL DSN for optional DB assertions

Test data:
- E2E_tc_automation_v4/test_data.json

Run:
  python -m pip install pytest playwright requests psycopg2-binary
  playwright install
  pytest -q
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import requests
from playwright.sync_api import Browser, Page, Playwright, expect, sync_playwright


DATA_FILE_PATH = Path(__file__).with_name("test_data.json")
ARTIFACTS_DIR = Path(os.getenv("TEST_ARTIFACTS_DIR", "test_artifacts"))


class TestConfigError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class RuntimeConfig:
    app_base_url: str
    api_base_url: str
    db_dsn: Optional[str]


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise TestConfigError(
            f"Missing required environment variable: {name}. "
            "Set it before running tests."
        )
    return value


def load_test_data() -> Dict[str, Any]:
    if not DATA_FILE_PATH.exists():
        raise TestConfigError(
            f"Missing test data file: {DATA_FILE_PATH}. "
            "Ensure it exists in the repo."
        )

    try:
        return json.loads(DATA_FILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TestConfigError(f"Invalid JSON in {DATA_FILE_PATH}: {exc}") from exc


def load_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        app_base_url=_require_env("APP_BASE_URL").rstrip("/"),
        api_base_url=_require_env("API_BASE_URL").rstrip("/"),
        db_dsn=os.getenv("DB_DSN"),
    )


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def safe_screenshot(page: Page, name: str) -> Optional[Path]:
    try:
        ensure_dir(ARTIFACTS_DIR)
        file_path = ARTIFACTS_DIR / f"{name}.png"
        page.screenshot(path=str(file_path), full_page=True)
        return file_path
    except Exception:
        return None


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


@pytest.fixture(scope="session")
def config() -> RuntimeConfig:
    return load_runtime_config()


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return load_test_data()


@pytest.fixture(scope="session")
def http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="session")
def playwright() -> Playwright:
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright: Playwright) -> Browser:
    browser = playwright.chromium.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture()
def page(browser: Browser, request: pytest.FixtureRequest) -> Page:
    context = browser.new_context()
    page = context.new_page()
    yield page

    rep = getattr(request.node, "rep_call", None)
    if rep and rep.failed:
        safe_screenshot(page, f"{request.node.name}_failed")

    context.close()


class PaymentPage:
    def __init__(self, page: Page, selectors: Dict[str, Any]):
        self._page = page
        self._s = selectors

    def goto_checkout(self, base_url: str) -> None:
        self._page.goto(f"{base_url}{self._s['checkout_path']}")
        expect(self._page).to_have_url(re.compile(r"checkout", re.IGNORECASE))

    def select_payment_method(self, method: str) -> None:
        method_selector = self._s["payment_method_selectors"][method]
        self._page.locator(method_selector).click()

    def fill_card_details(self, card: Dict[str, str]) -> None:
        self._page.fill(self._s["card_number"], card["number"])
        self._page.fill(self._s["card_expiry"], card["expiry"])
        self._page.fill(self._s["card_cvv"], card["cvv"])
        self._page.fill(self._s["card_name"], card["name"])

    def submit_payment(self) -> None:
        self._page.locator(self._s["pay_button"]).click()

    def retry_payment(self) -> None:
        retry_selector = self._s.get("retry_button")
        if retry_selector:
            self._page.locator(retry_selector).click()
            return
        self.submit_payment()

    def read_status_text(self) -> str:
        return self._page.locator(self._s["payment_status"]).inner_text().strip()

    def wait_for_status(
        self,
        expected: Tuple[str, ...],
        timeout_seconds: int,
        poll_seconds: float = 1.0,
    ) -> str:
        deadline = time.time() + timeout_seconds
        last = ""
        while time.time() < deadline:
            try:
                last = self.read_status_text()
            except Exception:
                last = ""
            if any(token.lower() in last.lower() for token in expected):
                return last
            time.sleep(poll_seconds)

        raise TimeoutError(
            f"Timed out waiting for payment status {expected}. Last observed: '{last}'"
        )

    def get_order_reference(self) -> Optional[str]:
        locator = self._page.locator(self._s["order_reference"])  # e.g., Order ID
        if locator.count() == 0:
            return None
        return locator.first.inner_text().strip()


def api_get_order_status(
    http_session: requests.Session,
    api_base_url: str,
    order_id: str,
    endpoints: Dict[str, str],
    timeout_seconds: int = 15,
) -> Dict[str, Any]:
    url = f"{api_base_url}{endpoints['order_status']}/{order_id}"
    resp = http_session.get(url, timeout=timeout_seconds)
    resp.raise_for_status()
    return resp.json()


def db_count_orders_by_payment_ref(db_dsn: str, payment_reference: str) -> Optional[int]:
    try:
        import psycopg2
    except ImportError:
        return None

    query = (
        "SELECT COUNT(1) "
        "FROM orders "
        "WHERE payment_reference = %s AND status IN ('PAID', 'CONFIRMED', 'SUCCESS')"
    )

    conn = psycopg2.connect(db_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(query, (payment_reference,))
            result = cur.fetchone()
            return int(result[0]) if result else 0
    finally:
        conn.close()


class TestPaymentFlow:
    """Automation-tagged payment flow tests from pipeline_testcase_v2.xlsx."""

    def _login_if_needed(
        self,
        page: Page,
        base_url: str,
        test_data: Dict[str, Any],
    ) -> None:
        login = test_data.get("login")
        credentials = test_data.get("credentials")
        if not login or not credentials:
            return

        page.goto(f"{base_url}{login['path']}")
        page.fill(login["username"], credentials["username"])
        page.fill(login["password"], credentials["password"])
        page.locator(login["submit"]).click()

        post_login = login.get("post_login")
        if post_login:
            expect(page.locator(post_login)).to_be_visible(timeout=15_000)

    def test_tc01_happy_path_supported_payment(
        self,
        page: Page,
        config: RuntimeConfig,
        test_data: Dict[str, Any],
        http_session: requests.Session,
    ):
        selectors = test_data["selectors"]
        self._login_if_needed(page, config.app_base_url, test_data)

        payment_page = PaymentPage(page, selectors)
        payment_page.goto_checkout(config.app_base_url)

        method = test_data["tc01"]["method"]
        payment_page.select_payment_method(method)

        if method == "card":
            payment_page.fill_card_details(test_data["tc01"]["card"])

        payment_page.submit_payment()

        final_status = payment_page.wait_for_status(
            expected=("success", "paid", "confirmed"),
            timeout_seconds=test_data["tc01"]["ui_timeout_seconds"],
        )
        assert "success" in final_status.lower() or "paid" in final_status.lower()

        order_id = payment_page.get_order_reference()
        if order_id and test_data.get("api", {}).get("endpoints", {}).get("order_status"):
            status_payload = api_get_order_status(
                http_session=http_session,
                api_base_url=config.api_base_url,
                order_id=order_id,
                endpoints=test_data["api"]["endpoints"],
            )
            assert status_payload.get("status") in {"SUCCESS", "PAID", "CONFIRMED"}

    def test_tc03_timeout_handling_and_safe_retry(
        self,
        page: Page,
        config: RuntimeConfig,
        test_data: Dict[str, Any],
        http_session: requests.Session,
    ):
        selectors = test_data["selectors"]
        self._login_if_needed(page, config.app_base_url, test_data)

        payment_page = PaymentPage(page, selectors)
        payment_page.goto_checkout(config.app_base_url)

        method = test_data["tc03"]["method"]
        payment_page.select_payment_method(method)

        if method == "card":
            payment_page.fill_card_details(test_data["tc03"]["card"])

        payment_page.submit_payment()

        payment_page.wait_for_status(
            expected=("pending", "processing"),
            timeout_seconds=test_data["tc03"]["pending_timeout_seconds"],
        )

        safe_state = payment_page.wait_for_status(
            expected=("pending", "failed", "timeout"),
            timeout_seconds=test_data["tc03"]["overall_timeout_seconds"],
        )
        assert any(token in safe_state.lower() for token in ("pending", "fail", "timeout"))

        payment_reference = payment_page.get_order_reference() or ""

        payment_page.retry_payment()

        final_status = payment_page.wait_for_status(
            expected=("success", "paid", "confirmed", "failed"),
            timeout_seconds=test_data["tc03"]["retry_timeout_seconds"],
        )

        if config.db_dsn and payment_reference:
            count = db_count_orders_by_payment_ref(config.db_dsn, payment_reference)
            if count is not None:
                assert count == 1, "Duplicate charge/order detected for payment reference"

        lowered = final_status.lower()
        assert any(token in lowered for token in ("success", "paid", "fail"))
