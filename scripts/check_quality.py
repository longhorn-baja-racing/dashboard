"""One cross-platform entry point for the local and CI quality commands."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("sync", "dependencies", "lint", "format", "types", "tests", "gates", "build", "package")


def command(stage: str) -> list[str]:
    python = sys.executable
    commands = {
        "sync": ["uv", "sync", "--locked", "--extra", "dev"],
        "dependencies": ["uv", "pip", "check", "--python", python],
        "lint": [python, "-m", "ruff", "check", "."],
        "format": [python, "-m", "ruff", "format", "--check", "."],
        "types": [python, "-m", "pyright"],
        "tests": [
            python,
            "-m",
            "pytest",
            "--cov",
            "--cov-report=xml",
            "--junitxml=test-results/pytest.xml",
        ],
        "gates": [python, "scripts/check_quality_gates.py"],
        "build": ["uv", "build", "--no-sources"],
        "package": [python, "scripts/check_package.py"],
    }
    return commands[stage]


def run_stage(stage: str, *, directory: Path = ROOT) -> int:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONFAULTHANDLER"] = "1"
    print(f"Quality stage: {stage}", flush=True)
    try:
        return subprocess.run(command(stage), cwd=directory, env=env, timeout=900).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Could not complete {stage}: {error}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=STAGES)
    args = parser.parse_args()
    for stage in (args.stage,) if args.stage else STAGES:
        result = run_stage(stage)
        if result:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
