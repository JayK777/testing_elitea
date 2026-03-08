"""E2E payment pipeline automation for scenarios tagged as Automation (A).

Covers:
- TC_01: Happy path success with real-time status and secure gateway transmission.
- TC_03: Pending -> final status transition.
- TC_04: Failure with clear, informative error message.

Tech:
- Playwright (UI)
- requests (API, optional)
- PostgreSQL (optional, best-effort)

Execution:
  pip install pytest playwright requests
  playwright install
  pytest -q

Configuration:
- Update selectors and URLs in test_data.json
- Optionally set environment variables:
  BASE_URL, HEADLESS, API_BASE_URL, DB_DSN
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiConfig:
    stub_config_endpoint: str
    payment_status_endpoint: str
    timeout_seconds: int
    poll_interval_seconds: int


@dataclass(frozen=True)
class DbConfig:
    dsn: str
    payments_status_query: str


@dataclass(frozen=True)
class GatewayConfig:
    request_url_substring: str
    sensitive_query_param_keys: List[str]


@dataclass(frozen=True)
class AuthSelectors:
    username_input: str
    password_input: str
    submit_button: str


@dataclass(frozen=True)
class AuthConfig:
    login_path: str
    username: str
    password: str
    selectors: AuthSelectors


@dataclass(frozen=True)
class CheckoutSelectors:
    payment_method_open: str
    payment_method_card: str
    payment_method_wallet: str
    payment_method_net_banking: str
    card_number: str
    card_expiry: str
    card_cvv: str
    card_name: str
    pay_button: str
    payment_status: str
    payment_error: str
    order_id: str


@dataclass(frozen=True)
class CheckoutConfig:
    selectors: CheckoutSelectors
    expected_error_substrings: List[str]


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    checkout_path: str


@dataclass(frozen=True)
class CardData:
    number: str
    expiry: str
    cvv: str
    name: str


@dataclass(frozen=True)
class PaymentData:
    valid_card: CardData
    invalid_card: CardData


@dataclass(frozen=True)
class TestConfig:
    app: AppConfig
    auth: AuthConfig
    checkout: CheckoutConfig
    gateway: GatewayConfig
    payment_data: PaymentData
    api: ApiConfig
    db: DbConfig


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def load_test_config() -> TestConfig:
    """Load config from test_data.json with environment variable overrides."""

    raw = _load_json(os.path.join(os.path.dirname(__file__), "test_data.json"))

    base_url = os.getenv("BASE_URL", raw["app"]["base_url"]).rstrip("/")
    api_base_url = os.getenv("API_BASE_URL", "").rstrip("/")
    db_dsn = os.getenv("DB_DSN", raw.get("db", {}).get("dsn", ""))

    auth_sel = AuthSelectors(**raw["auth"]["selectors"])
    checkout_sel = CheckoutSelectors(**raw["checkout"]["selectors"])

    api_raw = raw.get("api", {})
    stub_endpoint = api_raw.get("stub_config_endpoint", "")
    status_endpoint = api_raw.get("payment_status_endpoint", "")

    if api_base_url:
        if stub_endpoint and stub_endpoint.startswith("/"):
            stub_endpoint = f"{api_base_url}{stub_endpoint}"
        if status_endpoint and status_endpoint.startswith("/"):
            status_endpoint = f"{api_base_url}{status_endpoint}"

    return TestConfig(
        app=AppConfig(base_url=base_url, checkout_path=raw["app"]["checkout_path"]),
        auth=AuthConfig(
            login_path=raw["auth"]["login_path"],
            username=raw["auth"]["username"],
            password=raw["auth"]["password"],
            selectors=auth_sel,
        ),
        checkout=CheckoutConfig(
            selectors=checkout_sel,
            expected_error_substrings=raw["checkout"].get("expected_error_substrings", []),
        ),
        gateway=GatewayConfig(
            request_url_substring=raw["gateway"]["request_url_substring"],
            sensitive_query_param_keys=raw["gateway"].get("sensitive_query_param_keys", []),
        ),
        payment_data=PaymentData(
            valid_card=CardData(**raw["payment_data"]["valid_card"]),
            invalid_card=CardData(**raw["payment_data"]["invalid_card"]),
        ),
        api=ApiConfig(
            stub_config_endpoint=stub_endpoint,
            payment_status_endpoint=status_endpoint,
            timeout_seconds=int(api_raw.get("timeout_seconds", 90)),
            poll_interval_seconds=int(api_raw.get("poll_interval_seconds", 3)),
        ),
        db=DbConfig(
            dsn=db_dsn,
            payments_status_query=raw.get("db", {}).get(
                "payments_status_query",
                "SELECT status FROM payments WHERE order_id = %s ORDER BY created_at DESC LIMIT 1",
            ),
        ),
    )


@pytest.fixture(scope="session", autouse=True)
def _session_setup() -> None:
    _configure_logging()


@pytest.fixture(scope="session")
def config() -> TestConfig:
    return load_test_config()


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Browser:
    headless = os.getenv("HEADLESS", "true").lower() != "false"
    browser = playwright_instance.chromium.launch(headless=headless)
    yield browser
    browser.close()


@pytest.fixture()
def page(browser: Browser) -> Page:
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    yield page
    context.close()


def _artifact_name(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{ts}.png"


def _safe_screenshot(page: Page, name_prefix: str) -> Optional[str]:
    try:
        name = _artifact_name(name_prefix)
        page.screenshot(path=name, full_page=True)
        LOGGER.info("Saved screenshot: %s", name)
        return name
    except Exception:  # noqa: BLE001
        LOGGER.exception("Failed to capture screenshot")
        return None


def login(page: Page, cfg: TestConfig) -> None:
    url = f"{cfg.app.base_url}{cfg.auth.login_path}"
    LOGGER.info("Navigating to login: %s", url)
    page.goto(url, wait_until="domcontentloaded")

    page.locator(cfg.auth.selectors.username_input).fill(cfg.auth.username)
    page.locator(cfg.auth.selectors.password_input).fill(cfg.auth.password)
    page.locator(cfg.auth.selectors.submit_button).click()


def open_checkout(page: Page, cfg: TestConfig) -> None:
    url = f"{cfg.app.base_url}{cfg.app.checkout_path}"
    LOGGER.info("Navigating to checkout: %s", url)
    page.goto(url, wait_until="domcontentloaded")


def select_card_payment(page: Page, cfg: TestConfig) -> None:
    sel = cfg.checkout.selectors
    page.locator(sel.payment_method_open).click()
    page.locator(sel.payment_method_card).click()


def fill_card_details(page: Page, cfg: TestConfig, card: CardData) -> None:
    sel = cfg.checkout.selectors
    page.locator(sel.card_number).fill(card.number)
    page.locator(sel.card_expiry).fill(card.expiry)
    page.locator(sel.card_cvv).fill(card.cvv)
    page.locator(sel.card_name).fill(card.name)


def click_pay(page: Page, cfg: TestConfig) -> None:
    page.locator(cfg.checkout.selectors.pay_button).click()


def _assert_gateway_request_is_secure(cfg: TestConfig, request_url: str) -> None:
    parsed = urlparse(request_url)
    assert parsed.scheme == "https", f"Gateway request is not HTTPS: {request_url}"

    query = parse_qs(parsed.query)
    forbidden = {k.lower() for k in cfg.gateway.sensitive_query_param_keys}
    leaked = [k for k in query.keys() if k.lower() in forbidden]
    assert not leaked, f"Sensitive data present in query params: {leaked}"


def capture_gateway_requests(page: Page, cfg: TestConfig) -> List[str]:
    """Capture gateway request URLs observed during the test run."""

    captured: List[str] = []

    def _on_request(req: Any) -> None:
        try:
            if cfg.gateway.request_url_substring in req.url:
                captured.append(req.url)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Error while capturing request")

    page.on("request", _on_request)
    return captured


def wait_for_status_text(page: Page, selector: str, expected: str, timeout_ms: int) -> None:
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        try:
            text = page.locator(selector).inner_text(timeout=1000).strip().lower()
            if expected.lower() in text:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)

    actual = ""
    try:
        actual = page.locator(selector).inner_text(timeout=1000).strip()
    except Exception:  # noqa: BLE001
        actual = "<unavailable>"
    raise AssertionError(f"Timed out waiting for status '{expected}'. Last seen: '{actual}'")


def wait_for_any_status_text(
    page: Page,
    selector: str,
    expected_any: List[str],
    timeout_ms: int,
) -> str:
    """Wait until the status element contains any of the expected strings.

    Returns the matched expected string.
    """

    last_error: Optional[Exception] = None
    for expected in expected_any:
        try:
            wait_for_status_text(page, selector=selector, expected=expected, timeout_ms=timeout_ms)
            return expected
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    raise AssertionError(f"None of {expected_any} appeared in status within timeout") from last_error


def maybe_configure_gateway_stub(cfg: TestConfig, scenario: str) -> None:
    """Optional: configure gateway sandbox/stub to a given scenario via API."""

    if not cfg.api.stub_config_endpoint:
        LOGGER.info("No stub_config_endpoint configured; skipping stub configuration")
        return

    payload = {"scenario": scenario}
    try:
        resp = requests.post(cfg.api.stub_config_endpoint, json=payload, timeout=15)
        resp.raise_for_status()
        LOGGER.info("Configured gateway stub scenario '%s' via %s", scenario, cfg.api.stub_config_endpoint)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"Failed to configure gateway stub: {exc}") from exc


def maybe_poll_payment_status_api(cfg: TestConfig, order_id: str) -> Optional[str]:
    """Optional: poll payment status from API until terminal state."""

    if not cfg.api.payment_status_endpoint:
        return None

    deadline = time.time() + cfg.api.timeout_seconds
    last_status: Optional[str] = None

    while time.time() < deadline:
        try:
            resp = requests.get(
                cfg.api.payment_status_endpoint,
                params={"order_id": order_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            last_status = str(data.get("status", "")).strip()

            if last_status.lower() in {"success", "failure", "failed", "cancelled", "refunded"}:
                return last_status
        except Exception:  # noqa: BLE001
            LOGGER.exception("Payment status API poll failed")

        time.sleep(cfg.api.poll_interval_seconds)

    return last_status


def maybe_fetch_db_payment_status(cfg: TestConfig, order_id: str) -> Optional[str]:
    """Optional: read payment status from Postgres if DB_DSN is configured and driver exists."""

    if not cfg.db.dsn:
        return None

    try:
        import psycopg

        with psycopg.connect(cfg.db.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(cfg.db.payments_status_query, (order_id,))
                row = cur.fetchone()
                return str(row[0]) if row else None
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        LOGGER.exception("DB validation failed using psycopg")
        return None

    try:
        import psycopg2

        conn = psycopg2.connect(cfg.db.dsn)
        try:
            cur = conn.cursor()
            cur.execute(cfg.db.payments_status_query, (order_id,))
            row = cur.fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()
    except ImportError:
        LOGGER.warning("psycopg/psycopg2 not installed; skipping DB validation")
        return None
    except Exception:  # noqa: BLE001
        LOGGER.exception("DB validation failed using psycopg2")
        return None


def _get_order_id_if_present(page: Page, cfg: TestConfig) -> Optional[str]:
    try:
        text = page.locator(cfg.checkout.selectors.order_id).inner_text(timeout=2000).strip()
        return text or None
    except Exception:  # noqa: BLE001
        return None


class TestPaymentStatusFlow:
    """TC_01 + TC_03: Success and Pending->Final transitions."""

    def test_tc01_happy_path_success_with_secure_gateway(self, page: Page, config: TestConfig) -> None:
        captured = capture_gateway_requests(page, config)

        try:
            login(page, config)
            open_checkout(page, config)

            select_card_payment(page, config)
            fill_card_details(page, config, config.payment_data.valid_card)

            click_pay(page, config)

            # Real-time status (best-effort): look for Processing or Pending soon after Pay.
            wait_for_any_status_text(
                page,
                selector=config.checkout.selectors.payment_status,
                expected_any=["processing", "pending"],
                timeout_ms=15_000,
            )

            wait_for_status_text(
                page,
                selector=config.checkout.selectors.payment_status,
                expected="success",
                timeout_ms=config.api.timeout_seconds * 1000,
            )

            assert captured, (
                "No gateway-like requests captured. Update gateway.request_url_substring in test_data.json "
                "to match your gateway host/path."
            )

            for url in captured:
                _assert_gateway_request_is_secure(config, url)

            order_id = _get_order_id_if_present(page, config)
            if order_id:
                api_status = maybe_poll_payment_status_api(config, order_id)
                if api_status:
                    assert api_status.lower() == "success"

                db_status = maybe_fetch_db_payment_status(config, order_id)
                if db_status:
                    assert db_status.lower() == "success"

        except Exception:  # noqa: BLE001
            _safe_screenshot(page, "tc01_failure")
            raise

    def test_tc03_pending_then_final_status(self, page: Page, config: TestConfig) -> None:
        """Requires gateway sandbox/stub to return Pending then Success/Failure."""

        try:
            maybe_configure_gateway_stub(config, scenario="pending_then_success")

            login(page, config)
            open_checkout(page, config)

            select_card_payment(page, config)
            fill_card_details(page, config, config.payment_data.valid_card)
            click_pay(page, config)

            wait_for_status_text(
                page,
                selector=config.checkout.selectors.payment_status,
                expected="pending",
                timeout_ms=40_000,
            )

            wait_for_status_text(
                page,
                selector=config.checkout.selectors.payment_status,
                expected="success",
                timeout_ms=config.api.timeout_seconds * 1000,
            )

            order_id = _get_order_id_if_present(page, config)
            if order_id:
                api_status = maybe_poll_payment_status_api(config, order_id)
                if api_status:
                    assert api_status.lower() in {"success", "failure", "failed"}

        except Exception:  # noqa: BLE001
            _safe_screenshot(page, "tc03_failure")
            raise


class TestPaymentFailure:
    """TC_04: Failure state and clear error messaging."""

    def test_tc04_payment_failure_shows_informative_error(self, page: Page, config: TestConfig) -> None:
        try:
            maybe_configure_gateway_stub(config, scenario="failure")

            login(page, config)
            open_checkout(page, config)

            select_card_payment(page, config)
            fill_card_details(page, config, config.payment_data.invalid_card)
            click_pay(page, config)

            wait_for_status_text(
                page,
                selector=config.checkout.selectors.payment_status,
                expected="fail",
                timeout_ms=config.api.timeout_seconds * 1000,
            )

            error_text = ""
            try:
                error_text = page.locator(config.checkout.selectors.payment_error).inner_text(timeout=10_000).strip()
            except Exception:  # noqa: BLE001
                error_text = ""

            assert error_text, "Expected an error message on failure, but none was found."

            lowered = error_text.lower()
            expected_substrings = [s.lower() for s in config.checkout.expected_error_substrings]
            assert any(s in lowered for s in expected_substrings), (
                "Error message is not informative enough. "
                f"Actual: '{error_text}'. Expected to contain one of: {expected_substrings}"
            )

        except Exception:  # noqa: BLE001
            _safe_screenshot(page, "tc04_failure")
            raise
