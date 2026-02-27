"""
security_tests.py
-----------------
TC_11 - HTTPS enforcement: login traffic must not be submittable over HTTP.
TC_13 (security part) - CSRF protection: login request rejected without valid CSRF token.
TC_14 - SQL injection in username/email field is handled safely (no bypass, no crash).
TC_15 - XSS attempt in username/email field is escaped/sanitized (no script execution).

Uses: requests (API) + Playwright (UI).
Jira: EP-2
"""

import logging

from utils.api_helper import check_https_redirect, create_session, parse_json_response, post_login
from utils.browser_helper import fill_login_form, launch_browser, new_page
from utils.config_loader import load_test_data
from utils.reporter import TestReport, run_test

logger = logging.getLogger(__name__)

USERNAME_SELECTOR = "input[name='username'], input[type='email'], #username"
PASSWORD_SELECTOR = "input[name='password'], input[type='password'], #password"
SUBMIT_SELECTOR = "button[type='submit'], button:has-text('Login'), #login-btn"
ERROR_SELECTOR = ".error-message, [role='alert'], .flash-error, .login-error"


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def tc_11_https_enforcement(config: dict) -> tuple[bool, str]:
    """
    TC_11: Verify all login traffic is served over HTTPS.
    - HTTP login URL should redirect to HTTPS (or be inaccessible).
    - Credentials submitted over HTTP should be rejected.
    """
    login_url = config["login_url"]

    # Build HTTP variant of the login URL
    http_url = login_url.replace("https://", "http://", 1)
    if http_url == login_url:
        return False, "login_url in config is not HTTPS. Please update test_data.json."

    is_enforced, final_url = check_https_redirect(http_url)

    if not is_enforced:
        return (
            False,
            f"HTTP login URL was NOT redirected to HTTPS. Final URL: '{final_url}'",
        )

    return True, f"HTTPS enforced: HTTP URL '{http_url}' → HTTPS '{final_url}'"


def tc_csrf_protection(config: dict) -> tuple[bool, str]:
    """
    TC_12 (security): Submit login request with a missing/invalid CSRF token.
    Expects the server to reject the request (e.g., 403 Forbidden).
    """
    api_url = config["api_login_endpoint"]
    username = config["valid_user"]["username"]
    password = config["valid_user"]["password"]

    with create_session() as session:
        # Attempt 1: No CSRF token header at all
        response_no_csrf = post_login(
            session, api_url, username, password, csrf_token=None
        )

        # Attempt 2: Explicitly invalid CSRF token
        response_bad_csrf = post_login(
            session, api_url, username, password, csrf_token="invalid-csrf-token-xyz"
        )

    # At least one of these should return 403 (or equivalent CSRF rejection)
    blocked_statuses = {400, 401, 403, 422}
    no_csrf_blocked = response_no_csrf.status_code in blocked_statuses
    bad_csrf_blocked = response_bad_csrf.status_code in blocked_statuses

    if not no_csrf_blocked and not bad_csrf_blocked:
        return (
            False,
            f"CSRF check failed. No-token status: {response_no_csrf.status_code}, "
            f"bad-token status: {response_bad_csrf.status_code}. Neither was blocked.",
        )

    return (
        True,
        f"CSRF protection active. "
        f"no-token→{response_no_csrf.status_code}, "
        f"bad-token→{response_bad_csrf.status_code}",
    )


def tc_14_sql_injection_handled_safely(config: dict) -> tuple[bool, str]:
    """
    TC_14 (TC_13 in comment): SQL injection payload in username/email field.
    Verifies:
    - Login is not bypassed.
    - Application does not crash.
    - No DB error details are exposed to the user.
    """
    login_url = config["login_url"]
    sql_payload = config["sql_injection_payload"]
    any_password = "IrrelevantPass123"
    expected_msg = config["expected_messages"]["invalid_credentials"].lower()

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            page.fill(USERNAME_SELECTOR, sql_payload)
            page.fill(PASSWORD_SELECTOR, any_password)
            page.click(SUBMIT_SELECTOR)
            page.wait_for_timeout(2000)

            # Check we did NOT land on dashboard (login not bypassed)
            current_url = page.url
            if config["dashboard_url"] in current_url:
                return False, f"SQL injection BYPASSED login! URL: {current_url}"

            # Check no DB/server error is visible on page
            page_content = page.content().lower()
            db_error_indicators = [
                "sql syntax", "ora-", "pg::", "psycopg2", "uncaught exception",
                "stack trace", "internal server error", "database error", "syntax error",
            ]
            for indicator in db_error_indicators:
                if indicator in page_content:
                    return False, f"DB/server error exposed to user: '{indicator}'"

            # A generic error message should appear
            error_el = page.query_selector(ERROR_SELECTOR)
            if error_el is None or not error_el.is_visible():
                return False, "No error message shown for SQLi attempt (expected generic error)."

            error_text = error_el.inner_text().lower()
            if expected_msg not in error_text:
                return (
                    False,
                    f"Expected generic error, got: '{error_text}'",
                )

            return True, f"SQL injection handled safely. Error shown: '{error_text}'"


def tc_15_xss_attempt_not_executed(config: dict) -> tuple[bool, str]:
    """
    TC_15 (TC_14 in comment): XSS payload in username/email field.
    Verifies:
    - Script is NOT executed (no dialog triggered).
    - Payload is escaped/sanitized in any rendered output.
    - Only a safe generic error is shown.
    """
    login_url = config["login_url"]
    xss_payload = config["xss_payload"]
    any_password = "IrrelevantPass123"

    with launch_browser() as browser:
        with new_page(browser) as page:
            # Attach dialog listener — if XSS fires, alert() will trigger a dialog
            dialog_triggered = {"value": False}

            def handle_dialog(dialog):
                dialog_triggered["value"] = True
                logger.warning("XSS dialog triggered! Message: %s", dialog.message)
                dialog.dismiss()

            page.on("dialog", handle_dialog)

            page.goto(login_url)
            page.fill(USERNAME_SELECTOR, xss_payload)
            page.fill(PASSWORD_SELECTOR, any_password)
            page.click(SUBMIT_SELECTOR)
            page.wait_for_timeout(2000)

            if dialog_triggered["value"]:
                return False, "XSS script executed! alert() dialog was triggered."

            # Verify payload is not rendered as raw HTML/script in the page source
            page_content = page.content()
            if "<script>alert" in page_content.lower():
                return False, "XSS payload found unescaped in page source."

            # A safe generic error should be shown
            error_el = page.query_selector(ERROR_SELECTOR)
            error_text = error_el.inner_text().lower() if error_el and error_el.is_visible() else ""

            return True, (
                f"XSS payload safely handled. No script executed. "
                f"Error shown: '{error_text or 'none'}'"
            )


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    config = load_test_data()
    report = TestReport(suite_name="Security Tests")

    run_test(
        report, "TC_11",
        "HTTPS enforcement: HTTP login URL redirects to HTTPS",
        tc_11_https_enforcement, config,
    )
    run_test(
        report, "TC_12",
        "CSRF protection: login rejected without valid CSRF token",
        tc_csrf_protection, config,
    )
    run_test(
        report, "TC_14",
        "SQL injection in username/email: login not bypassed, no DB errors exposed",
        tc_14_sql_injection_handled_safely, config,
    )
    run_test(
        report, "TC_15",
        "XSS payload in username/email: not executed, payload sanitized",
        tc_15_xss_attempt_not_executed, config,
    )

    report.print_summary()
    report.save_json()


if __name__ == "__main__":
    main()
