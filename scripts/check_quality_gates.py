"""Prove real quality tools reject deliberate failures in disposable project copies."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from check_quality import ROOT, command


def check(args: list[str], directory: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(QT_QPA_PLATFORM="offscreen", PYTHONFAULTHANDLER="1", UV_PYTHON_DOWNLOADS="never")
    temporary = directory / "test-results/tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
    return subprocess.run(
        args,
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )


def copy_project(directory: Path) -> None:
    for folder in ("src", "tests", "scripts", "sample_data"):
        shutil.copytree(
            ROOT / folder, directory / folder, ignore=shutil.ignore_patterns("__pycache__")
        )
    for name in ("pyproject.toml", "uv.lock", "README.md", ".python-version"):
        shutil.copy2(ROOT / name, directory / name)
    # Use this process's already locked environment for the actual Pyright checks.
    # All rule settings remain those in the copied project configuration.
    config = directory / "pyproject.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'venvPath = "."', f'venvPath = "{ROOT.as_posix()}"'
        ),
        encoding="utf-8",
    )


def main() -> int:
    output = ROOT / "test-results/gates"
    output.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, object]] = []
    try:
        with TemporaryDirectory(prefix="isolated-", dir=output) as temporary:
            directory = Path(temporary)
            copy_project(directory)
            # Lock verification does not install or alter any developer environment.
            lock_check = ["uv", "lock", "--check", "--offline", "--python", sys.executable]
            cases = (
                (
                    "tests",
                    directory / "tests/test_gate_probe.py",
                    "def test_gate_probe():\n    assert False, 'intentional-gate-probe'\n",
                    "intentional-gate-probe",
                    1,
                ),
                (
                    "format",
                    directory / "src/gate_probe.py",
                    "values=[1,2,3]\n",
                    "would be reformatted",
                    1,
                ),
                (
                    "types",
                    directory / "src/gate_probe.py",
                    'gate_probe: int = "intentional-type-error"\n',
                    "reportAssignmentType",
                    1,
                ),
                ("lock", directory / "pyproject.toml", None, "needs to be updated", 1),
            )
            for name, path, mutation, signature, expected_code in cases:
                args = lock_check if name == "lock" else command(name)
                baseline = check(args, directory)
                (output / f"{name}-clean.txt").write_text(
                    baseline.stdout + baseline.stderr, encoding="utf-8"
                )
                if baseline.returncode:
                    raise RuntimeError(f"{name}: clean control failed; see test-results/gates")
                original = path.read_bytes() if path.exists() else None
                try:
                    if name == "lock":
                        text = path.read_text(encoding="utf-8")
                        path.write_text(
                            text.replace(
                                "dependencies = [", 'dependencies = [\n    "colorama>=0.4.6",', 1
                            ),
                            encoding="utf-8",
                        )
                        args = [
                            "uv",
                            "sync",
                            "--locked",
                            "--extra",
                            "dev",
                            "--python",
                            sys.executable,
                        ]
                    else:
                        assert mutation is not None
                        path.write_text(mutation, encoding="utf-8")
                    broken = check(args, directory)
                    text = broken.stdout + broken.stderr
                    (output / f"{name}-broken.txt").write_text(text, encoding="utf-8")
                    caught = (
                        broken.returncode == expected_code and signature.lower() in text.lower()
                    )
                    evidence.append(
                        {
                            "gate": name,
                            "clean_exit": baseline.returncode,
                            "broken_exit": broken.returncode,
                            "expected_exit": expected_code,
                            "expected_diagnostic_found": signature.lower() in text.lower(),
                            "passed": caught,
                        }
                    )
                    if not caught:
                        raise RuntimeError(
                            f"{name}: expected rejection was not observed; see test-results/gates"
                        )
                    print(
                        f"PASS {name}: clean accepted; "
                        f"deliberate failure rejected ({broken.returncode})",
                        flush=True,
                    )
                finally:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(original)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Quality gate verification failed: {error}", file=sys.stderr)
        return 1
    finally:
        (output / "summary.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
