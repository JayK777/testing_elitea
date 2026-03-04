"""Automation scenarios (tagged A) derived from pipeline_testcase.xlsx.

Coverage in this module:
- TC_06 Digital wallet payment fails due to insufficient wallet balance
- TC_08 Payment gateway timeout while processing payment

Notes:
- Timeout simulation is environment-dependent. If `web.network_simulation.timeout_url_pattern`
  is not configured, the timeout test will be skipped.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from playwright.sync_api import Page, Route, sync_playwright


LOGGER = logging.getLogger(__name__)


def _load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    return json.loads(data_path.read_text(encoding="utf-8"))


def _require(data: Dict[str, Any], dotted_key: str) -> Any:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"Missing required config key: {dotted_key}")
        cur = cur[part]
    return cur


def _maybe_get(data: Dict[str, Any], dotted_key: str, default: Any = None) -> Any:
    try:
        return _require(data, dotted_key)
    except KeyError:
        return default


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    return _load_test_data()


@pytest.fixture()
def page(test_data: Dict[str, Any]) -> Page:
    web = _require(test_data, "web")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(int(_maybe_get(web, "timeouts_ms.action", 10000)))
        yield page
        context.close()
        browser.close()


def _safe_screenshot(page: Page, name: str) -> Optional[str]:
    try:
        path = Path(__file__).with_name(f"{name}.png")
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        LOGGER.exception("Failed to take screenshot")
        return None


def _skip_if_missing_web_prereqs(test_data: Dict[str, Any]) -> None:
    try:
        _require(test_data, "web.base_url")
        _require(test_data, "web.selectors")
        _require(test_data, "web.credentials.username")
        _require(test_data, "web.credentials.password")
    except KeyError as exc:
        pytest.skip(str(exc))


def _login(page: Page, test_data: Dict[str, Any]) -> None:
    web = _require(test_data, "web")
    selectors = _require(web, "selectors")

    base_url = _require(web, "base_url")
    creds = _require(web, "credentials")

    username = _maybe_get(
        test_data,
        "web.payment.wallet.insufficient_balance_user",
        _require(creds, "username"),
    )
    password = _maybe_get(
        test_data,
        "web.payment.wallet.insufficient_balance_password",
        _require(creds, "password"),
    )

    page.goto(base_url, wait_until="domcontentloaded")
    page.fill(_require(selectors, "login_username"), str(username))
    page.fill(_require(selectors, "login_password"), str(password))
    page.click(_require(selectors, "login_submit"))


def _prepare_checkout(page: Page, test_data: Dict[str, Any]) -> None:
    selectors = _require(test_data, "web.selectors")
    page.click(_require(selectors, "add_to_cart"))
    page.click(_require(selectors, "cart_checkout"))


def _click_pay(page: Page, test_data: Dict[str, Any]) -> None:
    selectors = _require(test_data, "web.selectors")
    page.click(_require(selectors, "pay_button"))


def _assert_payment_error(page: Page, test_data: Dict[str, Any], contains: str) -> None:
    selectors = _require(test_data, "web.selectors")
    locator = page.locator(_require(selectors, "payment_error"))
    locator.wait_for()
    msg = (locator.text_content() or "").lower()
    assert contains.lower() in msg, f"Expected '{contains}' in error message, got: {msg!r}"


def test_tc06_wallet_payment_fails_insufficient_balance(
    page: Page,
    test_data: Dict[str, Any],
) -> None:
    """TC_06: Digital wallet payment fails due to insufficient wallet balance."""

    _skip_if_missing_web_prereqs(test_data)

    try:
        selectors = _require(test_data, "web.selectors")
        _login(page, test_data)
        _prepare_checkout(page, test_data)

        page.click(_require(selectors, "payment_method_wallet"))
        _click_pay(page, test_data)

        expected = str(
            _maybe_get(test_data, "web.payment.expected_errors.wallet", "insufficient")
        )
        _assert_payment_error(page, test_data, expected)
    except Exception:
        _safe_screenshot(page, "tc06_failure")
        raise


def _install_timeout_route(page: Page, test_data: Dict[str, Any]) -> None:
    pattern = _maybe_get(test_data, "web.network_simulation.timeout_url_pattern")
    if not pattern:
        pytest.skip("Timeout simulation pattern not configured: web.network_simulation.timeout_url_pattern")

    mode = str(_maybe_get(test_data, "web.network_simulation.timeout_mode", "abort")).lower()
    stall_seconds = float(_maybe_get(test_data, "web.network_simulation.stall_seconds", 15))

    def _handler(route: Route) -> None:
        if mode == "stall":
            time.sleep(stall_seconds)
        route.abort("timedout")

    page.route(pattern, _handler)


def test_tc08_payment_gateway_timeout(
    page: Page,
    test_data: Dict[str, Any],
) -> None:
    """TC_08: Payment gateway timeout while processing payment."""

    _skip_if_missing_web_prereqs(test_data)

    try:
        selectors = _require(test_data, "web.selectors")
        _install_timeout_route(page, test_data)

        _login(page, test_data)
        _prepare_checkout(page, test_data)

        # Use card method for the attempt if configured.
        if "payment_method_card" in selectors:
            page.click(_require(selectors, "payment_method_card"))

            card = _maybe_get(test_data, "web.payment.card.valid")
            fields = _maybe_get(test_data, "web.payment.card.fields")
            if not (card and fields):
                pytest.skip("Card test data missing for timeout test")

            page.fill(_require(fields, "number"), str(_require(card, "number")))
            page.fill(_require(fields, "expiry_month"), str(_require(card, "expiry_month")))
            page.fill(_require(fields, "expiry_year"), str(_require(card, "expiry_year")))
            page.fill(_require(fields, "cvv"), str(_require(card, "cvv")))
            page.fill(_require(fields, "name"), str(_require(card, "name")))

        _click_pay(page, test_data)

        expected = str(_maybe_get(test_data, "web.payment.expected_errors.timeout", "timeout"))
        _assert_payment_error(page, test_data, expected)

    except Exception:
        _safe_screenshot(page, "tc08_failure")
        raise
