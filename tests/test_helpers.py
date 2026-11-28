"""Tests for the standalone-helper pipeline (no compilers involved)."""

from __future__ import annotations

import json
from pathlib import Path

from vscode_loong64 import helpers, patches


def _fake_loongarch_elf(machine: int = 258) -> bytes:
    """A 20-byte ELF header (enough for ``elf_machine``) plus padding."""

    # e_ident: magic, class=2 (64-bit), data=1 (little endian); e_machine at 18
    header = b"\x7fELF" + b"\x02\x01" + b"\x00" * 12 + machine.to_bytes(2, "little")
    return header + b"\x00" * 80
from vscode_loong64.config import STANDALONE_HELPERS, BuildConfig, Paths


def _config(tmp_path: Path) -> BuildConfig:
    paths = Paths.create(tmp_path)
    return BuildConfig(version="1.148.0", paths=paths)


def test_upstream_tag_and_url() -> None:
    assert helpers.upstream_tag_for("1.148.0") == "1.148.0"
    assert helpers.source_tarball_url("1.148.0") == (
        "https://codeload.github.com/microsoft/vscode/tar.gz/1.148.0"
    )


def test_upstream_tag_requires_a_version() -> None:
    try:
        helpers.upstream_tag_for("")
    except Exception as exc:  # noqa: BLE001
        assert "source tag" in str(exc)
    else:
        raise AssertionError("empty version was accepted")


def test_change_report_detects_newer_upstream() -> None:
    report = helpers.check_source_update("1.148.0", "1.149.0")
    assert report["update"] is True
    assert report["currentTag"] == "1.148.0"
    assert report["latestTag"] == "1.149.0"


def test_change_report_is_quiet_when_current() -> None:
    report = helpers.check_source_update("1.148.0", "1.148.0")
    assert report["update"] is False


def test_change_report_without_network() -> None:
    report = helpers.check_source_update("1.148.0", None)
    assert report["update"] is False
    assert report["latest"] is None


def test_every_helper_has_an_install_path() -> None:
    for name, spec in STANDALONE_HELPERS.items():
        assert spec.get("install"), name
        assert spec.get("kind") in ("rust", "seccomp", "dotnet"), name
        assert not str(spec["install"]).startswith("/"), name


def test_prebuilt_lookup_requires_matching_version(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = config.paths.cache / "prebuilts" / "apply-seccomp" / "1.147.0" / "loong64"
    root.mkdir(parents=True)
    binary = root / "apply-seccomp"
    binary.write_bytes(_fake_loongarch_elf())
    # wrong version: the marker does not exist and the tree is keyed elsewhere
    assert helpers.find_prebuilt(config, "apply-seccomp", "1.148.0", "apply-seccomp") is None
    # matching tree but the synthetic file is not a valid ELF of the right kind
    found = helpers.find_prebuilt(config, "apply-seccomp", "1.147.0", "apply-seccomp")
    assert found is not None and found.prebuilt is True


def test_prebuilt_marker_is_honoured(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = config.paths.cache / "prebuilts" / "code-tunnel" / "1.148.0" / "loong64"
    root.mkdir(parents=True)
    binary = root / "code"
    binary.write_bytes(_fake_loongarch_elf())
    (root / "code.helper.json").write_text(
        json.dumps(
            {"name": "code-tunnel", "version": "1.147.0", "arch": "loong64"}
        ),
        encoding="utf-8",
    )
    # marker says 1.147.0 but the requested version is 1.148.0 -> stale
    assert helpers.find_prebuilt(config, "code-tunnel", "1.148.0", "code") is None


def test_install_skips_missing_helpers(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stage = type("Stage", (), {"root": tmp_path / "stage", "native_report": {}})()
    stage.root.mkdir()
    report = helpers.install(stage, {})
    assert {entry["name"] for entry in report} == set(STANDALONE_HELPERS)
    assert all(entry["status"] == "missing" for entry in report)


def test_install_copies_helper_to_upstream_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stage = type("Stage", (), {"root": tmp_path / "stage", "native_report": {}})()
    stage.root.mkdir()
    binary = tmp_path / "apply-seccomp"
    binary.write_bytes(_fake_loongarch_elf())
    report = helpers.install(stage, {"apply-seccomp": binary})
    entry = next(e for e in report if e["name"] == "apply-seccomp")
    assert entry["status"] == "built"
    target = stage.root / STANDALONE_HELPERS["apply-seccomp"]["install"]
    assert target.exists() and target.stat().st_mode & 0o111


# -- source patches ----------------------------------------------------------


def test_prereqs_patch_applies_once(tmp_path: Path) -> None:
    target = tmp_path / patches.PREREQS_PATH
    target.parent.mkdir(parents=True)
    target.write_text(
        "fn f() {\n"
        + patches.DEFAULT_LIB_PATH_OLD
        + "\n\tconst X: u8 = 0;\n}\n",
        encoding="utf-8",
    )
    first = patches.apply(tmp_path, "cli")
    assert first[0]["status"] == "applied"
    assert 'target_arch = "loongarch64"' in target.read_text(encoding="utf-8")
    second = patches.apply(tmp_path, "cli")
    assert second[0]["status"] == "present"


def test_prereqs_patch_reports_stale_upstream(tmp_path: Path) -> None:
    target = tmp_path / patches.PREREQS_PATH
    target.parent.mkdir(parents=True)
    target.write_text("fn f() {}\n", encoding="utf-8")
    report = patches.apply(tmp_path, "cli")
    assert report[0]["status"] == "stale"
    try:
        patches.require_applied(report)
    except Exception as exc:  # noqa: BLE001
        assert "no longer apply" in str(exc)
    else:
        raise AssertionError("stale patch did not fail the build")


def test_patch_for_unknown_crate_is_empty(tmp_path: Path) -> None:
    assert patches.apply(tmp_path, "copilot_shim") == []
