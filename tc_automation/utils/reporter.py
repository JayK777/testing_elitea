"""
reporter.py
-----------
Lightweight test result reporter with structured console logging
and optional JSON report file output.
Follows Single Responsibility Principle.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

logger = logging.getLogger(__name__)

# Configure root logging format once
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@dataclass
class TestResult:
    """Represents the outcome of a single test case."""
    tc_id: str
    name: str
    status: str          # "PASS" | "FAIL" | "ERROR" | "SKIP"
    message: str = ""
    duration_sec: float = 0.0
    error_detail: Optional[str] = None


@dataclass
class TestReport:
    """Aggregated report for a test module/suite."""
    suite_name: str
    results: List[TestResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def add(self, result: TestResult) -> None:
        """Append a TestResult to this report."""
        self.results.append(result)
        _log_result(result)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "PASS")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status in ("FAIL", "ERROR"))

    def print_summary(self) -> None:
        """Print a concise summary to the console."""
        separator = "=" * 60
        print(f"\n{separator}")
        print(f"  Suite    : {self.suite_name}")
        print(f"  Total    : {self.total}")
        print(f"  Passed   : {self.passed}")
        print(f"  Failed   : {self.failed}")
        print(f"  Skipped  : {self.total - self.passed - self.failed}")
        print(separator)
        for result in self.results:
            icon = "✅" if result.status == "PASS" else ("❌" if result.status in ("FAIL", "ERROR") else "⚠️")
            print(f"  {icon} [{result.tc_id}] {result.name} ({result.duration_sec:.2f}s)")
            if result.message:
                print(f"       → {result.message}")
            if result.error_detail:
                print(f"       ⚠  {result.error_detail}")
        print(separator + "\n")

    def save_json(self, output_dir: str = "reports") -> str:
        """
        Save the report as a JSON file.

        Args:
            output_dir (str): Directory to write the report to.

        Returns:
            str: Path to the created report file.
        """
        os.makedirs(output_dir, exist_ok=True)
        filename = f"{self.suite_name.replace(' ', '_')}_{self.started_at.replace(':', '-')}.json"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
        logger.info("Report saved to: %s", filepath)
        return filepath


def _log_result(result: TestResult) -> None:
    """Log a single test result to the standard logger."""
    level = logging.INFO if result.status == "PASS" else logging.WARNING
    logger.log(
        level,
        "[%s] %s - %s | %s",
        result.tc_id,
        result.name,
        result.status,
        result.message or "-",
    )


def run_test(
    report: TestReport,
    tc_id: str,
    name: str,
    test_fn,
    *args,
    **kwargs,
) -> TestResult:
    """
    Execute a single test function, capture result and timing, and add to the report.

    Args:
        report (TestReport): The report to append results to.
        tc_id (str): Test case identifier (e.g., "TC_01").
        name (str): Human-readable test name.
        test_fn (callable): The test function to execute. Must return (bool, str):
                            (passed: bool, message: str).
        *args: Positional arguments for test_fn.
        **kwargs: Keyword arguments for test_fn.

    Returns:
        TestResult: The result of the test execution.
    """
    start = time.time()
    try:
        passed, message = test_fn(*args, **kwargs)
        status = "PASS" if passed else "FAIL"
        result = TestResult(
            tc_id=tc_id,
            name=name,
            status=status,
            message=message,
            duration_sec=round(time.time() - start, 3),
        )
    except Exception as exc:  # noqa: BLE001
        result = TestResult(
            tc_id=tc_id,
            name=name,
            status="ERROR",
            message="Unexpected exception during test execution.",
            duration_sec=round(time.time() - start, 3),
            error_detail=str(exc),
        )
        logger.exception("Exception in test [%s] %s", tc_id, name)

    report.add(result)
    return result
