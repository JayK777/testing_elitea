"""UI automation for payment flows (Playwright).

Covers Automation-tagged scenarios:
- TC_01 Card happy path
- TC_02 Wallet happy path
- TC_04 Invalid card number
- TC_05 Expired card
- TC_06 CVV length boundary
- TC_08 Gateway delay/timeout behaviour (pending -> resolved)
- TC_11 Multiple rapid Pay taps do not create duplicates
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# <AUTOGEN:HELPERS>

# <AUTOGEN:TESTS>
