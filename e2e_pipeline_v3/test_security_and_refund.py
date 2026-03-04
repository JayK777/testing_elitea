"""Automation scenarios (tagged A) derived from pipeline_testcase.xlsx.

Coverage in this module:
- TC_12 Secure transmission: HTTPS & no sensitive data in URL/query params
- TC_13 Refund requested for an already refunded/cancelled order (negative)

Notes:
- TC_12 uses Playwright request event capture and pattern matching.
- TC_13 is implemented as an API-level negative test using `requests`.
  If API endpoints or a refunded order id are not configured, it will be skipped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import requests
from playwright.sync_api import Page, sync_playwright


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    endpoints: Dict[str, str]


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


@pytest.fixture(scope="session")
def api_config(test_data: Dict[str, Any]) -> ApiConfig:
    api = _require(test_data, "api")
    return ApiConfig(base_url=_require(api, "base_url"), endpoints=_require(api, "endpoints"))


def _skip_if_missing_web_prereqs(test_data: Dict[str, Any]) -> None:
    try:
        _require(test_data, "web.base_url")
        _require(test_data, "web.selectors")
        _require(test_data, "web.credentials.username")
        _require(test_data, "web.credentials.password")
        _require(test_data, "web.security.payment_request_url_patterns")
    except KeyError as exc:
        pytest.skip(str(exc))


def _login(page: Page, test_data: Dict[str, Any]) -> None:
    web = _require(test_data, "web")
    selectors = _require(web, "selectors")
    creds = _require(web, "credentials")

    page.goto(_require(web, "base_url"), wait_until="domcontentloaded")
    page.fill(_require(selectors, "login_username"), str(_require(creds, "username")))
    page.fill(_require(selectors, "login_password"), str(_require(creds, "password")))
    page.click(_require(selectors, "login_submit"))


def _prepare_checkout(page: Page, test_data: Dict[str, Any]) -> None:
    selectors = _require(test_data, "web.selectors")
    page.click(_require(selectors, "add_to_cart"))
    page.click(_require(selectors, "cart_checkout"))


def _attempt_payment_to_generate_requests(page: Page, test_data: Dict[str, Any]) -> None:
    """Best-effort action to trigger payment-related network requests."""

    selectors = _require(test_data, "web.selectors")

    if "payment_method_card" in selectors:
        page.click(_require(selectors, "payment_method_card"))

        card = _maybe_get(test_data, "web.payment.card.valid")
        fields = _maybe_get(test_data, "web.payment.card.fields")
        if card and fields:
            page.fill(_require(fields, "number"), str(_require(card, "number")))
            page.fill(_require(fields, "expiry_month"), str(_require(card, "expiry_month")))
            page.fill(_require(fields, "expiry_year"), str(_require(card, "expiry_year")))
            page.fill(_require(fields, "cvv"), str(_require(card, "cvv")))
            page.fill(_require(fields, "name"), str(_require(card, "name")))

    page.click(_require(selectors, "pay_button"))


def _match_by_substring(url: str, patterns: List[str]) -> bool:
    # Simple best-effort match for both wildcard and plain substring patterns.
    for pattern in patterns:
        token = pattern.replace("**", "").replace("*", "")
        if token and token in url:
            return True
    return False


def test_tc12_secure_transmission_https_and_no_sensitive_data_in_url(
    page: Page,
    test_data: Dict[str, Any],
) -> None:
    """TC_12: payment requests sent over HTTPS and no sensitive data in URL/query params."""

    _skip_if_missing_web_prereqs(test_data)

    patterns = list(_require(test_data, "web.security.payment_request_url_patterns"))
    sensitive_keys = [
        str(k).lower() for k in _maybe_get(test_data, "web.security.sensitive_query_keys", [])
    ]

    captured_urls: List[str] = []

    def _on_request(request: Any) -> None:
        url = str(request.url)
        if _match_by_substring(url, patterns):
            captured_urls.append(url)

    page.on("request", _on_request)

    _login(page, test_data)
    _prepare_checkout(page, test_data)

    _attempt_payment_to_generate_requests(page, test_data)

    if not captured_urls:
        pytest.skip(
            "No payment-related requests were captured. "
            "Update web.security.payment_request_url_patterns to match your app's URLs."
        )

    possible_pan = re.compile(r"\b\d{12,19}\b")

    for url in captured_urls:
        assert url.startswith("https://"), f"Non-HTTPS payment request detected: {url}"

        if "?" in url:
            query = url.split("?", 1)[1].lower()
            for key in sensitive_keys:
                assert f"{key}=" not in query, (
                    "Sensitive data key present in URL query params: "
                    f"key={key!r}, url={url}"
                )

        assert not possible_pan.search(url), f"Possible PAN/card number leaked in URL: {url}"


def _api_get_auth_headers(test_data: Dict[str, Any], api_cfg: ApiConfig) -> Dict[str, str]:
    """Return Authorization headers based on config.

    Supported options:
    - `api.auth.bearer_token`
    - `api.endpoints.login` + `api.auth.username/password` (expects JSON token response)
    """

    token = _maybe_get(test_data, "api.auth.bearer_token")
    if token:
        return {"Authorization": f"Bearer {token}"}

    login_path = api_cfg.endpoints.get("login")
    if not login_path:
        pytest.skip("API auth not configured: set api.auth.bearer_token or api.endpoints.login")

    username = _maybe_get(test_data, "api.auth.username")
    password = _maybe_get(test_data, "api.auth.password")
    if not (username and password):
        pytest.skip("API auth credentials not configured: api.auth.username/password")

    url = f"{api_cfg.base_url}{login_path}"
    resp = requests.post(url, json={"username": username, "password": password}, timeout=20)
    resp.raise_for_status()

    payload = resp.json()
    token = payload.get("access_token") or payload.get("token")
    if not token:
        raise AssertionError("Login response did not contain access_token/token")

    return {"Authorization": f"Bearer {token}"}


def test_tc13_prevent_duplicate_refund_request(
    test_data: Dict[str, Any],
    api_config: ApiConfig,
) -> None:
    """TC_13: Refund requested for an already refunded/cancelled order should be blocked."""

    refunded_order_id = str(
        _maybe_get(test_data, "api.test_entities.already_refunded_order_id", "")
    ).strip()
    if not refunded_order_id:
        pytest.skip("No refunded order configured: api.test_entities.already_refunded_order_id")

    refund_path = api_config.endpoints.get("refund")
    if not refund_path:
        pytest.skip("Refund endpoint not configured: api.endpoints.refund")

    headers = {"Content-Type": "application/json"}
    headers.update(_api_get_auth_headers(test_data, api_config))

    url = f"{api_config.base_url}{refund_path}"
    payload = {"order_id": refunded_order_id}

    resp = requests.post(url, json=payload, headers=headers, timeout=30)

    acceptable_statuses = set(
        _maybe_get(test_data, "api.expected_duplicate_refund_statuses", [400, 409, 422])
    )

    if resp.status_code not in acceptable_statuses:
        body_text = resp.text.lower()
        assert "already" in body_text or "duplicate" in body_text, (
            "Unexpected response for duplicate refund request: "
            f"status={resp.status_code}, body={resp.text[:500]!r}"
        )
