import logging
import os
from typing import Any, Dict, Optional

import pytest
import requests
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


LOGGER = logging.getLogger(__name__)


def _get_selector(test_data: Dict[str, Any], name: str) -> str:
    selector = test_data.get("selectors", {}).get(name)
    if not selector:
        raise KeyError(f"Missing selector in test_data.json: selectors.{name}")
    return selector


def _goto_base(page: Page, test_data: Dict[str, Any]) -> None:
    base_url = test_data.get("base_url") or os.environ.get("BASE_URL")
    if not base_url:
        raise RuntimeError("BASE_URL is not configured (set env var or test_data.json)")

    page.goto(base_url, wait_until="domcontentloaded")


def login_if_needed(page: Page, test_data: Dict[str, Any]) -> None:
    """Optional login helper.

    If selectors are not present in your AUT, update test_data.json or remove login.
    """
    username = test_data.get("auth", {}).get("username")
    password = test_data.get("auth", {}).get("password")
    if not username or not password:
        return

    try:
        page.locator(_get_selector(test_data, "login_username")).fill(username)
        page.locator(_get_selector(test_data, "login_password")).fill(password)
        page.locator(_get_selector(test_data, "login_submit")).click()
        page.wait_for_load_state("networkidle")
    except PlaywrightTimeoutError as exc:
        raise AssertionError("Login did not complete in time") from exc


def open_checkout(page: Page, test_data: Dict[str, Any]) -> None:
    page.locator(_get_selector(test_data, "checkout_open")).click()
    page.locator(_get_selector(test_data, "payment_method_section")).wait_for(
        state="visible", timeout=15_000
    )


def select_payment_method(page: Page, test_data: Dict[str, Any], method: str) -> None:
    key_map = {
        "card": "payment_method_card",
        "wallet": "payment_method_wallet",
        "netbanking": "payment_method_netbanking",
    }
    if method not in key_map:
        raise ValueError(f"Unsupported payment method: {method}")

    page.locator(_get_selector(test_data, key_map[method])).click()


def fill_card_details(page: Page, test_data: Dict[str, Any], card_key: str) -> None:
    card = test_data.get("payments", {}).get(card_key)
    if not card:
        raise KeyError(f"Missing card test data: payments.{card_key}")

    page.locator(_get_selector(test_data, "card_number")).fill(card["number"])
    page.locator(_get_selector(test_data, "card_expiry")).fill(card["expiry"])
    page.locator(_get_selector(test_data, "card_cvv")).fill(card["cvv"])
    page.locator(_get_selector(test_data, "card_name")).fill(card["name"])


def click_pay(page: Page, test_data: Dict[str, Any]) -> None:
    pay_btn = page.locator(_get_selector(test_data, "pay_button"))
    pay_btn.wait_for(state="visible", timeout=10_000)
    pay_btn.click()


def _read_toast(page: Page, test_data: Dict[str, Any]) -> str:
    toast = page.locator(_get_selector(test_data, "toast_message"))
    try:
        toast.wait_for(state="visible", timeout=10_000)
        return toast.inner_text().strip()
    except PlaywrightTimeoutError:
        return ""


def assert_order_paid(page: Page, test_data: Dict[str, Any]) -> None:
    status = page.locator(_get_selector(test_data, "order_status"))
    try:
        status.wait_for(state="visible", timeout=20_000)
        text = status.inner_text().strip().lower()
    except PlaywrightTimeoutError as exc:
        raise AssertionError("Order status was not visible after payment") from exc

    assert "paid" in text, f"Expected 'paid' in order status, got: {text!r}"


def _api_get_payment_status(test_data: Dict[str, Any], order_id: str) -> Optional[Dict[str, Any]]:
    api_base = test_data.get("api", {}).get("base_url")
    path = test_data.get("api", {}).get("payment_status_path")
    if not api_base or not path:
        return None

    url = f"{api_base.rstrip('/')}{path}?order_id={order_id}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        LOGGER.warning("API payment status check failed: %s", exc)
        return None


@pytest.mark.e2e
@pytest.mark.parametrize(
    "method",
    [
        pytest.param("card", id="TC_01_card_success"),
        pytest.param("wallet", id="TC_02_wallet_success"),
    ],
)
def test_successful_payment_from_checkout(page: Page, test_data: Dict[str, Any], method: str) -> None:
    """Covers:
    - TC_01: Successful payment using Credit/Debit Card from checkout
    - TC_02: Successful payment using Digital Wallet from checkout
    """
    _goto_base(page, test_data)
    login_if_needed(page, test_data)

    open_checkout(page, test_data)
    select_payment_method(page, test_data, method)

    if method == "card":
        fill_card_details(page, test_data, card_key="valid_card")

    click_pay(page, test_data)

    toast_text = _read_toast(page, test_data)
    if toast_text:
        LOGGER.info("Toast message after payment: %s", toast_text)

    assert_order_paid(page, test_data)

    # Optional API cross-check (best-effort). If your AUT exposes an order id in URL,
    # adapt the extraction and the API endpoint configuration in test_data.json.
    _ = _api_get_payment_status(test_data, order_id="")
