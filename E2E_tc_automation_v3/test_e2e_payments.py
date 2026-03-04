"""E2E payment flow automation (Playwright + optional API/DB checks).

Scenarios automated (tagged A in pipeline_testcase_v1.xlsx):
- TC_01: Happy path payment success
- TC_02: Payment failure with retry/alternate method
- TC_04: Refund/cancellation updates status and notifies user

How to run (example):
  pip install playwright pytest requests psycopg2-binary
  playwright install
  pytest -q E2E_tc_automation_v3/test_e2e_payments.py

Configuration:
- Update E2E_tc_automation_v3/test_data.json for selectors and test data.
- Optionally set env vars for API/DB integrations.

Environment variables (optional):
- BASE_URL: overrides test_data.json base_url
- API_BASE_URL: base URL for backend API checks
- API_TOKEN: bearer token for API calls
- PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD: PostgreSQL connection
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    api_base_url: Optional[str]
    api_token: Optional[str]
    pg_dsn: Optional[str]


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def _load_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    if not data_path.exists():
        raise FileNotFoundError(f"Missing test data file: {data_path}")

    with data_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_pg_dsn_from_env() -> Optional[str]:
    host = os.getenv("PGHOST")
    db = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    port = os.getenv("PGPORT", "5432")

    if not all([host, db, user, password]):
        return None

    # psycopg2 DSN format
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _get_runtime_config(test_data: Dict[str, Any]) -> RuntimeConfig:
    base_url = os.getenv("BASE_URL") or str(test_data.get("base_url", "")).strip()
    if not base_url:
        raise ValueError("base_url is required (set in test_data.json or BASE_URL env var)")

    return RuntimeConfig(
        base_url=base_url.rstrip("/"),
        api_base_url=os.getenv("API_BASE_URL") or test_data.get("api_base_url"),
        api_token=os.getenv("API_TOKEN") or test_data.get("api_token"),
        pg_dsn=_build_pg_dsn_from_env() or test_data.get("pg_dsn"),
    )


def _safe_mkdir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Failed to create directory {path}: {exc}") from exc


def _artifact_dir() -> Path:
    out_dir = Path(__file__).with_name("artifacts")
    _safe_mkdir(out_dir)
    return out_dir


def _save_failure_artifacts(page: Page, name: str) -> None:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)

    out_dir = _artifact_dir()
    screenshot_path = out_dir / f"{safe_name}_{timestamp}.png"
    html_path = out_dir / f"{safe_name}_{timestamp}.html"

    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        LOGGER.info("Saved screenshot: %s", screenshot_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to save screenshot: %s", exc)

    try:
        html_path.write_text(page.content(), encoding="utf-8")
        LOGGER.info("Saved page HTML: %s", html_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to save page HTML: %s", exc)


def _require_key(dct: Dict[str, Any], key: str) -> Any:
    if key not in dct:
        raise KeyError(f"Missing required key in test_data.json: '{key}'")
    return dct[key]


def _selector(selectors: Dict[str, str], key: str) -> str:
    value = selectors.get(key)
    if not value:
        raise KeyError(f"Missing selector '{key}' in test_data.json")
    return value


def _goto(page: Page, base_url: str, path: str) -> None:
    url = f"{base_url}/{path.lstrip('/')}" if path else base_url
    LOGGER.info("Navigating to %s", url)
    page.goto(url, wait_until="domcontentloaded")


def _ui_login(page: Page, cfg: RuntimeConfig, test_data: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})
    creds = _require_key(test_data, "credentials")

    _goto(page, cfg.base_url, test_data.get("paths", {}).get("login", "/login"))
    page.fill(_selector(selectors, "login_username"), str(_require_key(creds, "username")))
    page.fill(_selector(selectors, "login_password"), str(_require_key(creds, "password")))

    with page.expect_navigation(wait_until="domcontentloaded"):
        page.click(_selector(selectors, "login_submit"))

    assert page.url.startswith(cfg.base_url), "Unexpected redirect after login"


def _ui_add_item_to_cart(page: Page, cfg: RuntimeConfig, test_data: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})
    _goto(page, cfg.base_url, test_data.get("paths", {}).get("shop", "/"))

    page.click(_selector(selectors, "first_item"))
    page.click(_selector(selectors, "add_to_cart"))
    page.click(_selector(selectors, "go_to_cart"))


def _ui_proceed_to_checkout(page: Page, test_data: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})
    page.click(_selector(selectors, "proceed_to_checkout"))


def _ui_select_payment_method(page: Page, test_data: Dict[str, Any], method: str) -> None:
    selectors = test_data.get("selectors", {})

    # Method can be "card", "wallet", "net_banking" etc.
    mapping = {
        "card": "payment_card",
        "wallet": "payment_wallet",
        "net_banking": "payment_net_banking",
    }
    selector_key = mapping.get(method)
    if not selector_key:
        raise ValueError(f"Unsupported payment method: {method}")

    page.click(_selector(selectors, selector_key))


def _ui_pay_by_card(page: Page, test_data: Dict[str, Any], card: Dict[str, Any]) -> None:
    selectors = test_data.get("selectors", {})

    page.fill(_selector(selectors, "card_number"), str(_require_key(card, "number")))
    page.fill(_selector(selectors, "card_expiry"), str(_require_key(card, "expiry")))
    page.fill(_selector(selectors, "card_cvv"), str(_require_key(card, "cvv")))

    page.click(_selector(selectors, "pay_now"))


def _ui_wait_for_status(page: Page, test_data: Dict[str, Any]) -> Tuple[str, str]:
    selectors = test_data.get("selectors", {})
    timeout_ms = int(test_data.get("timeouts", {}).get("payment_status_ms", 60000))

    status_el = _selector(selectors, "payment_status")
    page.wait_for_selector(status_el, timeout=timeout_ms)

    status_text = page.inner_text(status_el).strip()

    order_id = ""
    order_id_selector = selectors.get("order_id")
    if order_id_selector:
        try:
            order_id = page.inner_text(order_id_selector).strip()
        except Exception:  # noqa: BLE001
            order_id = ""

    LOGGER.info("Payment status: %s, order_id: %s", status_text, order_id)
    return status_text, order_id


def _api_get_order_status(cfg: RuntimeConfig, order_id: str) -> Optional[Dict[str, Any]]:
    if not cfg.api_base_url or not cfg.api_token or not order_id:
        return None

    try:
        import requests

        url = f"{cfg.api_base_url.rstrip('/')}/orders/{order_id}"
        headers = {"Authorization": f"Bearer {cfg.api_token}"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("API order status check failed: %s", exc)
        return None


def _api_cancel_order(cfg: RuntimeConfig, order_id: str) -> bool:
    if not cfg.api_base_url or not cfg.api_token or not order_id:
        return False

    try:
        import requests

        url = f"{cfg.api_base_url.rstrip('/')}/orders/{order_id}/cancel"
        headers = {"Authorization": f"Bearer {cfg.api_token}"}
        resp = requests.post(url, headers=headers, timeout=20)
        resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("API cancel/refund call failed: %s", exc)
        return False


def _db_fetch_payment_row(cfg: RuntimeConfig, order_id: str) -> Optional[Dict[str, Any]]:
    if not cfg.pg_dsn or not order_id:
        return None

    try:
        import psycopg2
        import psycopg2.extras

        query = """
            SELECT order_id, status, updated_at
            FROM payments
            WHERE order_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
        """

        with psycopg2.connect(cfg.pg_dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, (order_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("DB validation failed: %s", exc)
        return None


def _place_paid_order(page: Page, cfg: RuntimeConfig, test_data: Dict[str, Any]) -> str:
    """Places an order and performs a successful card payment."""

    _ui_login(page, cfg, test_data)
    _ui_add_item_to_cart(page, cfg, test_data)
    _ui_proceed_to_checkout(page, test_data)

    _ui_select_payment_method(page, test_data, method="card")
    card = _require_key(_require_key(test_data, "payments"), "valid_card")
    _ui_pay_by_card(page, test_data, card=card)

    status_text, order_id = _ui_wait_for_status(page, test_data)
    expected = str(test_data.get("expected", {}).get("payment_success_substring", "success")).lower()
    assert expected in status_text.lower(), f"Expected success status containing '{expected}', got '{status_text}'"

    return order_id


def _run_test_case(page: Page, name: str, fn) -> None:  # type: ignore[no-untyped-def]
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Test case failed: %s", name)
        _save_failure_artifacts(page, name=name)
        raise exc


@pytest.fixture(scope="session")
def test_data() -> Dict[str, Any]:
    _configure_logging()
    return _load_test_data()


@pytest.fixture(scope="session")
def cfg(test_data: Dict[str, Any]) -> RuntimeConfig:
    return _get_runtime_config(test_data)


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright, test_data: Dict[str, Any]) -> Browser:
    browser_name = str(test_data.get("browser", "chromium")).strip().lower()
    headless = bool(test_data.get("headless", True))

    if browser_name == "firefox":
        return playwright_instance.firefox.launch(headless=headless)
    if browser_name == "webkit":
        return playwright_instance.webkit.launch(headless=headless)

    return playwright_instance.chromium.launch(headless=headless)


@pytest.fixture()
def page(browser: Browser, test_data: Dict[str, Any]) -> Page:
    context = browser.new_context(
        viewport=test_data.get("viewport", {"width": 1366, "height": 768}),
    )
    page_obj = context.new_page()
    yield page_obj

    try:
        context.close()
    except Exception:  # noqa: BLE001
        pass


def test_tc01_happy_path_successful_card_payment(page: Page, cfg: RuntimeConfig, test_data: Dict[str, Any]) -> None:
    """TC_01: Successful card payment shows confirmation and order proceeds."""

    def _steps() -> None:
        order_id = _place_paid_order(page, cfg, test_data)

        api_status = _api_get_order_status(cfg, order_id)
        if api_status is not None:
            expected_state = test_data.get("expected", {}).get("order_state_after_payment")
            if expected_state:
                assert (
                    str(api_status.get("state", "")).lower() == str(expected_state).lower()
                ), f"Unexpected API order state: {api_status}"

        db_row = _db_fetch_payment_row(cfg, order_id)
        if db_row is not None:
            expected_payment_status = test_data.get("expected", {}).get("db_payment_status_after_payment")
            if expected_payment_status:
                assert (
                    str(db_row.get("status", "")).lower() == str(expected_payment_status).lower()
                ), f"Unexpected DB payment status: {db_row}"

    _run_test_case(page, "TC_01", _steps)


def test_tc02_payment_failure_allows_retry_or_alternate_method(
    page: Page,
    cfg: RuntimeConfig,
    test_data: Dict[str, Any],
) -> None:
    """TC_02: Payment failure shows actionable error and allows retry/alternate method."""

    def _steps() -> None:
        selectors = test_data.get("selectors", {})

        _ui_login(page, cfg, test_data)
        _ui_add_item_to_cart(page, cfg, test_data)
        _ui_proceed_to_checkout(page, test_data)

        _ui_select_payment_method(page, test_data, method="card")
        card = _require_key(_require_key(test_data, "payments"), "declined_card")
        _ui_pay_by_card(page, test_data, card=card)

        # Expect an error message and ability to retry/change method.
        error_selector = selectors.get("payment_error")
        if not error_selector:
            raise KeyError("Missing selectors.payment_error in test_data.json")

        page.wait_for_selector(error_selector, timeout=60000)
        error_text = page.inner_text(error_selector).strip()

        expected_sub = str(test_data.get("expected", {}).get("payment_failure_substring", "declined")).lower()
        assert expected_sub in error_text.lower(), f"Expected error containing '{expected_sub}', got '{error_text}'"

        # Verify checkout data is still present (order summary/cart not reset)
        summary_sel = selectors.get("order_summary")
        if summary_sel:
            assert page.is_visible(summary_sel), "Order summary not visible after payment failure"

        # Retry / Alternate method
        if selectors.get("change_payment_method"):
            page.click(selectors["change_payment_method"])

        alt = _require_key(_require_key(test_data, "payments"), "alternate_method")
        alt_method = str(_require_key(alt, "method"))
        _ui_select_payment_method(page, test_data, method=alt_method)

        if alt_method == "card":
            _ui_pay_by_card(page, test_data, card=_require_key(_require_key(test_data, "payments"), "valid_card"))
        else:
            # Generic submit for non-card methods (selectors must be configured)
            page.click(_selector(selectors, "pay_now"))

        status_text, _order_id = _ui_wait_for_status(page, test_data)
        expected_success = str(test_data.get("expected", {}).get("payment_success_substring", "success")).lower()
        assert (
            expected_success in status_text.lower()
        ), f"Expected success after alternate method, got '{status_text}'"

    _run_test_case(page, "TC_02", _steps)


def test_tc04_refund_cancellation_updates_status_and_notifies_user(
    page: Page,
    cfg: RuntimeConfig,
    test_data: Dict[str, Any],
) -> None:
    """TC_04: Refund/cancellation updates status and user receives notification."""

    def _steps() -> None:
        selectors = test_data.get("selectors", {})

        order_id = _place_paid_order(page, cfg, test_data)

        cancelled_by_api = _api_cancel_order(cfg, order_id)
        if not cancelled_by_api:
            # Fallback to UI cancellation if configured.
            cancel_button = selectors.get("cancel_order")
            if not cancel_button:
                pytest.skip("Cancel/refund not configured (no API token or UI selector).")

            page.click(cancel_button)
            confirm_btn = selectors.get("confirm_cancel")
            if confirm_btn:
                page.click(confirm_btn)

        refund_status_sel = selectors.get("refund_status") or selectors.get("payment_status")
        if not refund_status_sel:
            raise KeyError("Missing selectors.refund_status (or payment_status) in test_data.json")

        page.wait_for_selector(refund_status_sel, timeout=120000)
        refund_text = page.inner_text(refund_status_sel).strip()
        expected_refund_sub = str(test_data.get("expected", {}).get("refund_status_substring", "refund")).lower()
        assert (
            expected_refund_sub in refund_text.lower()
        ), f"Expected refund/cancel status containing '{expected_refund_sub}', got '{refund_text}'"

        notification_sel = selectors.get("notification_toast")
        if notification_sel:
            assert page.is_visible(notification_sel), "Refund/cancel notification not visible"

        api_status = _api_get_order_status(cfg, order_id)
        if api_status is not None:
            expected_state = test_data.get("expected", {}).get("order_state_after_refund")
            if expected_state:
                assert (
                    str(api_status.get("state", "")).lower() == str(expected_state).lower()
                ), f"Unexpected API order state after refund: {api_status}"

        db_row = _db_fetch_payment_row(cfg, order_id)
        if db_row is not None:
            expected_payment_status = test_data.get("expected", {}).get("db_payment_status_after_refund")
            if expected_payment_status:
                assert (
                    str(db_row.get("status", "")).lower() == str(expected_payment_status).lower()
                ), f"Unexpected DB payment status after refund: {db_row}"

    _run_test_case(page, "TC_04", _steps)
