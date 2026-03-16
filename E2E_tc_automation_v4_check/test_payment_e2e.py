"""End-to-end payment flow tests.

Covers automated scenarios from test_v1.xlsx:
- TC_01: Successful card payment with real-time status updates
- TC_02: Card payment failure with retry/change method
- TC_04: Slow gateway response; prevent duplicate orders/charges
- TC_06: Payable amount integrity (UI vs gateway request)

Tech stack: Python + Playwright (+ pytest).

Execution:
    pytest -q E2E_tc_automation_v4_check/test_payment_e2e.py \
        --base-url=https://example.test \
        --headed

Config:
    Uses E2E_tc_automation_v4_check/test_data.json.

Note:
    Selectors and endpoints are application-specific; update test_data.json accordingly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from playwright.sync_api import Browser, Page, Playwright, TimeoutError, expect, sync_playwright

LOGGER = logging.getLogger(__name__)


# ============================
# Configuration
# ============================


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    timeouts: Dict[str, int]
    credentials: Dict[str, str]
    selectors: Dict[str, str]
    card_success: Dict[str, str]
    card_decline: Dict[str, str]
    expected_currency: str
    network: Dict[str, Any]
    api: Dict[str, Any]
    db: Dict[str, Any]


def _load_config() -> AppConfig:
    config_path = Path(__file__).with_name("test_data.json")
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    base_url = os.getenv("BASE_URL", raw.get("base_url", "")).strip()
    if not base_url:
        raise ValueError("BASE_URL is not set and base_url missing in test_data.json")

    return AppConfig(
        base_url=base_url,
        timeouts=raw["timeouts"],
        credentials=raw["credentials"],
        selectors=raw["selectors"],
        card_success=raw["cards"]["success"],
        card_decline=raw["cards"]["decline"],
        expected_currency=raw["expected_currency"],
        network=raw.get("network", {}),
        api=raw.get("api", {}),
        db=raw.get("db", {}),
    )


# ============================
# Pytest plumbing
# ============================


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--base-url", action="store", default=None)
    parser.addoption("--headed", action="store_true", default=False)


@pytest.fixture(scope="session")
def config(pytestconfig: pytest.Config) -> AppConfig:
    cfg = _load_config()
    cli_base_url = pytestconfig.getoption("base_url")
    if cli_base_url:
        return AppConfig(**{**cfg.__dict__, "base_url": cli_base_url})
    return cfg


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright, pytestconfig: pytest.Config) -> Browser:
    headed = bool(pytestconfig.getoption("headed"))
    browser = playwright_instance.chromium.launch(headless=not headed)
    yield browser
    browser.close()


@pytest.fixture()
def page(browser: Browser, config: AppConfig, request: pytest.FixtureRequest) -> Page:
    context = browser.new_context(base_url=config.base_url)
    context.set_default_timeout(config.timeouts["default_ms"])
    context.set_default_navigation_timeout(config.timeouts["navigation_ms"])

    page = context.new_page()

    yield page

    if request.node.rep_call.failed:  # type: ignore[attr-defined]
        _capture_failure_artifacts(page, request.node.name)

    context.close()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def _capture_failure_artifacts(page: Page, test_name: str) -> None:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", test_name)[:80]
    ts = int(time.time())
    screenshot_path = Path(__file__).with_name(f"failure_{safe}_{ts}.png")

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        LOGGER.error("Saved failure screenshot: %s", screenshot_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Failed to capture screenshot: %s", exc)


# ============================
# Helpers (in-file, per constraints)
# ============================


class PaymentFlowError(RuntimeError):
    """Raised when payment flow fails in an unexpected way."""


def _to_money(value: str) -> Decimal:
    cleaned = re.sub(r"[^0-9.,-]", "", value).replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Unable to parse money value: {value!r}") from exc


def _login(page: Page, config: AppConfig) -> None:
    page.goto(config.selectors["login_url"], wait_until="domcontentloaded")

    page.locator(config.selectors["login_username"]).fill(config.credentials["username"])
    page.locator(config.selectors["login_password"]).fill(config.credentials["password"])
    page.locator(config.selectors["login_submit"]).click()

    expect(page.locator(config.selectors["home_marker"])).to_be_visible()


def _add_items_and_go_to_checkout(page: Page, config: AppConfig) -> None:
    page.goto(config.selectors["menu_url"], wait_until="domcontentloaded")

    add_button = page.locator(config.selectors["add_item_button"]).first
    add_button.click()

    page.locator(config.selectors["cart_button"]).click()
    expect(page.locator(config.selectors["checkout_button"])).to_be_visible()
    page.locator(config.selectors["checkout_button"]).click()

    expect(page.locator(config.selectors["payment_screen_marker"])).to_be_visible()


def _select_address_if_required(page: Page, config: AppConfig) -> None:
    selector = config.selectors.get("address_continue_button")
    if not selector:
        return

    locator = page.locator(selector)
    if locator.count() > 0 and locator.first.is_visible():
        locator.first.click()


def _select_card_method(page: Page, config: AppConfig) -> None:
    page.locator(config.selectors["payment_method_card"]).click()
    expect(page.locator(config.selectors["card_form_marker"])).to_be_visible()


def _fill_card_details(page: Page, config: AppConfig, card: Dict[str, str]) -> None:
    page.locator(config.selectors["card_number"]).fill(card["number"])
    page.locator(config.selectors["card_expiry"]).fill(card["expiry"])
    page.locator(config.selectors["card_cvv"]).fill(card["cvv"])
    name_selector = config.selectors.get("card_name")
    if name_selector and card.get("name"):
        page.locator(name_selector).fill(card["name"])


def _maybe_complete_3ds(page: Page, config: AppConfig) -> None:
    """Handle OTP/3DS if an iframe or redirect is used.

    This is intentionally generic; update selectors in test_data.json.
    """

    otp_selector = config.selectors.get("otp_input")
    otp_submit = config.selectors.get("otp_submit")
    otp_value = config.card_success.get("otp", "")

    if not (otp_selector and otp_submit and otp_value):
        return

    try:
        otp = page.locator(otp_selector)
        otp.wait_for(state="visible", timeout=config.timeouts["otp_ms"])
        otp.fill(otp_value)
        page.locator(otp_submit).click()
    except TimeoutError:
        return


def _click_pay(page: Page, config: AppConfig) -> None:
    pay = page.locator(config.selectors["pay_button"])
    expect(pay).to_be_enabled()
    pay.click()


def _wait_for_payment_final_status(page: Page, config: AppConfig) -> str:
    status = page.locator(config.selectors["payment_status_label"])
    status.wait_for(state="visible")

    final_states = {"success", "failure"}
    start = time.time()

    while time.time() - start < (config.timeouts["payment_final_ms"] / 1000):
        text = (status.text_content() or "").strip().lower()
        if any(state in text for state in final_states):
            return text
        time.sleep(0.3)

    raise PaymentFlowError("Timed out waiting for final payment status")


def _extract_order_id(page: Page, config: AppConfig) -> Optional[str]:
    selector = config.selectors.get("order_id_label")
    if not selector:
        return None

    label = page.locator(selector)
    if label.count() == 0:
        return None

    text = (label.first.text_content() or "").strip()
    match = re.search(r"([A-Z0-9-]{6,})", text)
    return match.group(1) if match else text or None


def _ensure_single_order_confirmation(page: Page, config: AppConfig) -> None:
    selector = config.selectors.get("order_confirmation_marker")
    if not selector:
        return

    locator = page.locator(selector)
    expect(locator).to_be_visible()

    # Best-effort duplication check: confirmation element should not appear multiple times.
    count = locator.count()
    if count > 1:
        raise AssertionError(f"Order confirmation duplicated on UI (count={count})")


def _get_ui_payable_amount(page: Page, config: AppConfig) -> Decimal:
    label = page.locator(config.selectors["payable_amount_label"])
    expect(label).to_be_visible()
    return _to_money(label.text_content() or "")


def _setup_payment_request_capture(page: Page, config: AppConfig) -> Dict[str, Optional[str]]:
    """Capture payment request fields for validations (best-effort).

    Configure in test_data.json:
      network.payment_request_url_pattern: Playwright URL match pattern/regex
      network.amount_field / currency_field / order_id_field: JSON field names

    Returns a dict that will be populated once the matching request is sent.
    """

    target_pattern = config.network.get("payment_request_url_pattern")
    captured: Dict[str, Optional[str]] = {"amount": None, "currency": None, "order_id": None}

    if not target_pattern:
        return captured

    amount_field = config.network.get("amount_field", "amount")
    currency_field = config.network.get("currency_field", "currency")
    order_id_field = config.network.get("order_id_field", "orderId")

    def _handler(route, request):  # type: ignore[no-untyped-def]
        try:
            post = request.post_data_json
        except Exception:  # noqa: BLE001
            post = None

        if isinstance(post, dict):
            if amount_field in post:
                captured["amount"] = str(post.get(amount_field))
            if currency_field in post:
                captured["currency"] = str(post.get(currency_field))
            if order_id_field in post:
                captured["order_id"] = str(post.get(order_id_field))

        route.continue_()

    page.route(target_pattern, _handler)
    return captured


def _get_receipt_amount(page: Page, config: AppConfig) -> Optional[Decimal]:
    selector = config.selectors.get("receipt_total_label")
    if not selector:
        return None

    total = page.locator(selector)
    if total.count() == 0:
        return None

    expect(total.first).to_be_visible()
    return _to_money(total.first.text_content() or "")


# ============================
# Tests
# ============================


class TestCardPaymentFlow:
    """TC_01, TC_02, TC_04."""

    def test_tc01_successful_card_payment_realtime_status(self, page: Page, config: AppConfig) -> None:
        _login(page, config)
        _add_items_and_go_to_checkout(page, config)
        _select_address_if_required(page, config)

        _select_card_method(page, config)
        _fill_card_details(page, config, config.card_success)

        _click_pay(page, config)
        _maybe_complete_3ds(page, config)

        final_status = _wait_for_payment_final_status(page, config)
        assert "success" in final_status, f"Expected success status, got: {final_status!r}"

        _ensure_single_order_confirmation(page, config)

    def test_tc02_gateway_failure_allows_retry_or_change_method(self, page: Page, config: AppConfig) -> None:
        _login(page, config)
        _add_items_and_go_to_checkout(page, config)
        _select_address_if_required(page, config)

        _select_card_method(page, config)
        _fill_card_details(page, config, config.card_decline)

        _click_pay(page, config)
        final_status = _wait_for_payment_final_status(page, config)
        assert "failure" in final_status, f"Expected failure status, got: {final_status!r}"

        failure_msg = page.locator(config.selectors["payment_failure_message"])
        expect(failure_msg).to_be_visible()

        retry = page.locator(config.selectors["payment_retry_button"])
        change = page.locator(config.selectors["payment_change_method_button"])
        expect(retry).to_be_visible()
        expect(change).to_be_visible()

    def test_tc04_slow_gateway_prevents_duplicates(self, page: Page, config: AppConfig) -> None:
        _login(page, config)
        _add_items_and_go_to_checkout(page, config)
        _select_address_if_required(page, config)

        delay_ms = int(config.network.get("simulate_delay_ms", 0))
        delay_pattern = config.network.get("payment_request_url_pattern")

        if delay_ms > 0 and delay_pattern:

            def _delay(route, request):  # type: ignore[no-untyped-def]
                time.sleep(delay_ms / 1000)
                route.continue_()

            page.route(delay_pattern, _delay)

        _select_card_method(page, config)
        _fill_card_details(page, config, config.card_success)

        _click_pay(page, config)

        # Attempt to duplicate by clicking Pay again.
        pay = page.locator(config.selectors["pay_button"])
        try:
            pay.click(timeout=config.timeouts["duplicate_click_timeout_ms"])
        except TimeoutError:
            pass
        except Exception:  # noqa: BLE001
            pass

        processing = page.locator(config.selectors["payment_status_label"])
        expect(processing).to_be_visible()

        _maybe_complete_3ds(page, config)
        final_status = _wait_for_payment_final_status(page, config)
        assert any(s in final_status for s in ("success", "failure"))

        if "success" in final_status:
            _ensure_single_order_confirmation(page, config)


class TestAmountIntegrity:
    """TC_06."""

    def test_tc06_ui_amount_matches_gateway_request_and_receipt(self, page: Page, config: AppConfig) -> None:
        _login(page, config)
        _add_items_and_go_to_checkout(page, config)
        _select_address_if_required(page, config)

        ui_amount = _get_ui_payable_amount(page, config)

        # Best-effort interception. If not configured, the test will validate receipt-only.
        _intercept_amount_from_gateway_request(page, config)

        _select_card_method(page, config)
        _fill_card_details(page, config, config.card_success)

        _click_pay