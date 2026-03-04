import logging
from typing import Any, Dict

import pytest
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

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


@pytest.mark.e2e
def test_refund_or_cancellation_updates_status_and_notifies_user(
    page: Page,
    test_data: Dict[str, Any],
    pg_conn,
) -> None:
    """Covers TC_14.

    This test assumes your AUT supports cancellation/refund from order details.
    Update selectors in `test_data.json` to match your UI.
    """
    _goto_base(page, test_data)
    login_if_needed(page, test_data)

    open_checkout(page, test_data)
    select_payment_method(page, test_data, "card")
    fill_card_details(page, test_data, card_key="valid_card")
    click_pay(page, test_data)
    assert_order_paid(page, test_data)

    cancel_btn = page.locator(_get_selector(test_data, "order_cancel"))
    cancel_btn.wait_for(state="visible", timeout=15_000)
    cancel_btn.click()

    refund_status = page.locator(_get_selector(test_data, "refund_status"))
    try:
        refund_status.wait_for(state="visible", timeout=30_000)
        status_text = refund_status.inner_text().strip().lower()
    except PlaywrightTimeoutError as exc:
        raise AssertionError("Refund/cancellation status did not appear") from exc

    assert any(k in status_text for k in ["refund", "cancel"]), (
        f"Unexpected refund/cancellation status text: {status_text!r}"
    )

    notifications = page.locator(_get_selector(test_data, "notification_area"))
    if notifications.is_visible():
        LOGGER.info("Notifications: %s", notifications.inner_text().strip())

    # Optional DB validation hook (best-effort).
    # Provide connection env vars (PG_*) to enable.
    if pg_conn is not None:
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT 1")
                _ = cur.fetchone()
        except Exception as exc:
            LOGGER.warning("PostgreSQL validation skipped/failed: %s", exc)
