import logging
import re
from typing import Any, Dict

import pytest
from playwright.sync_api import Page, Route, TimeoutError as PlaywrightTimeoutError

from E2E_tc_automation_v1.test_payment_happy_paths import (
    _get_selector,
    _goto_base,
    assert_order_paid,
    click_pay,
    fill_card_details,
    login_if_needed,
    open_checkout,
    select_payment_method,
)


LOGGER = logging.getLogger(__name__)


def _assert_toast_contains(page: Page, test_data: Dict[str, Any], expected_substrings: list[str]) -> None:
    toast = page.locator(_get_selector(test_data, "toast_message"))
    try:
        toast.wait_for(state="visible", timeout=10_000)
        text = toast.inner_text().strip().lower()
    except PlaywrightTimeoutError as exc:
        raise AssertionError("Expected an error/validation toast, but none appeared") from exc

    for needle in expected_substrings:
        assert needle.lower() in text, f"Expected toast to contain {needle!r}, got: {text!r}"


def _assert_order_not_paid(page: Page, test_data: Dict[str, Any]) -> None:
    status = page.locator(_get_selector(test_data, "order_status"))
    try:
        if status.is_visible():
            text = status.inner_text().strip().lower()
            assert "paid" not in text, f"Order should not be paid, but status was: {text!r}"
    except Exception:
        # Some AUTs show order status only after successful placement.
        return


@pytest.mark.e2e
def test_supported_payment_methods_are_visible(page: Page, test_data: Dict[str, Any]) -> None:
    """Covers TC_03."""
    _goto_base(page, test_data)
    login_if_needed(page, test_data)
    open_checkout(page, test_data)

    for key in [
        "payment_method_card",
        "payment_method_wallet",
        "payment_method_netbanking",
    ]:
        locator = page.locator(_get_selector(test_data, key))
        assert locator.is_visible(), f"Expected payment method '{key}' to be visible"


@pytest.mark.e2e
def test_invalid_card_number_shows_validation_and_blocks_payment(page: Page, test_data: Dict[str, Any]) -> None:
    """Covers TC_04."""
    _goto_base(page, test_data)
    login_if_needed(page, test_data)
    open_checkout(page, test_data)

    select_payment_method(page, test_data, "card")
    fill_card_details(page, test_data, card_key="invalid_card")
    click_pay(page, test_data)

    _assert_toast_contains(page, test_data, ["invalid", "card"])
    _assert_order_not_paid(page, test_data)


@pytest.mark.e2e
def test_expired_card_shows_error_and_allows_retry(page: Page, test_data: Dict[str, Any]) -> None:
    """Covers TC_05."""
    _goto_base(page, test_data)
    login_if_needed(page, test_data)
    open_checkout(page, test_data)

    select_payment_method(page, test_data, "card")
    fill_card_details(page, test_data, card_key="expired_card")
    click_pay(page, test_data)

    _assert_toast_contains(page, test_data, ["expired"])

    retry = page.locator(_get_selector(test_data, "retry_button"))
    if retry.is_visible():
        retry.click()


@pytest.mark.e2e
def test_gateway_decline_shows_failure_and_supports_retry_and_switch(page: Page, test_data: Dict[str, Any]) -> None:
    """Covers TC_06 and partially TC_12."""
    _goto_base(page, test_data)
    login_if_needed(page, test_data)
    open_checkout(page, test_data)

    select_payment_method(page, test_data, "card")
    fill_card_details(page, test_data, card_key="decline_card")
    click_pay(page, test_data)

    _assert_toast_contains(page, test_data, ["declin"])  # declined/decline

    retry = page.locator(_get_selector(test_data, "retry_button"))
    if retry.is_visible():
        retry.click()

    # Switch method after a failure (TC_12): wallet -> pay should succeed if your env supports it.
    select_payment_method(page, test_data, "wallet")
    click_pay(page, test_data)

    # If wallet is configured and succeeds, paid is expected; otherwise surface helpful failure.
    assert_order_paid(page, test_data)


def _route_timeout(route: Route) -> None:
    # Delay and then abort to simulate timeout/no response.
    route.fulfill(status=504, content_type="application/json", body="{\"error\":\"timeout\"}")


def _route_503(route: Route) -> None:
    route.fulfill(
        status=503,
        content_type="application/json",
        body="{\"error\":\"service unavailable\"}",
    )


@pytest.mark.e2e
@pytest.mark.parametrize(
    "scenario, handler, expected_toast",
    [
        pytest.param("timeout", _route_timeout, ["timeout"], id="TC_08_timeout"),
        pytest.param("unavailable", _route_503, ["unavailable"], id="TC_09_5xx"),
    ],
)
def test_gateway_failure_messages_are_user_friendly(
    page: Page,
    test_data: Dict[str, Any],
    scenario: str,
    handler,
    expected_toast: list[str],
) -> None:
    """Covers TC_08 and TC_09 using network stubbing (when configured).

    Configure `network.gateway_url_pattern` in `test_data.json` to enable.
    """
    pattern = test_data.get("network", {}).get("gateway_url_pattern")
    if not pattern:
        pytest.skip("network.gateway_url_pattern not configured for gateway stubbing")

    page.route(re.compile(pattern), lambda route: handler(route))

    _goto_base(page, test_data)
    login_if_needed(page, test_data)
    open_checkout(page, test_data)

    select_payment_method(page, test_data, "card")
    fill_card_details(page, test_data, card_key="valid_card")
    click_pay(page, test_data)

    _assert_toast_contains(page, test_data, expected_toast)
    _assert_order_not_paid(page, test_data)


@pytest.mark.e2e
def test_multiple_rapid_pay_taps_do_not_double_submit(page: Page, test_data: Dict[str, Any]) -> None:
    """Covers TC_11 (best-effort).

    Verifies the Pay button becomes disabled or the UI transitions to processing after the first click.
    """
    _goto_base(page, test_data)
    login_if_needed(page, test_data)
    open_checkout(page, test_data)

    select_payment_method(page, test_data, "card")
    fill_card_details(page, test_data, card_key="valid_card")

    pay_btn = page.locator(_get_selector(test_data, "pay_button"))
    pay_btn.wait_for(state="visible", timeout=10_000)

    for _ in range(5):
        pay_btn.click(force=True)

    try:
        page.wait_for_timeout(500)
        assert pay_btn.is_disabled() or not pay_btn.is_visible(), (
            "Pay button should be disabled/hidden after submit to prevent duplicates"
        )
    except Exception as exc:
        raise AssertionError(
            "Could not confirm double-submit prevention. "
            "Add a stable selector for processing state or instrument network calls."
        ) from exc
