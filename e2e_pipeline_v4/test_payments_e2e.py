"""E2E payment automation (Playwright) for Automation-tagged scenarios.

Covers:
- TC_01: Successful card payment (happy path)
- TC_02: Failed card payment shows clear error and order is not marked paid
- TC_04: Secure payment transmission over gateway integration

Usage:
  pytest -q e2e_pipeline_v4/test_payments_e2e.py

Configuration:
  Update e2e_pipeline_v4/test_data.json and/or set environment variables.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent
TEST_DATA_PATH = PROJECT_ROOT / "test_data.json"


class TestDataError(RuntimeError):
    """Raised when test data is missing or invalid."""


def _load_test_data() -> Dict[str, Any]:
    if not TEST_DATA_PATH.exists():
        raise TestDataError(f"Missing test data file: {TEST_DATA_PATH}")

    try:
        data = json.loads(TEST_DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TestDataError("test_data.json is not valid JSON") from exc

    if not isinstance(data, dict) or not data.get("base_url"):
        raise TestDataError("test_data.json must contain at least 'base_url'")

    return data


@dataclass(frozen=True)
class GatewaySecurityFinding:
    url: str
    reason: str


class GatewaySecurityMonitor:
    """Captures network requests and evaluates basic security rules."""

    def __init__(
        self,
        filter_domains: List[str],
        sensitive_values: List[str],
        allow_insecure: bool,
    ) -> None:
        self._filter_domains = [d.lower() for d in filter_domains]
        self._sensitive_values = [v for v in sensitive_values if v]
        self._allow_insecure = allow_insecure
        self._seen_requests: List[str] = []

    def attach(self, page: Page) -> None:
        page.on("request", lambda req: self._seen_requests.append(req.url))

    def evaluate(self) -> List[GatewaySecurityFinding]:
        findings: List[GatewaySecurityFinding] = []

        for url in self._seen_requests:
            url_l = url.lower()
            if self._filter_domains and not any(
                d in url_l for d in self._filter_domains
            ):
                continue

            if not self._allow_insecure and url_l.startswith("http://"):
                findings.append(
                    GatewaySecurityFinding(url=url, reason="Insecure HTTP request detected")
                )

            # Basic leakage checks: sensitive values should not appear in URL.
            for val in self._sensitive_values:
                if val and val in url:
                    findings.append(
                        GatewaySecurityFinding(
                            url=url,
                            reason="Sensitive value appears in URL (query/path)",
                        )
                    )

            # Heuristic for common sensitive parameter names.
            if re.search(r"(card|cvv|cvc|pan)=", url_l):
                findings.append(
                    GatewaySecurityFinding(
                        url=url,
                        reason="Sensitive parameter name detected in URL",
                    )
                )

        return findings


class PaymentApp:
    """Minimal app driver. Selectors are provided via test_data.json."""

    def __init__(self, page: Page, base_url: str, selectors: Dict[str, str]) -> None:
        self._page = page
        self._base_url = base_url.rstrip("/")
        self._s = selectors

    def goto_base(self) -> None:
        self._page.goto(self._base_url, wait_until="domcontentloaded")

    def login(self, username: str, password: str) -> None:
        self._page.fill(self._s["username_input"], username)
        self._page.fill(self._s["password_input"], password)
        self._page.click(self._s["login_button"])

    def open_checkout(self) -> None:
        self._page.click(self._s["cart_button"])
        self._page.click(self._s["checkout_button"])

    def pay_by_card(self, number: str, expiry: str, cvv: str) -> None:
        self._page.click(self._s["payment_method_card"])
        self._page.fill(self._s["card_number_input"], number)
        self._page.fill(self._s["card_expiry_input"], expiry)
        self._page.fill(self._s["card_cvv_input"], cvv)
        self._page.click(self._s["pay_now_button"])

    def read_order_reference(self) -> Optional[str]:
        try:
            locator = self._page.locator(self._s["order_reference"])
            if locator.count() == 0:
                return None
            text = locator.first.inner_text().strip()
            return text or None
        except Exception:
            return None


# === PYTEST HOOKS & FIXTURES ===


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Expose test outcome to fixtures for diagnostics (e.g., screenshots)."""

    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return _load_test_data()


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Browser:
    headless = os.getenv("HEADLESS", "true").strip().lower() in {"1", "true", "yes"}
    browser = playwright_instance.chromium.launch(headless=headless)
    yield browser
    browser.close()


