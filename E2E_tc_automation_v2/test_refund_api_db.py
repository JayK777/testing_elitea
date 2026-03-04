"""API/DB automation for gateway error and refund flows.

Covers Automation-tagged scenarios:
- TC_10 Gateway API error handled gracefully
- TC_13 Refund on cancellation updates status
- TC_14 Refund idempotency (no double refund)
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import requests

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None

# <AUTOGEN:HELPERS>

# <AUTOGEN:TESTS>
