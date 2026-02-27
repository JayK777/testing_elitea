"""
db_helper.py
------------
Reusable PostgreSQL database utility functions using psycopg2.
Used for verifying DB-side state (e.g., lockout records, session tokens).
Follows Single Responsibility Principle.
"""

import logging
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, Tuple

import psycopg2
from psycopg2.extensions import connection as PgConnection, cursor as PgCursor

logger = logging.getLogger(__name__)


@contextmanager
def get_db_connection(db_config: Dict[str, Any]) -> Generator[PgConnection, None, None]:
    """
    Context manager for a PostgreSQL database connection.

    Args:
        db_config (dict): Dictionary with keys: host, port, dbname, user, password.

    Yields:
        psycopg2 connection object.
    """
    conn: Optional[PgConnection] = None
    try:
        conn = psycopg2.connect(
            host=db_config["host"],
            port=db_config.get("port", 5432),
            dbname=db_config["dbname"],
            user=db_config["user"],
            password=db_config["password"],
        )
        logger.info("DB connection established to '%s'.", db_config["dbname"])
        yield conn
    except psycopg2.OperationalError as exc:
        logger.error("DB connection failed: %s", exc)
        raise
    finally:
        if conn and not conn.closed:
            conn.close()
            logger.info("DB connection closed.")


@contextmanager
def get_db_cursor(conn: PgConnection) -> Generator[PgCursor, None, None]:
    """
    Context manager for a database cursor with auto-commit/rollback.

    Args:
        conn: Active psycopg2 connection.

    Yields:
        psycopg2 cursor object.
    """
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error("DB operation rolled back due to error: %s", exc)
        raise
    finally:
        cursor.close()


def fetch_one(
    conn: PgConnection, query: str, params: Tuple = ()
) -> Optional[Tuple]:
    """
    Execute a SELECT query and return a single result row.

    Args:
        conn: Active psycopg2 connection.
        query (str): SQL query string.
        params (tuple): Query parameters.

    Returns:
        Single row tuple or None.
    """
    with get_db_cursor(conn) as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()


def fetch_all(
    conn: PgConnection, query: str, params: Tuple = ()
) -> List[Tuple]:
    """
    Execute a SELECT query and return all result rows.

    Args:
        conn: Active psycopg2 connection.
        query (str): SQL query string.
        params (tuple): Query parameters.

    Returns:
        List of row tuples.
    """
    with get_db_cursor(conn) as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def is_account_locked(conn: PgConnection, username: str) -> bool:
    """
    Check whether a user account is locked in the database.

    Args:
        conn: Active psycopg2 connection.
        username (str): The user's username or email.

    Returns:
        bool: True if account is locked, False otherwise.
    """
    row = fetch_one(
        conn,
        "SELECT is_locked FROM users WHERE username = %s OR email = %s",
        (username, username),
    )
    if row is None:
        logger.warning("User '%s' not found in database.", username)
        return False
    return bool(row[0])


def reset_user_lockout(conn: PgConnection, username: str) -> None:
    """
    Reset a user's failed login attempts and unlock their account.
    Useful for test teardown.

    Args:
        conn: Active psycopg2 connection.
        username (str): Username or email to unlock.
    """
    with get_db_cursor(conn) as cursor:
        cursor.execute(
            "UPDATE users SET is_locked = FALSE, failed_attempts = 0, "
            "locked_until = NULL WHERE username = %s OR email = %s",
            (username, username),
        )
    logger.info("Lockout reset for user: %s", username)