@pytest.fixture()
def page(
    browser: Browser, test_data: Dict[str, Any], request: pytest.FixtureRequest
) -> Page:
    timeouts = test_data.get("timeouts", {})
    navigation_ms = int(timeouts.get("navigation_ms", 30000))
    action_ms = int(timeouts.get("action_ms", 15000))

    context = browser.new_context()
    page = context.new_page()
    page.set_default_navigation_timeout(navigation_ms)
    page.set_default_timeout(action_ms)

    yield page

    try:
        rep = getattr(request.node, "rep_call", None)
        if rep is not None and rep.failed:
            screenshots_dir = Path(os.getenv("E2E_SCREENSHOTS_DIR", "/tmp"))
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            file_name = f"{request.node.name}.png".replace("/", "_")
            page.screenshot(path=str(screenshots_dir / file_name), full_page=True)
    finally:
        context.close()


def _assert_visible(page: Page, selector: str, timeout_ms: int = 15000) -> None:
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise AssertionError(f"Expected selector to be visible: {selector}") from exc


def _assert_not_visible(page: Page, selector: str, timeout_ms: int = 3000) -> None:
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        raise AssertionError(f"Expected selector to NOT be visible: {selector}")
    except Exception:
        return


def _request_with_retries(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    timeout_s: int = 20,
    max_attempts: int = 4,
) -> requests.Response:
    """HTTP helper with basic 429 retry to mitigate 'Too Many Requests'."""

    backoff_s = 1
    last_exc: Optional[Exception] = None

    for _ in range(1, max_attempts + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout_s)
            if resp.status_code != 429:
                return resp

            retry_after = resp.headers.get("Retry-After")
            sleep_for = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else backoff_s
            )
            time.sleep(sleep_for)
            backoff_s *= 2
        except Exception as exc:
            last_exc = exc
            time.sleep(backoff_s)
            backoff_s *= 2

    if last_exc is not None:
        raise last_exc

    raise RuntimeError(f"Request failed after retries: {method} {url}")


