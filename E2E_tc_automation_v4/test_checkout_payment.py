"""E2E checkout payment tests derived from EP-31 automation scenarios.

Covers:
- TC_01: Successful payment using Card
- TC_02: Invalid/expired card rejected
- TC_03: Gateway timeout/delay shows Pending then Failure (or configured final state)

Prerequisites:
- pytest
- playwright (sync API) + installed browsers: `playwright install`

Configuration:
- Update `test_data.json` (same folder) and/or override with environment variables.

Note:
This is a template-style automation implementation because AUT-specific selectors,
URLs, and domain flows vary. Update the selectors and paths in `test_data.json`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError:  # pragma: no cover
    Page = object  # type: ignore
    sync_playwright = None  # type: ignore

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

try:
    import psycopg2
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebConfig:
    base_url: str
    username: str
    password: str


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    order_status_path_template: str
    auth_token: str


@dataclass(frozen=True)
class DbConfig:
    enabled: bool
    host: str
    port: int
    database: str
    user: str
    password: str
    order_status_query: str


@dataclass(frozen=True)
class CardData:
    number: str
    expiry: str
    cvv: str


@dataclass(frozen=True)
class GatewayTimeoutConfig:
    url_pattern: str
    delay_ms: int
    pending_text: str
    final_failure_text: str


@dataclass(frozen=True)
class PaymentConfig:
    valid_card: CardData
    invalid_card: CardData
    gateway_timeout: GatewayTimeoutConfig
    success_text: str


@dataclass(frozen=True)
class TestConfig:
    web: WebConfig
    selectors: Dict[str, str]
    payment: PaymentConfig
    api: ApiConfig
    db: DbConfig


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _read_test_data() -> Dict[str, Any]:
    data_path = Path(__file__).with_name("test_data.json")
    if not data_path.exists():
        raise FileNotFoundError(f"Missing test data file: {data_path}")

    with data_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _build_config(raw: Dict[str, Any]) -> TestConfig:
    web_raw = raw.get("web", {})
    web = WebConfig(
        base_url=os.getenv("WEB_BASE_URL", web_raw.get("base_url", "")),
        username=os.getenv("WEB_USERNAME", web_raw.get("username", "")),
        password=os.getenv("WEB_PASSWORD", web_raw.get("password", "")),
    )

    api_raw = raw.get("api", {})
    api = ApiConfig(
        base_url=os.getenv("API_BASE_URL", api_raw.get("base_url", "")),
        order_status_path_template=api_raw.get(
            "order_status_path_template", "/orders/{order_id}"
        ),
        auth_token=os.getenv("API_AUTH_TOKEN", api_raw.get("auth_token", "")),
    )

    db_raw = raw.get("db", {})
    db = DbConfig(
        enabled=bool(db_raw.get("enabled", False)),
        host=db_raw.get("host", ""),
        port=int(db_raw.get("port", 5432)),
        database=db_raw.get("database", ""),
        user=db_raw.get("user", ""),
        password=db_raw.get("password", ""),
        order_status_query=db_raw.get("order_status_query", ""),
    )

    payment_raw = raw.get("payment", {})
    valid_card_raw = payment_raw.get("valid_card", {})
    invalid_card_raw = payment_raw.get("invalid_card", {})
    gateway_raw = payment_raw.get("gateway_timeout", {})

    payment = PaymentConfig(
        valid_card=CardData(
            number=str(valid_card_raw.get("number", "")),
            expiry=str(valid_card_raw.get("expiry", "")),
            cvv=str(valid_card_raw.get("cvv", "")),
        ),
        invalid_card=CardData(
            number=str(invalid_card_raw.get("number", "")),
            expiry=str(invalid_card_raw.get("expiry", "")),
            cvv=str(invalid_card_raw.get("cvv", "")),
        ),
        gateway_timeout=GatewayTimeoutConfig(
            url_pattern=str(gateway_raw.get("url_pattern", "**/payments/**")),
            delay_ms=int(gateway_raw.get("delay_ms", 15000)),
            pending_text=str(gateway_raw.get("pending_text", "Pending")),
            final_failure_text=str(gateway_raw.get("final_failure_text", "Failure")),
        ),
        success_text=str(payment_raw.get("success_text", "Success")),
    )

    selectors = raw.get("selectors", {})
    if not isinstance(selectors, dict):
        raise ValueError("'selectors' must be a JSON object of key -> selector")

    return TestConfig(web=web, selectors=selectors, payment=payment, api=api, db=db)


def _ensure_prerequisites(config: TestConfig) -> None:
    if sync_playwright is None:
        pytest.skip("playwright is not installed")

    if not config.web.base_url:
        pytest.skip("WEB base_url is not configured in test_data.json or env")


@pytest.fixture(scope="session")
def config() -> TestConfig:
    _configure_logging()
    raw = _read_test_data()
    cfg = _build_config(raw)
    _ensure_prerequisites(cfg)
    return cfg


@pytest.fixture()
def page(config: TestConfig) -> Page:
    assert sync_playwright is not None  # for type checkers

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=(os.getenv("HEADLESS", "true").lower() == "true")
        )
        context = browser.new_context()
        page_obj = context.new_page()

        try:
            yield page_obj
        finally:
            context.close()
            browser.close()


def _safe_screenshot(page: Page, name: str) -> Optional[str]:
    try:
        timestamp = int(time.time())
        file_name = f"{name}_{timestamp}.png"
        page.screenshot(path=file_name, full_page=True)
        return file_name
    except Exception:  # pragma: no cover
        LOGGER.exception("Failed to take screenshot")
        return None


def _selector(config: TestConfig, key: str) -> str:
    selector = config.selectors.get(key)
    if not selector:
        raise KeyError(f"Missing selector '{key}' in test_data.json")
    return selector


def _login(page: Page, config: TestConfig) -> None:
    page.goto(config.web.base_url, wait_until="domcontentloaded")

    page.fill(_selector(config, "username"), config.web.username)
    page.fill(_selector(config, "password"), config.web.password)
    page.click(_selector(config, "login_button"))


def _start_checkout(page: Page, config: TestConfig) -> None:
    page.click(_selector(config, "cart_button"))
    page.click(_selector(config, "checkout_button"))


def _pay_by_card(page: Page, config: TestConfig, card: CardData) -> None:
    page.click(_selector(config, "payment_method_card"))
    page.fill(_selector(config, "card_number"), card.number)
    page.fill(_selector(config, "card_expiry"), card.expiry)
    page.fill(_selector(config, "card_cvv"), card.cvv)
    page.click(_selector(config, "pay_now"))


def _pay_by_wallet(page: Page, config: TestConfig) -> None:
    page.click(_selector(config, "payment_method_wallet"))
    page.click(_selector(config, "pay_now"))


def _try_extract_order_id(page: Page, config: TestConfig) -> Optional[str]:
    selector = config.selectors.get("order_id")
    if not selector:
        return None

    try:
        order_id = page.inner_text(selector).strip()
        return order_id or None
    except Exception:
        LOGGER.exception("Failed to extract order_id using selector '%s'", selector)
        return None


def _validate_order_status_via_api(
    config: TestConfig,
    order_id: str,
    expected_status_contains: str,
) -> None:
    if requests is None:
        LOGGER.info("Skipping API validation (requests not installed)")
        return

    if not config.api.base_url or not config.api.auth_token:
        LOGGER.info("Skipping API validation (API config not provided)")
        return

    url = (
        config.api.base_url.rstrip("/")
        + config.api.order_status_path_template.format(order_id=order_id)
    )
    headers = {"Authorization": f"Bearer {config.api.auth_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise AssertionError(f"API order status validation failed: {exc}") from exc

    status_str = json.dumps(payload)
    if expected_status_contains not in status_str:
        raise AssertionError(
            f"API status did not contain '{expected_status_contains}'. Payload: {payload}"
        )


def _validate_payment_status_in_db(
    config: TestConfig,
    order_id: str,
    expected_status_contains: str,
) -> None:
    if not config.db.enabled:
        LOGGER.info("Skipping DB validation (db.enabled=false)")
        return

    if psycopg2 is None:
        LOGGER.info("Skipping DB validation (psycopg2 not installed)")
        return

    conn = None
    try:
        conn = psycopg2.connect(
            host=config.db.host,
            port=config.db.port,
            dbname=config.db.database,
            user=config.db.user,
            password=config.db.password,
            connect_timeout=10,
        )
        with conn.cursor() as cursor:
            cursor.execute(config.db.order_status_query, (order_id,))
            row = cursor.fetchone()

        if not row:
            raise AssertionError(f"No DB record found for order_id={order_id}")

        status = str(row[0])
        if expected_status_contains not in status:
            raise AssertionError(
                f"DB status '{status}' did not contain '{expected_status_contains}'"
            )
    finally:
        if conn is not None:
            conn.close()


class TestCheckoutPayment:
    """Automation scenarios tagged as A in the EP-31 sheet."""

    def test_tc_01_happy_path_payment_success(
        self,
        page: Page,
        config: TestConfig,
    ) -> None:
        try:
            _login(page, config)
            _start_checkout(page, config)
            _pay_by_card(page, config, config.payment.valid_card)

            page.wait_for_selector(
                _selector(config, "order_confirmation"),
                timeout=60000,
            )
            status_text = page.inner_text(_selector(config, "payment_status"))
            assert config.payment.success_text in status_text

            order_id = _try_extract_order_id(page, config)
            if order_id:
                _validate_order_status_via_api(
                    config,
                    order_id,
                    expected_status_contains=config.payment.success_text,
                )
                _validate_payment_status_in_db(
                    config,
                    order_id,
                    expected_status_contains=config.payment.success_text,
                )
            else:
                LOGGER.info("order_id selector not configured; skipping API/DB checks")
        except Exception as exc:
            shot = _safe_screenshot(page, "tc_01_failure")
            pytest.fail(f"TC_01 failed: {exc}. Screenshot: {shot}")

    def test_tc_02_invalid_card_rejected(
        self,
        page: Page,
        config: TestConfig,
    ) -> None:
        try:
            _login(page, config)
            _start_checkout(page, config)
            _pay_by_card(page, config, config.payment.invalid_card)

            page.wait_for_selector(
                _selector(config, "payment_error"),
                timeout=60000,
            )
            assert page.is_visible(_selector(config, "payment_error"))

            confirmation_selector = _selector(config, "order_confirmation")
            assert not page.is_visible(confirmation_selector)
        except Exception as exc:
            shot = _safe_screenshot(page, "tc_02_failure")
            pytest.fail(f"TC_02 failed: {exc}. Screenshot: {shot}")

    def test_tc_03_gateway_timeout_shows_pending_then_failure(
        self,
        page: Page,
        config: TestConfig,
    ) -> None:
        try:
            delay_ms = config.payment.gateway_timeout.delay_ms
            url_pattern = config.payment.gateway_timeout.url_pattern

            def _delay_route(route, request):  # type: ignore[no-untyped-def]
                LOGGER.info(
                    "Delaying gateway request %s by %sms",
                    request.url,
                    delay_ms,
                )
                time.sleep(delay_ms / 1000)
                route.continue_()

            page.route(url_pattern, _delay_route)

            _login(page, config)
            _start_checkout(page, config)
            _pay_by_wallet(page, config)

            page.wait_for_selector(_selector(config, "payment_status"), timeout=60000)
            status_initial = page.inner_text(_selector(config, "payment_status"))
            assert config.payment.gateway_timeout.pending_text in status_initial

            page.wait_for_function(
                (
                    "(sel, finalText) => document.querySelector(sel)"
                    "?.innerText?.includes(finalText)"
                ),
                arg=(
                    _selector(config, "payment_status"),
                    config.payment.gateway_timeout.final_failure_text,
                ),
                timeout=max(120000, delay_ms + 60000),
            )
        except Exception as exc:
            shot = _safe_screenshot(page, "tc_03_failure")
            pytest.fail(f"TC_03 failed: {exc}. Screenshot: {shot}")
