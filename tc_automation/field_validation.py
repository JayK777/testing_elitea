"""
field_validation.py
-------------------
TC_03 - Both fields blank → required errors shown, login blocked.
TC_04 - Username/email blank only → required error for that field.
TC_05 - Password blank only → required error, password remains masked.
TC_10 - Password masking by default + show/hide toggle preserves value.
TC_12 - Remember Me unchecked → session does NOT persist after browser close.

Uses: Playwright (UI).
Jira: EP-2
"""

import logging

from utils.browser_helper import fill_login_form, get_visible_text, launch_browser, new_page
from utils.config_loader import load_test_data
from utils.reporter import TestReport, run_test

logger = logging.getLogger(__name__)

# Common selectors
USERNAME_SELECTOR = "input[name='username'], input[type='email'], #username"
PASSWORD_SELECTOR = "input[name='password'], input[type='password'], #password"
SUBMIT_SELECTOR = "button[type='submit'], button:has-text('Login'), #login-btn"
ERROR_SELECTOR = ".error-message, [role='alert'], .field-error, .validation-error"


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def tc_03_both_fields_blank(config: dict) -> tuple[bool, str]:
    """
    TC_03: Submit login with both fields empty.
    Expects inline required-field errors for both username/email and password.
    """
    login_url = config["login_url"]

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            page.click(SUBMIT_SELECTOR)

            # Allow time for client-side validation to display
            page.wait_for_timeout(1000)

            errors = page.query_selector_all(ERROR_SELECTOR)
            error_texts = [e.inner_text().strip() for e in errors if e.is_visible()]

            if len(error_texts) < 2:
                return (
                    False,
                    f"Expected at least 2 required-field errors, got {len(error_texts)}: {error_texts}",
                )

            # Ensure user was NOT redirected
            if "login" not in page.url.lower() and config["login_url"] not in page.url:
                return False, f"User was redirected despite blank form. URL: {page.url}"

            return True, f"Both blank → errors shown: {error_texts}"


def tc_04_username_blank(config: dict) -> tuple[bool, str]:
    """
    TC_04: Submit with username/email blank, password filled.
    Expects required-field error for username only; no login.
    """
    login_url = config["login_url"]
    password = config["valid_user"]["password"]

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            page.fill(PASSWORD_SELECTOR, password)
            page.click(SUBMIT_SELECTOR)
            page.wait_for_timeout(1000)

            errors = page.query_selector_all(ERROR_SELECTOR)
            error_texts = [e.inner_text().strip() for e in errors if e.is_visible()]

            if not error_texts:
                return False, "No validation error shown when username/email is blank."

            if "login" not in page.url.lower() and config["login_url"] not in page.url:
                return False, f"User was redirected despite blank username. URL: {page.url}"

            return True, f"Username blank → error shown: {error_texts}"


def tc_05_password_blank(config: dict) -> tuple[bool, str]:
    """
    TC_05: Submit with password blank, username filled.
    Expects required-field error for password; password field remains masked.
    """
    login_url = config["login_url"]
    username = config["valid_user"]["username"]

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            page.fill(USERNAME_SELECTOR, username)
            page.click(SUBMIT_SELECTOR)
            page.wait_for_timeout(1000)

            errors = page.query_selector_all(ERROR_SELECTOR)
            error_texts = [e.inner_text().strip() for e in errors if e.is_visible()]

            if not error_texts:
                return False, "No validation error shown when password is blank."

            # Verify password field is still type=password (masked)
            pw_field = page.query_selector(PASSWORD_SELECTOR)
            field_type = pw_field.get_attribute("type") if pw_field else None
            if field_type != "password":
                return (
                    False,
                    f"Password field should be masked (type='password'), got: '{field_type}'",
                )

            return True, f"Password blank → error shown: {error_texts}. Field masked: type='{field_type}'"


def tc_10_password_masking_and_toggle(config: dict) -> tuple[bool, str]:
    """
    TC_10: Password is masked by default; show/hide toggle reveals/re-masks
    without clearing the entered value.
    """
    login_url = config["login_url"]
    test_password = config["valid_user"]["password"]
    toggle_selector = (
        "button[aria-label*='password'], "
        "[data-testid='show-password'], "
        ".password-toggle, "
        "button:has-text('Show')"
    )

    with launch_browser() as browser:
        with new_page(browser) as page:
            page.goto(login_url)
            page.fill(PASSWORD_SELECTOR, test_password)

            # Step 1: Verify masked by default
            pw_field = page.query_selector(PASSWORD_SELECTOR)
            initial_type = pw_field.get_attribute("type")
            if initial_type != "password":
                return False, f"Password not masked by default. type='{initial_type}'"

            # Step 2: Click show/hide toggle
            page.click(toggle_selector)
            page.wait_for_timeout(300)
            revealed_type = pw_field.get_attribute("type")
            if revealed_type not in ("text", "search"):
                return False, f"After toggle, password not revealed. type='{revealed_type}'"

            # Step 3: Verify value still intact
            current_value = pw_field.input_value()
            if current_value != test_password:
                return False, f"Password value changed by toggle. Got: '{current_value}'"

            # Step 4: Toggle back to masked
            page.click(toggle_selector)
            page.wait_for_timeout(300)
            re_masked_type = pw_field.get_attribute("type")
            if re_masked_type != "password":
                return False, f"Password not re-masked after second toggle. type='{re_masked_type}'"

            return True, "Password masking and show/hide toggle work correctly; value preserved."


def tc_12_remember_me_unchecked_no_persist(config: dict) -> tuple[bool, str]:
    """
    TC_12: Login without 'Remember Me' checked.
    Session should NOT persist after browser context is closed and re-opened.
    """
    login_url = config["login_url"]
    dashboard_url = config["dashboard_url"]
    username = config["valid_user"]["username"]
    password = config["valid_user"]["password"]

    with launch_browser() as browser:
        # Step 1: Login without Remember Me
        with new_page(browser) as page:
            page.goto(login_url)
            fill_login_form(page, username, password)
            page.wait_for_url(f"{dashboard_url}**", timeout=10_000)

            # Ensure Remember Me is NOT checked (default)
            remember_selector = "input[name='rememberMe'], #remember-me, input[type='checkbox']"
            checkbox = page.query_selector(remember_selector)
            if checkbox and checkbox.is_checked():
                checkbox.uncheck()

            storage_state = page.context.storage_state()

        # Step 2: Open new context without persistent storage; navigate to dashboard
        with new_page(browser) as new_pg:
            # Use empty storage to simulate new browser session
            new_pg.goto(dashboard_url)
            current_url = new_pg.url
            if "login" not in current_url.lower():
                return (
                    False,
                    f"Session persisted without Remember Me. URL: {current_url}",
                )

            return True, "Session correctly expired/cleared when Remember Me was not checked."


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def main():
    config = load_test_data()
    report = TestReport(suite_name="Field Validation & UI Controls")

    run_test(report, "TC_03", "Both fields blank → required errors for both fields",
             tc_03_both_fields_blank, config)
    run_test(report, "TC_04", "Username blank → required error for username field",
             tc_04_username_blank, config)
    run_test(report, "TC_05", "Password blank → required error, field stays masked",
             tc_05_password_blank, config)
    run_test(report, "TC_10", "Password masking default + show/hide toggle preserves value",
             tc_10_password_masking_and_toggle, config)
    run_test(report, "TC_12", "Remember Me unchecked → session does not persist",
             tc_12_remember_me_unchecked_no_persist, config)

    report.print_summary()
    report.save_json()


if __name__ == "__main__":
    main()
