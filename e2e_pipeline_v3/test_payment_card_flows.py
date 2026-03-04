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


def _db_is_enabled(test_data: Dict[str, Any]) -> bool:
    return bool(_maybe_get(test_data, "db.enabled", False))


def _db_fetch_one(
    test_data: Dict[str, Any],
    query: str,
    params: tuple[Any, ...],
) -> Optional[tuple[Any, ...]]:
    if not _db_is_enabled(test_data):
        return None

    db_cfg = _require(test_data, "db")

    try:
        import psycopg  # type: ignore

        conn = psycopg.connect(
            host=_require(db_cfg, "host"),
            port=_require(db_cfg, "port"),
            dbname=_require(db_cfg, "dbname"),
            user=_require(db_cfg, "user"),
            password=_require(db_cfg, "password"),
            sslmode=_maybe_get(db_cfg, "sslmode", "prefer"),
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()
    except ImportError:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(
            host=_require(db_cfg, "host"),
            port=_require(db_cfg, "port"),
            dbname=_require(db_cfg, "dbname"),
            user=_require(db_cfg, "user"),
            password=_require(db_cfg, "password"),
            sslmode=_maybe_get(db_cfg, "sslmode", "prefer"),
        )
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.fetchone()
        finally:
            conn.close()


def _maybe_get_order_id(page: Page, web_cfg: WebConfig) -> Optional[str]:
    """Try to derive order_id from UI.

    Configure `web.selectors.order_id` to extract it from the page.
    """

    order_id_selector = web_cfg.selectors.get("order_id")
    if not order_id_selector:
        return None

    text = (page.locator(order_id_selector).text_content() or "").strip()
    return text or None


def _expected_error_text(test_data: Dict[str, Any], card_key: str, fallback: str) -> str:
    override = _maybe_get(test_data, f"web.payment.expected_errors.{card_key}")
    return str(override) if override else fallback


def _skip_if_missing_web_prereqs(test_data: Dict[str, Any]) -> None:
    try:
        _require(test_data, "web.base_url")
        _require(test_data, "web.selectors")
        _require(test_data, "web.credentials.username")
        _require(test_data, "web.credentials.password")
    except KeyError as exc:
        pytest.skip(str(exc))


def test_tc01_successful_card_payment_happy_path(
    page: Page,
    test_data: Dict[str, Any],
    web_config: WebConfig,
) -> None:
    """TC_01: Successful payment using valid credit/debit card."""

    _skip_if_missing_web_prereqs(test_data)

    try:
        _login(page, test_data, web_config)
        _prepare_checkout(page, web_config)
        _select_card_method(page, web_config)
        _fill_card(page, test_data, "valid")
        _click_pay(page, web_config)
        _assert_payment_success(page, web_config)
    except Exception:
        _safe_screenshot(page, "tc01_failure")
        raise


@pytest.mark.parametrize(
    "card_key,fallback_error_contains",
    [
        ("invalid_number", "invalid"),  # TC_03
        ("expired", "expired"),  # TC_04
        ("decline", "declined"),  # TC_05
    ],
)
def test_card_payment_negative_validations(
    page: Page,
    test_data: Dict[str, Any],
    web_config: WebConfig,
    card_key: str,
    fallback_error_contains: str,
) -> None:
    """TC_03/04/05: Card payment fails for invalid/expired/declined cards."""

    _skip_if_missing_web_prereqs(test_data)

    expected = _expected_error_text(test_data, card_key, fallback_error_contains)

    try:
        _login(page, test_data, web_config)
        _prepare_checkout(page, web_config)
        _select_card_method(page, web_config)
        _fill_card(page, test_data, card_key)
        _click_pay(page, web_config)
        _assert_payment_error(page, web_config, expected)
    except Exception:
        _safe_screenshot(page, f"{card_key}_failure")
        raise


def test_tc10_prevent_duplicate_charges_idempotency(
    page: Page,
    test_data: Dict[str, Any],
    web_config: WebConfig,
) -> None:
    """TC_10: Prevent duplicate charges on rapid repeated Pay clicks."""

    _skip_if_missing_web_prereqs(test_data)

    payment_patterns = _maybe_get(test_data, "web.security.payment_request_url_patterns", [])
    request_count = {"count": 0}

    def _is_payment_request(url: str) -> bool:
        return any(part.strip("*") in url for part in payment_patterns) if payment_patterns else False

    def _on_request(request: Any) -> None:
        if _is_payment_request(request.url):
            request_count["count"] += 1

    page.on("request", _on_request)

    try:
        _login(page, test_data, web_config)
        _prepare_checkout(page, web_config)
        _select_card_method(page, web_config)
        _fill_card(page, test_data, "valid")

        pay_sel = _require(web_config.selectors, "pay_button")
        for _ in range(3):
            try:
                page.click(pay_sel, timeout=500)
            except Exception:
                # Button may become disabled/hidden after first click.
                break

        _assert_payment_success(page, web_config)

        if payment_patterns:
            assert request_count["count"] <= 1, (
                "Detected multiple payment submission requests; "
                f"count={request_count['count']} (expected <= 1)"
            )
    except Exception:
        _safe_screenshot(page, "tc10_failure")
        raise


def test_tc15_payment_confirmation_within_threshold(
    page: Page,
    test_data: Dict[str, Any],
    web_config: WebConfig,
) -> None:
    """TC_15: Payment confirmation completes within acceptable time."""

    _skip_if_missing_web_prereqs(test_data)

    threshold = float(_maybe_get(test_data, "web.payment.performance_threshold_seconds", 10))

    try:
        _login(page, test_data, web_config)
        _prepare_checkout(page, web_config)
        _select_card_method(page, web_config)
        _fill_card(page, test_data, "valid")

        start = time.monotonic()
        _click_pay(page, web_config)
        _assert_payment_success(page, web_config)
        elapsed = time.monotonic() - start

        assert elapsed <= threshold, (
            f"Payment confirmation exceeded threshold: {elapsed:.2f}s > {threshold:.2f}s"
        )
    except Exception:
        _safe_screenshot(page, "tc15_failure")
        raise
