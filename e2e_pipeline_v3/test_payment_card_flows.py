"""Automation scenarios (tagged A) derived from pipeline_testcase.xlsx.

Coverage in this module:
- TC_01 Successful card payment (happy path)
- TC_03 Invalid card number format
- TC_04 Expired card
- TC_05 Declined/insufficient funds
- TC_10 Prevent duplicate charges on rapid repeated Pay clicks (idempotency)
- TC_15 Performance boundary for payment confirmation
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from playwright.sync_api import Page, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebConfig:
    base_url: str
    selectors: Dict[str, str]
    timeouts_ms: Dict[str, int]


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


@pytest.fixture(scope="session")
def web_config(test_data: Dict[str, Any]) -> WebConfig:
    web = _require(test_data, "web")
    return WebConfig(
        base_url=_require(web, "base_url"),
        selectors=_require(web, "selectors"),
        timeouts_ms=_require(web, "timeouts_ms"),
    )


@pytest.fixture()
def page(web_config: WebConfig) -> Page:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(web_config.timeouts_ms.get("action", 10000))
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


def _login(page: Page, test_data: Dict[str, Any], web_cfg: WebConfig) -> None:
    selectors = web_cfg.selectors
    creds = _require(test_data, "web.credentials")

    page.goto(web_cfg.base_url, wait_until="domcontentloaded")

    page.fill(_require(selectors, "login_username"), _require(creds, "username"))
    page.fill(_require(selectors, "login_password"), _require(creds, "password"))
    page.click(_require(selectors, "login_submit"))


def _prepare_checkout(page: Page, web_cfg: WebConfig) -> None:
    sel = web_cfg.selectors
    page.click(_require(sel, "add_to_cart"))
    page.click(_require(sel, "cart_checkout"))


def _select_card_method(page: Page, web_cfg: WebConfig) -> None:
    page.click(_require(web_cfg.selectors, "payment_method_card"))


def _fill_card(page: Page, test_data: Dict[str, Any], card_key: str) -> None:
    card = _require(test_data, f"web.payment.card.{card_key}")
    fields = _require(test_data, "web.payment.card.fields")

    page.fill(_require(fields, "number"), _require(card, "number"))
    page.fill(_require(fields, "expiry_month"), _require(card, "expiry_month"))
    page.fill(_require(fields, "expiry_year"), _require(card, "expiry_year"))
    page.fill(_require(fields, "cvv"), _require(card, "cvv"))
    page.fill(_require(fields, "name"), _require(card, "name"))


def _click_pay(page: Page, web_cfg: WebConfig) -> None:
    page.click(_require(web_cfg.selectors, "pay_button"))


def _assert_payment_success(page: Page, web_cfg: WebConfig) -> None:
    sel = web_cfg.selectors
    page.wait_for_selector(_require(sel, "payment_success"))


def _assert_payment_error(page: Page, web_cfg: WebConfig, contains: str) -> None:
    sel = web_cfg.selectors
    locator = page.locator(_require(sel, "payment_error"))
    locator.wait_for()
    msg = (locator.text_content() or "").lower()
    assert contains.lower() in msg, f"Expected '{contains}' in error message, got: {msg!r}"