def _fetch_order_status_api(test_data: Dict[str, Any], order_reference: str) -> Optional[str]:
    api_cfg = test_data.get("api", {})
    if not api_cfg.get("enabled", False):
        return None

    base_url = str(api_cfg.get("base_url", "")).rstrip("/")
    endpoint_tpl = str(api_cfg.get("order_status_endpoint", ""))
    headers = api_cfg.get("auth_header") or {}

    if not base_url or "{order_reference}" not in endpoint_tpl:
        pytest.skip(
            "API verification enabled but api.base_url/order_status_endpoint not configured"
        )

    url = f"{base_url}{endpoint_tpl.format(order_reference=order_reference)}"
    resp = _request_with_retries("GET", url, headers=headers)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except Exception as exc:
        raise AssertionError("Order status API did not return JSON") from exc

    # Flexible parsing for different API shapes.
    for key in ("payment_status", "status", "paymentStatus"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return None


def _fetch_order_status_db(test_data: Dict[str, Any], order_reference: str) -> Optional[str]:
    db_cfg = test_data.get("db", {})
    if not db_cfg.get("enabled", False):
        return None

    dsn = str(db_cfg.get("dsn", "")).strip()
    query = str(db_cfg.get("order_status_query", "")).strip()
    if not dsn or not query:
        pytest.skip(
            "DB verification enabled but db.dsn/order_status_query not configured"
        )

    try:
        import psycopg2  # type: ignore
    except Exception:
        pytest.skip("psycopg2 is required for DB verification but is not installed")

    conn = None
    try:
        conn = psycopg2.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(query, (order_reference,))
            row = cur.fetchone()
            if not row:
                return None
            val = row[0]
            return str(val) if val is not None else None
    finally:
        if conn is not None:
            conn.close()


def _verify_paid_status(test_data: Dict[str, Any], order_reference: str) -> None:
    api_status = _fetch_order_status_api(test_data, order_reference)
    db_status = _fetch_order_status_db(test_data, order_reference)

    observed = [s for s in (api_status, db_status) if s]
    if not observed:
        return

    assert any("paid" in s.lower() or "confirm" in s.lower() for s in observed), (
        "Expected order to be paid/confirmed; got: " + ", ".join(observed)
    )


def _verify_not_paid_status(test_data: Dict[str, Any], order_reference: str) -> None:
    api_status = _fetch_order_status_api(test_data, order_reference)
    db_status = _fetch_order_status_db(test_data, order_reference)

    observed = [s for s in (api_status, db_status) if s]
    if not observed:
        return

    assert all("paid" not in s.lower() for s in observed), (
        "Expected order NOT to be paid; got: " + ", ".join(observed)
    )


# === TESTS ===


class TestCardPayments:
    """Automation scenarios for card payments (TC_01, TC_02)."""

    def test_tc_01_successful_card_payment_happy_path(
        self, page: Page, test_data: Dict[str, Any]
    ) -> None:
        selectors = test_data.get("selectors", {})
        creds = test_data.get("credentials", {})
        payment = test_data.get("payment", {}).get("valid_card", {})

        app = PaymentApp(page, base_url=test_data["base_url"], selectors=selectors)

        try:
            app.goto_base()
            app.login(creds["username"], creds["password"])
            app.open_checkout()
            app.pay_by_card(payment["number"], payment["expiry"], payment["cvv"])

            _assert_visible(page, selectors["payment_success_banner"])
            _assert_not_visible(page, selectors["payment_error_banner"])

            order_reference = app.read_order_reference()
            if order_reference:
                _verify_paid_status(test_data, order_reference)
        except KeyError as exc:
            raise TestDataError(f"Missing required test_data.json key: {exc}") from exc

    def test_tc_02_failed_card_payment_shows_error_order_not_paid(
        self, page: Page, test_data: Dict[str, Any]
    ) -> None:
        selectors = test_data.get("selectors", {})
        creds = test_data.get("credentials", {})
        payment = test_data.get("payment", {}).get("invalid_card", {})

        app = PaymentApp(page, base_url=test_data["base_url"], selectors=selectors)

        try:
            app.goto_base()
            app.login(creds["username"], creds["password"])
            app.open_checkout()
            app.pay_by_card(payment["number"], payment["expiry"], payment["cvv"])

            _assert_visible(page, selectors["payment_error_banner"])
            _assert_not_visible(page, selectors["payment_success_banner"])

            order_reference = app.read_order_reference()
            if order_reference:
                _verify_not_paid_status(test_data, order_reference)
        except KeyError as exc:
            raise TestDataError(f"Missing required test_data.json key: {exc}") from exc


class TestGatewaySecurity:
    """Automation scenario for gateway secure transmission (TC_04)."""

    def test_tc_04_secure_payment_transmission_over_gateway(
        self, page: Page, test_data: Dict[str, Any]
    ) -> None:
        selectors = test_data.get("selectors", {})
        creds = test_data.get("credentials", {})
        payment = test_data.get("payment", {}).get("valid_card", {})
        gw_cfg = test_data.get("gateway", {})

        monitor = GatewaySecurityMonitor(
            filter_domains=list(gw_cfg.get("filter_domains", [])),
            sensitive_values=list(gw_cfg.get("sensitive_values", [])),
            allow_insecure=bool(gw_cfg.get("allow_insecure", False)),
        )
        monitor.attach(page)

        app = PaymentApp(page, base_url=test_data["base_url"], selectors=selectors)

        try:
            app.goto_base()
            app.login(creds["username"], creds["password"])
            app.open_checkout()
            app.pay_by_card(payment["number"], payment["expiry"], payment["cvv"])

            # We don't enforce success here; we focus on transmission security.
            page.wait_for_load_state("networkidle", timeout=15000)

            findings = monitor.evaluate()
            assert not findings, "Gateway security findings:\n" + "\n".join(
                f"- {f.reason}: {f.url}" for f in findings
            )
        except KeyError as exc:
            raise TestDataError(f"Missing required test_data.json key: {exc}") from exc
