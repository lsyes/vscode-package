"""Guards for the CI workflow.

The packaging job runs its shell with ``set -euo pipefail``.  That combination
turns a *successful* command into a failing one whenever the reader on the left
of a pipe exits early::

    dpkg-deb -c pkg.deb | grep -q path   # grep exits on the first match,
                                         # tar gets EPIPE and exits 2

which is exactly how a complete, valid ``.deb`` failed its own verification
step.  These tests pin the two shapes that were affected so they cannot come
back, without needing a YAML parser (the CI ``check`` job only installs
pytest).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOW = Path(".github/workflows/build.yml")


def _workflow_text() -> str:
    if not WORKFLOW.exists():
        pytest.skip("no workflow in this checkout")
    return WORKFLOW.read_text(encoding="utf-8")


def test_no_grep_q_reading_from_a_command_pipeline() -> None:
    text = _workflow_text()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\|\s*grep\s+-q", line) and not line.strip().startswith("#")
    ]
    assert offenders == [], (
        "grep -q closes the pipe early; list the output into a file first "
        f"(pipefail makes the writer fail): {offenders}"
    )


def test_package_checks_use_a_listing_file() -> None:
    text = _workflow_text()
    assert 'dpkg-deb -c "$deb" > contents.txt' in text
    for helper in (
        "./usr/share/code/bin/code-tunnel",
        "./usr/share/code/bin/copilot-shim/copilot",
        "vendor/seccomp/loong64/apply-seccomp",
        "vsce-sign/bin/vsce-sign",
    ):
        assert f"'{helper}'" in text, helper


def test_no_ls_piped_into_head() -> None:
    text = _workflow_text()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"\bls\b[^|]*\|\s*head\b", line) and not line.strip().startswith("#")
    ]
    assert offenders == [], (
        "use nullglob arrays instead: 'ls | head -1' can also trip pipefail "
        f"and hides the empty case: {offenders}"
    )


def test_bundle_lookups_use_nullglob_arrays() -> None:
    text = _workflow_text()
    assert "shopt -s nullglob" in text
    assert re.search(r"bundles=\(dist/loong64-prebuilts-\*\.tar\.gz\)", text) or re.search(
        r"downloaded=\(dist/loong64-prebuilts-\*\.tar\.gz\)", text
    )


def test_pipelines_run_with_pipefail() -> None:
    """A step with a pipe must set ``pipefail`` or a writer error is lost."""

    text = _workflow_text()
    blocks = re.findall(r"run: \|\n((?:          .*\n|\n)+)", text)
    assert blocks, "no shell blocks found"
    missing = [
        i
        for i, block in enumerate(blocks)
        if "|" in block and "set -euo pipefail" not in block
    ]
    assert missing == [], f"run blocks pipe without pipefail: {missing}"


def test_workflow_targets_both_runner_classes() -> None:
    text = _workflow_text()
    assert "runs-on: ubuntu-latest" in text
    assert "[self-hosted, linux, loong64]" in text
    # the LoongArch job must be opt-in, otherwise the workflow can never run
    assert "inputs.build_payloads" in text


def test_workflow_is_packaging_only_by_default() -> None:
    text = _workflow_text()
    # ``package`` must not depend on the LoongArch job: the bundle is uploaded
    # by hand as a release asset
    package = text.split("  package:", 1)[1]
    assert "needs: check" in package
    assert "needs: payloads" not in package
