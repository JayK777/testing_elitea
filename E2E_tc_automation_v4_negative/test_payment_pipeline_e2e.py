"""E2E payment pipeline automation for EP-35.

Covers Automation (A) test cases from pipeline_testcase_v2.xlsx:
- TC_01: Successful card payment (happy path)
- TC_02: Invalid/expired card details (negative)
- TC_04: Refund/Cancellation updates status + user notification

Tech stack:
- Python + pytest
- Playwright (web UI)
- requests (API verification)
- PostgreSQL (optional DB verification)

Execution (example):
  pytest -q E2E_tc_automation_v4_negative/test_payment_pipeline_e2e.py

Configuration:
- Update E2E_tc_automation_v4_negative/test_data.json
- Optional overrides via env vars:
  - TEST_DATA_JSON: JSON string merged on top of test_data.json
  - API_TOKEN: bearer token override

Notes:
- Selectors and URLs are placeholders and must match your AUT.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest
import requests
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


LOGGER = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    timeout_seconds: int
    get_order_path: str
    cancel_order_path: str
    bearer_token: str


@dataclass(frozen=True)
class DbConfig:
    enabled: bool
    host: str
    port: int
    database: str
    user: str
    password: str
    payment_status_query: str


@dataclass(frozen=True)
class WebConfig:
    base_url: str
    username: str
    password: str
    selectors: Dict[str, str]
    card_iframe_selector: Optional[str]
    card_selectors: Dict[str, str]
    timeouts: Dict[str, int]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class PaymentUi:
    def __init__(self, page: Page, config: WebConfig) -> None:
        self._page = page
        self._config = config

    def login(self) -> None:
        selectors = self._config.selectors
        self._page.goto(self._config.base_url, timeout=self._config.timeouts["page_navigation_ms"])
        self._page.fill(selectors["username_input"], self._config.username)
        self._page.fill(selectors["password_input"], self._config.password)
        self._page.click(selectors["submit_button"])

    def open_checkout(self) -> None:
        self._page.click(self._config.selectors["go_to_checkout_button"])

    def select_card_payment(self) -> None:
        self._page.click(self._config.selectors["payment_method_card_radio"])

    def enter_card_details(self, card: Dict[str, str]) -> None:
        iframe_selector = self._config.card_iframe_selector
        selectors = self._config.card_selectors

        if iframe_selector:
            frame = self._page.frame_locator(iframe_selector)
            frame.locator(selectors["card_number"]).fill(card["number"])
            frame.locator(selectors["expiry"]).fill(card["expiry"])
            frame.locator(selectors["cvv"]).fill(card["cvv"])
            frame.locator(selectors["name"]).fill(card["name"])
            return

        self._page.fill(selectors["card_number"], card["number"])
        self._page.fill(selectors["expiry"], card["expiry"])
        self._page.fill(selectors["cvv"], card["cvv"])
        self._page.fill(selectors["name"], card["name"])

    def submit_payment(self) -> None:
        self._page.click(self._config.selectors["place_order_button"])

    def wait_for_payment_success(self) -> None:
        self._page.wait_for_selector(
            self._config.selectors["payment_success_message"],
            timeout=self._config.timeouts["payment_processing_ms"],
        )

    def read_order_id(self) -> str:
        locator = self._page.locator(self._config.selectors["order_id_value"])
        locator.wait_for(timeout=self._config.timeouts["payment_processing_ms"])
        order_id = locator.inner_text().strip()
        if not order_id:
            raise AssertionError("Order id element resolved but was empty")
        return order_id

    def wait_for_payment_error(self) -> str:
        locator = self._page.locator(self._config.selectors["payment_error_message"])
        locator.wait_for(timeout=self._config.timeouts["payment_processing_ms"])
        return locator.inner_text().strip()


class BackendVerifier:
    def __init__(self, api_config: ApiConfig, db_config: DbConfig) -> None:
        self._api_config = api_config
        self._db_config = db_config

    def get_order(self, order_id: str) -> Dict[str, Any]:
        url = self._api_config.base_url.rstrip("/") + self._api_config.get_order_path.format(order_id=order_id)
        headers = {}
        if self._api_config.bearer_token:
            headers["Authorization"] = f"Bearer {self._api_config.bearer_token}"

        response = requests.get(url, headers=headers, timeout=self._api_config.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        url = (
            self._api_config.base_url.rstrip("/")
            + self._api_config.cancel_order_path.format(order_id=order_id)
        )
        headers = {}
        if self._api_config.bearer_token:
            headers["Authorization"] = f"Bearer {self._api_config.bearer_token}"

        response = requests.post(url, headers=headers, timeout=self._api_config.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def get_payment_status_from_db(self, order_id: str) -> Optional[str]:
        if not self._db_config.enabled:
            return None

        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "psycopg2 is required for DB verification. "
                "Install it or set db.enabled=false in test_data.json."
            ) from exc

        conn = None
        try:
            conn = psycopg2.connect(
                host=self._db_config.host,
                port=self._db_config.port,
                dbname=self._db_config.database,
                user=self._db_config.user,
                password=self._db_config.password,
                connect_timeout=10,
            )
            with conn.cursor() as cur:
                cur.execute(self._db_config.payment_status_query, (order_id,))
                row = cur.fetchone()
                return str(row[0]) if row else None
        finally:
            if conn is not None:
                conn.close()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_test_data() -> Dict[str, Any]:
    data_path = os.path.join(os.path.dirname(__file__), "test_data.json")
    with open(data_path, "r", encoding="utf-8") as handle:
        base_data = json.load(handle)

    override_raw = os.getenv("TEST_DATA_JSON", "").strip()
    if not override_raw:
        return base_data

    try:
        override_data = json.loads(override_raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("TEST_DATA_JSON is not valid JSON") from exc

    return _deep_merge(base_data, override_data)


def _build_configs(raw: Dict[str, Any]) -> tuple[WebConfig, ApiConfig, DbConfig]:
    web = raw.get("web", {})
    api = raw.get("api", {})
    db = raw.get("db", {})
    timeouts = raw.get("timeouts", {})

    web_login = web.get("login", {})
    web_checkout = web.get("checkout", {})

    login_selectors = (web_login.get("selectors") or {}).copy()
    checkout_selectors = (web_checkout.get("selectors") or {}).copy()
    selectors = {**login_selectors, **checkout_selectors}

    required_selectors = [
        "username_input",
        "password_input",
        "submit_button",
        "go_to_checkout_button",
        "payment_method_card_radio",
        "place_order_button",
        "payment_success_message",
        "payment_error_message",
        "order_id_value",
    ]
    missing = [name for name in required_selectors if not selectors.get(name)]
    if missing:
        raise ConfigError(f"Missing required selectors in test_data.json: {missing}")

    card_form = (web_checkout.get("card_form") or {})
    card_iframe_selector = card_form.get("iframe_selector")
    card_selectors = (card_form.get("selectors") or {}).copy()

    required_card_selectors = ["card_number", "expiry", "cvv", "name"]
    missing_card = [name for name in required_card_selectors if not card_selectors.get(name)]
    if missing_card:
        raise ConfigError(f"Missing required card selectors in test_data.json: {missing_card}")

    web_config = WebConfig(
        base_url=str(web.get("base_url", "")).strip(),
        username=str(web_login.get("username", "")).strip(),
        password=str(web_login.get("password", "")).strip(),
        selectors=selectors,
        card_iframe_selector=card_iframe_selector,
        card_selectors=card_selectors,
        timeouts={
            "page_navigation_ms": int(timeouts.get("page_navigation_ms", 30000)),
            "payment_processing_ms": int(timeouts.get("payment_processing_ms", 60000)),
        },
    )

    if not web_config.base_url or not web_config.username or not web_config.password:
        raise ConfigError("web.base_url and web.login.(username/password) must be set")

    bearer_token = os.getenv("API_TOKEN", "").strip() or str((api.get("auth") or {}).get("token", "")).strip()
    endpoints = api.get("endpoints") or {}
    api_config = ApiConfig(
        base_url=str(api.get("base_url", "")).strip(),
        timeout_seconds=int(api.get("timeout_seconds", 20)),
        get_order_path=str(endpoints.get("get_order", "/orders/{order_id}")),
        cancel_order_path=str(endpoints.get("cancel_order", "/orders/{order_id}/cancel")),
        bearer_token=bearer_token,
    )

    db_config = DbConfig(
        enabled=bool(db.get("enabled", False)),
        host=str(db.get("host", "localhost")),
        port=int(db.get("port", 5432)),
        database=str(db.get("database", "")),
        user=str(db.get("user", "")),
        password=str(db.get("password", "")),
        payment_status_query=str(db.get("payment_status_query", "")),
    )

    return web_config, api_config, db_config


@pytest.fixture(scope="session")
def raw_test_data() -> Dict[str, Any]:
    return _load_test_data()


@pytest.fixture(scope="session")
def configs(raw_test_data: Dict[str, Any]) -> tuple[WebConfig, ApiConfig, DbConfig]:
    return _build_configs(raw_test_data)


@pytest.fixture(scope="session")
def web_config(configs: tuple[WebConfig, ApiConfig, DbConfig]) -> WebConfig:
    return configs[0]


@pytest.fixture(scope="session")
def backend_verifier(configs: tuple[WebConfig, ApiConfig, DbConfig]) -> BackendVerifier:
    _, api_config, db_config = configs
    return BackendVerifier(api_config=api_config, db_config=db_config)


@pytest.fixture(scope="session")
def playwright_instance() -> Playwright:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Browser:
    browser = playwright_instance.chromium.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture(scope="function")
def page(browser: Browser, request: pytest.FixtureRequest) -> Page:
    context = browser.new_context()
    page = context.new_page()
    request.node._page = page  # type: ignore[attr-defined]
    yield page
    context.close()


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or report.passed:
        return

    page_obj = getattr(item, "_page", None)
    if page_obj is None:
        return

    timestamp = int(time.time())
    safe_nodeid = report.nodeid.replace("/", "_").replace(":", "_")
    screenshot_path = f"playwright_failure_{safe_nodeid}_{timestamp}.png"

    try:
        page_obj.screenshot(path=screenshot_path, full_page=True)
        LOGGER.error("Saved failure screenshot: %s", screenshot_path)
    except Exception:
        LOGGER.exception("Failed to capture screenshot")


def _assert_contains(haystack: str, needles: list[str], context: str) -> None:
    haystack_norm = (haystack or "").lower()
    if not any(needle.lower() in haystack_norm for needle in needles):
        raise AssertionError(f"{context}. Expected one of {needles}, got: {haystack!r}")


def _create_paid_order(
    page: Page,
    web_config: WebConfig,
    card: Dict[str, str],
) -> str:
    ui = PaymentUi(page=page, config=web_config)
    ui.login()
    ui.open_checkout()
    ui.select_card_payment()
    ui.enter_card_details(card)
    ui.submit_payment()
    ui.wait_for_payment_success()
    return ui.read_order_id()
