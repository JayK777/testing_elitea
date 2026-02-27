"""
api_helper.py
-------------
Reusable HTTP/API utility functions using the `requests` library.
Supports login API calls, session management, and CSRF/security checks.
Follows Single Responsibility and Dependency Inversion Principles.
"""

import logging
from typing import Any, Dict, Optional, Tuple

import requests
from requests import Response, Session

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10  # seconds


def create_session() -> Session:
    """
    Create and return a new requests Session.

    Returns:
        Session: A new requests.Session instance.
    """
    session = Session()
    logger.info("New API session created.")
    return session


def post_login(
    session: Session,
    url: str,
    username: str,
    password: str,
    csrf_token: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Response:
    """
    Submit a POST login request.

    Args:
        session (Session): requests Session to use.
        url (str): API endpoint URL.
        username (str): Username or email.
        password (str): Password.
        csrf_token (str): Optional CSRF token to include in headers.
        extra_headers (dict): Any additional headers.
        timeout (int): Request timeout in seconds.

    Returns:
        Response: HTTP response object.
    """
    headers = {"Content-Type": "application/json"}
    if csrf_token:
        headers["X-CSRF-Token"] = csrf_token
    if extra_headers:
        headers.update(extra_headers)

    payload = {"username": username, "password": password}

    logger.info("Sending POST login request to %s for user: %s", url, username)
    response = session.post(url, json=payload, headers=headers, timeout=timeout)
    logger.info("Response status: %s", response.status_code)
    return response


def attempt_multiple_logins(
    url: str,
    username: str,
    password: str,
    attempts: int,
    timeout: int = DEFAULT_TIMEOUT,
) -> list:
    """
    Perform multiple consecutive login attempts (e.g., for lockout boundary testing).

    Args:
        url (str): Login API endpoint.
        username (str): Username.
        password (str): Password.
        attempts (int): Number of times to attempt login.
        timeout (int): Request timeout per call.

    Returns:
        list[Response]: List of response objects for each attempt.
    """
    responses = []
    with create_session() as session:
        for i in range(1, attempts + 1):
            response = post_login(session, url, username, password, timeout=timeout)
            logger.info("Attempt %d/%d - Status: %s", i, attempts, response.status_code)
            responses.append(response)
    return responses


def check_https_redirect(url: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[bool, str]:
    """
    Verify that an HTTP URL is redirected to HTTPS.

    Args:
        url (str): The URL to check (should begin with http://).
        timeout (int): Request timeout.

    Returns:
        Tuple[bool, str]: (is_https_enforced, final_url)
    """
    try:
        response = requests.get(url, timeout=timeout, allow_redirects=True)
        final_url = response.url
        is_enforced = final_url.startswith("https://")
        logger.info(
            "HTTPS check - final URL: %s | enforced: %s", final_url, is_enforced
        )
        return is_enforced, final_url
    except requests.exceptions.RequestException as exc:
        logger.warning("HTTPS redirect check failed: %s", exc)
        return False, ""


def parse_json_response(response: Response) -> Optional[Dict[str, Any]]:
    """
    Safely parse JSON from a response object.

    Args:
        response (Response): HTTP response.

    Returns:
        dict or None: Parsed JSON body, or None if parsing fails.
    """
    try:
        return response.json()
    except ValueError:
        logger.warning("Response body is not valid JSON. Status: %s", response.status_code)
        return None
