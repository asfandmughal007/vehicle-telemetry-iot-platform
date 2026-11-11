#!/usr/bin/env python3
"""Build the Lambda deployment directories ``build/processor`` and ``build/api``.

The Lambdas run on Linux/arm64 (Python 3.12) while developers build on any OS, so
third-party dependencies are installed as *prebuilt wheels for the target platform*
(``--platform manylinux2014_aarch64 --only-binary=:all:``) -- no Docker needed. The
project's own packages are pure Python and are installed without dependency resolution.
``boto3`` is not bundled: the Lambda runtime provides it.

Usage (from infra/cdk):  python scripts/build_lambdas.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

CDK_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CDK_DIR.parents[1]
BUILD_DIR = CDK_DIR / "build"

# Keep in sync with the stack (Runtime.PYTHON_3_12, Architecture.ARM_64).
PYTHON_VERSION = "3.12"
PLATFORM = "manylinux2014_aarch64"

TARGETS: dict[str, dict[str, list[str]]] = {
    "processor": {
        "third_party": ["pydantic>=2.7,<3"],
        "local": ["packages/telemetry-common", "cloud/lambdas/telemetry_processor"],
    },
    "api": {
        "third_party": [
            "pydantic>=2.7,<3",
            "pydantic-settings>=2.2,<3",
            "fastapi>=0.115,<1",
            "mangum>=0.19,<1",
        ],
        "local": ["packages/telemetry-common", "services/api"],
    },
}

# Trimmed from the bundles (the runtime ships boto3; caches and test data are dead weight).
_PRUNE_DIRS = ("__pycache__", "tests", "test")
_PRUNE_GLOBS = ("*.pyc", "*.dist-info/RECORD")


def run(*command: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", *command], check=True)


def build(name: str, spec: dict[str, list[str]]) -> Path:
    target = BUILD_DIR / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    run(
        "install",
        "--quiet",
        "--disable-pip-version-check",
        "--target",
        str(target),
        "--platform",
        PLATFORM,
        "--python-version",
        PYTHON_VERSION,
        "--implementation",
        "cp",
        "--only-binary=:all:",
        *spec["third_party"],
    )
    run(
        "install",
        "--quiet",
        "--disable-pip-version-check",
        "--target",
        str(target),
        "--no-deps",
        *(str(REPO_ROOT / path) for path in spec["local"]),
    )

    for directory in _PRUNE_DIRS:
        for found in target.rglob(directory):
            if found.is_dir():
                shutil.rmtree(found, ignore_errors=True)
    for pattern in _PRUNE_GLOBS:
        for found in target.rglob(pattern):
            found.unlink(missing_ok=True)
    return target


def main() -> None:
    for name, spec in TARGETS.items():
        path = build(name, spec)
        size_mb = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6
        print(f"built {path.relative_to(CDK_DIR)}  ({size_mb:.1f} MB unzipped)")


if __name__ == "__main__":
    main()
