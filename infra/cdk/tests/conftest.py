"""The CDK tests synthesize CloudFormation through the jsii runtime, which needs Node.js.

Without Node.js they are skipped (instead of failing at import time), so a plain ``pytest``
works on a machine that only has Python. CI installs Node.js and runs them.
"""

import shutil

import pytest

NODE_MISSING = shutil.which("node") is None

collect_ignore_glob = ["test_*.py"] if NODE_MISSING else []


def pytest_report_header(config: pytest.Config) -> str | None:
    if NODE_MISSING:
        return "CDK tests skipped: Node.js not found on PATH (install Node 18+ to run them)"
    return None
