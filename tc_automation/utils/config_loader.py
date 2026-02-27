"""
config_loader.py
----------------
Utility to load test configuration and test data from test_data.json.
Follows Single Responsibility Principle (SRP).
"""

import json
import os
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def load_test_data(config_path: str = None) -> Dict[str, Any]:
    """
    Load test data from the JSON configuration file.

    Args:
        config_path (str): Optional path to the test_data.json file.
                           Defaults to tc_automation/test_data.json relative to project root.

    Returns:
        dict: Parsed test data dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        json.JSONDecodeError: If the config file is not valid JSON.
    """
    if config_path is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(base_dir, "test_data.json")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Test data file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    logger.info("Test data loaded successfully from: %s", config_path)
    return data


def get_nested(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """
    Safely retrieve a nested value from a dictionary.

    Args:
        data (dict): The source dictionary.
        *keys (str): Sequence of keys to traverse.
        default: Value to return if any key is missing.

    Returns:
        The value at the nested key path, or default if not found.
    """
    result = data
    for key in keys:
        if isinstance(result, dict):
            result = result.get(key, default)
        else:
            return default
    return result
