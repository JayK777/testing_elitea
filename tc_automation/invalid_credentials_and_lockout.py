"""
invalid_credentials_and_lockout.py
-----------------------------------
TC_06 - Invalid credentials → generic error (no field disclosure).
TC_07 - Account lockout triggers exactly after 5 consecutive failed attempts.
TC_08 - 4 consecutive failures do NOT lock the account; 5th with correct creds succeeds.
TC_09 - Locked account cannot log in even with correct password within 15-min window.

Uses: Playwright (UI) + requests (API) + PostgreSQL (DB state verification).
Jira: EP-2
"""

import logging
import time

from utils.api_helper import attempt_multiple_logins, create_session, post_login
from utils.browser_helper import fill_login_form, get_visible_text, launch_browser, new_page
from utils.config_loader import load_test_data
from utils.db_helper import get_db_connection, is_account_locked, reset_user_lockout
from utils.reporter import TestReport, run_test

logger = logging.getLogger(__name__)

SUBMIT_SELECTOR = "button[type='submit'], button:has-text('Login'), #login-btn"
ERROR_SELECTOR = ".error-message, [role='alert'], .flash-error, .login-error"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _teardown_unlock_user(config: dict) -> None:
    """Reset user lockout state in the DB after lockout tests."""
    try:
        with get_db_connection(config["db"]) as conn:
            reset_user_lockout(conn, config["valid_user"]["username"])
    except Exception as exc:
        logger.warning("Teardown: could not reset user lockout: %s", exc)


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def tc_06_invalid_credentials_generic_error(config: dict) -> tuple[bool, str]:
    """
    TC_06: Enter invalid credentials and verify:
    1. A generic error message is displayed.
    2. The message does NOT reveal which field was incorrect.
    """
    login_url = config["login_url"]
    username = config["valid_user"]["username"]
    wrong_password = config["wrong_password"]
    expected_msg = config["expected_messages"]["invalid_credentials"].lower()

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            fill_login_form(page, username, wrong_password)
            page.wait_for_timeout(2000)

            error_el = page.query_selector(ERROR_SELECTOR)
            if error_el is None or not error_el.is_visible():
                return False, "No error message displayed for invalid credentials."

            error_text = error_el.inner_text().lower()

            if expected_msg not in error_text:
                return False, f"Error message does not match expected. Got: '{error_text}'"

            # Ensure no field-specific language
            forbidden_phrases = ["username", "email address", "password is wrong",
                                  "incorrect password", "user not found", "no account"]
            for phrase in forbidden_phrases:
                if phrase in error_text:
                    return False, f"Error reveals which field is wrong ('{phrase}'): '{error_text}'"

            return True, f"Generic error shown correctly: '{error_text}'"


def tc_07_lockout_after_five_failures(config: dict) -> tuple[bool, str]:
    """
    TC_07: Attempt login with wrong password 5 times.
    - After 5th attempt: account must be locked and user notified.
    - 6th attempt (even with correct password): still blocked.
    DB state is verified post-lockout and reset during teardown.
    """
    api_url = config["api_login_endpoint"]
    username = config["valid_user"]["username"]
    wrong_password = config["wrong_password"]
    correct_password = config["valid_user"]["password"]
    max_attempts = config["lockout"]["max_attempts"]
    expected_locked_msg = config["expected_messages"]["account_locked"].lower()

    try:
        # Perform 5 failed attempts via API
        responses = attempt_multiple_logins(api_url, username, wrong_password, max_attempts)
        fifth_response = responses[-1]
        fifth_body = fifth_response.text.lower()

        if fifth_response.status_code not in (401, 403, 429):
            return (
                False,
                f"Expected 401/403/429 on 5th attempt, got: {fifth_response.status_code}",
            )

        # Verify lockout message in 5th response
        if expected_locked_msg not in fifth_body:
            return (
                False,
                f"Lockout notification not found in 5th attempt. Body: '{fifth_body[:300]}'",
            )

        # Verify DB state
        with get_db_connection(config["db"]) as conn:
            locked = is_account_locked(conn, username)
        if not locked:
            return False, "DB shows account is NOT locked after 5 failed attempts."

        # 6th attempt with correct password should still be blocked
        with create_session() as session:
            sixth_response = post_login(session, api_url, username, correct_password)
        if sixth_response.status_code not in (401, 403, 429):
            return (
                False,
                f"6th attempt (correct creds) was NOT blocked during lockout. "
                f"Status: {sixth_response.status_code}",
            )

        return True, f"Account correctly locked after {max_attempts} failed attempts. DB verified."

    finally:
        _teardown_unlock_user(config)


