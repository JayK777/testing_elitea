"""E2E payment automation (Playwright) for Automation-tagged scenarios.

Covers:
- TC_01: Successful card payment (happy path)
- TC_02: Failed card payment shows clear error and order is not marked paid
- TC_04: Secure payment transmission over gateway integration

Usage:
  pytest -q e2e_pipeline_v4/test_payments_e2e.py

Configuration:
  Update e2e_pipeline_v4/test_data.json and/or set environment variables.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import requests
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent
TEST_DATA_PATH = PROJECT_ROOT / "test_data.json"


class TestDataError(RuntimeError):
    """Raised when test data is missing or invalid."""


def _load_test_data() -> Dict[str, Any]:
    if not TEST_DATA_PATH.exists():
        raise TestDataError(f"Missing test data file: {TEST_DATA_PATH}")

    try:
        data = json.loads(TEST_DATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TestDataError("test_data.json is not valid JSON") from exc

    if not isinstance(data, dict) or not data.get("base_url"):
        raise TestDataError("test_data.json must contain at least 'base_url'")

    return data


@dataclass(frozen=True)
class GatewaySecurityFinding:
    url: str
    reason: str


class GatewaySecurityMonitor:
    """Captures network requests and evaluates basic security rules."""

    def __init__(
        self,
        filter_domains: List[str],
        sensitive_values: List[str],
        allow_insecure: bool,
    ) -> None:
        self._filter_domains = [d.lower() for d in filter_domains]
        self._sensitive_values = [v for v in sensitive_values if v]
        self._allow_insecure = allow_insecure
        self._seen_requests: List[str] = []

    def attach(self, page: Page) -> None:
        page.on("request", lambda req: self._seen_requests.append(req.url))

    def evaluate(self) -> List[GatewaySecurityFinding]:
        findings: List[GatewaySecurityFinding] = []

        for url in self._seen_requests:
            url_l = url.lower()
            if self._filter_domains and not any(d in url_l for d in self._filter_domains):
                continue

            if not self._allow_insecure and url_l.startswith("http://"):
                findings.append(
                    GatewaySecurityFinding(url=url, reason="Insecure HTTP request detected")
                )

            # Basic leakage checks: sensitive values should not appear in URL.
            for val in self._sensitive_values:
                if val and val in url:
                    findings.append(
                        GatewaySecurityFinding(
                            url=url,
                            reason="Sensitive value appears in URL (query/path)",
                        )
                    )

            # Heuristic for common sensitive parameter names.
            if re.search(r"(card|cvv|cvc|pan)=", url_l):
                findings.append(
                    GatewaySecurityFinding(
                        url=url,
                        reason="Sensitive parameter name detected in URL",
                    )
                )

        return findings


class PaymentApp:
    """Minimal app driver. Selectors are provided via test_data.json."""

    def __init__(self, page: Page, base_url: str, selectors: Dict[str, str]) -> None:
        self._page = page
        self._base_url = base_url.rstrip("/")
        self._s = selectors

    def goto_base(self) -> None:
        self._page.goto(self._base_url, wait_until="domcontentloaded")

    def login(self, username: str, password: str) -> None:
        self._page.fill(self._s["username_input"], username)
        self._page.fill(self._s["password_input"], password)
        self._page.click(self._s["login_button"])

    def open_checkout(self) -> None:
        self._page.click(self._s["cart_button"])
        self._page.click(self._s["checkout_button"])

    def pay_by_card(self, number: str, expiry: str, cvv: str) -> None:
        self._page.click(self._s["payment_method_card"])
        self._page.fill(self._s["card_number_input"], number)
        self._page.fill(self._s["card_expiry_input"], expiry)
        self._page.fill(self._s["card_cvv_input"], cvv)
        self._page.click(self._s["pay_now_button"])

    def read_order_reference(self) -> Optional[str]:
        try:
            locator = self._page.locator(self._s["order_reference"])
            if locator.count() == 0:
                return None
            text = locator.first.inner_text().strip()
            return text or None
        except Exception:
            return None


# === FIXTURES PLACEHOLDER ===


# === TESTS PLACEHOLDER ===