def tc_08_no_lockout_before_threshold(config: dict) -> tuple[bool, str]:
    """
    TC_08: 4 consecutive failed login attempts should NOT lock the account.
    5th attempt with correct credentials must succeed.
    """
    api_url = config["api_login_endpoint"]
    login_url = config["login_url"]
    dashboard_url = config["dashboard_url"]
    username = config["valid_user"]["username"]
    wrong_password = config["wrong_password"]
    correct_password = config["valid_user"]["password"]

    try:
        # 4 failed API attempts
        responses = attempt_multiple_logins(api_url, username, wrong_password, 4)
        for i, resp in enumerate(responses, start=1):
            if resp.status_code in (403, 429):
                return False, f"Account locked prematurely on attempt {i}. Status: {resp.status_code}"

        # Verify NOT locked in DB
        with get_db_connection(config["db"]) as conn:
            locked = is_account_locked(conn, username)
        if locked:
            return False, "DB shows account is locked after only 4 failed attempts."

        # 5th attempt via UI with correct creds should succeed
        with launch_browser() as browser:
            with new_page(browser) as page:
                page.goto(login_url)
                fill_login_form(page, username, correct_password)
                page.wait_for_url(f"{dashboard_url}**", timeout=10_000)
                if dashboard_url not in page.url:
                    return False, f"Login failed on 5th attempt (correct creds). URL: {page.url}"

        return True, "No lockout after 4 failures; 5th attempt with correct creds succeeded."

    finally:
        _teardown_unlock_user(config)


def tc_09_locked_account_blocks_correct_creds(config: dict) -> tuple[bool, str]:
    """
    TC_09: After account is locked (5 failed attempts), even correct credentials
    should be blocked within the 15-minute lockout window.
    """
    api_url = config["api_login_endpoint"]
    username = config["valid_user"]["username"]
    wrong_password = config["wrong_password"]
    correct_password = config["valid_user"]["password"]
    max_attempts = config["lockout"]["max_attempts"]

    try:
        # Trigger lockout
        attempt_multiple_logins(api_url, username, wrong_password, max_attempts)

        # Verify DB lockout
        with get_db_connection(config["db"]) as conn:
            locked = is_account_locked(conn, username)
        if not locked:
            return False, "Pre-condition failed: account is not locked in DB."

        # Try correct credentials immediately
        with create_session() as session:
            response = post_login(session, api_url, username, correct_password)

        if response.status_code not in (401, 403, 429):
            return (
                False,
                f"Locked account accepted correct credentials! Status: {response.status_code}",
            )

        body = response.text.lower()
        expected_locked_msg = config["expected_messages"]["account_locked"].lower()
        if expected_locked_msg not in body:
            return (
                False,
                f"Lock notification missing from response. Body: '{body[:300]}'",
            )

        return True, "Locked account correctly blocked login with correct credentials."

    finally:
        _teardown_unlock_user(config)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    config = load_test_data()
    report = TestReport(suite_name="Invalid Credentials & Account Lockout")

    run_test(
        report, "TC_06",
        "Invalid credentials → generic error message (no field disclosure)",
        tc_06_invalid_credentials_generic_error, config,
    )
    run_test(
        report, "TC_07",
        "Account lockout triggers after exactly 5 consecutive failed attempts",
        tc_07_lockout_after_five_failures, config,
    )
    run_test(
        report, "TC_08",
        "4 consecutive failures do NOT lock; 5th correct attempt succeeds",
        tc_08_no_lockout_before_threshold, config,
    )
    run_test(
        report, "TC_09",
        "Locked account: correct creds blocked within 15-min lock window",
        tc_09_locked_account_blocks_correct_creds, config,
    )

    report.print_summary()
    report.save_json()


if __name__ == "__main__":
    main()
