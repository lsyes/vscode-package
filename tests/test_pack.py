"""Tests for the staging/pruning helpers that cover the old scan blind spots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vscode_loong64 import asar, pack
from vscode_loong64.config import BuildConfig, Paths, STANDALONE_HELPERS, UNSUPPORTED_PREBUILTS


def _elf(machine: int = 258) -> bytes:
    return b"\x7fELF" + b"\x02\x01" + b"\x00" * 12 + machine.to_bytes(2, "little") + b"\x00" * 80


def _stage(tmp_path: Path) -> pack.Stage:
    root = tmp_path / "stage"
    (root / "resources" / "app").mkdir(parents=True)
    config = BuildConfig(version="1.148.0", paths=Paths.create(tmp_path / "cache-root"))
    return pack.Stage(root=root, config=config)


def test_native_scan_roots_include_node_modules() -> None:
    roots = pack.native_scan_roots(Path("/app"))
    names = {p.name for p in roots}
    # ``node_modules`` used to be missing, which hid apply-seccomp and vsce-sign
    assert {"node_modules.asar.unpacked", "node_modules", "extensions"} <= names


def test_is_native_payload_covers_extensionless_helpers(tmp_path: Path) -> None:
    for name in ("apply-seccomp", "vsce-sign", "rg", "tgrep", "code-tunnel", "copilot", "srt"):
        assert pack.is_native_payload(Path(name)), name
    assert pack.is_native_payload(Path("x.node"))
    assert pack.is_native_payload(Path("libfoo.so.1"))
    assert not pack.is_native_payload(Path("index.js"))
    assert not pack.is_native_payload(Path("README.md"))


def test_disable_foreign_helpers_scans_node_modules(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    app = stage.app_dir
    vendor = app / "node_modules" / "@vscode" / "sandbox-runtime" / "vendor" / "seccomp" / "x64"
    vendor.mkdir(parents=True)
    foreign = vendor / "apply-seccomp"
    foreign.write_bytes(_elf(62))
    ok = app / "node_modules" / "native" / "good.node"
    ok.parent.mkdir(parents=True)
    ok.write_bytes(_elf())
    disabled = pack.disable_foreign_helpers(stage)
    assert any("apply-seccomp" in entry for entry in disabled)
    assert not foreign.exists()
    assert (vendor / "apply-seccomp.foreign.disabled").exists()
    assert ok.exists()


def test_verify_flags_foreign_payload_in_node_modules(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    (stage.root / "code").write_bytes(_elf())
    app = stage.app_dir
    (app / "node_modules").mkdir()
    (app / "node_modules" / "x.node").write_bytes(_elf(62))
    checks = {c.name: c for c in pack.verify_layout(stage)}
    assert checks["no foreign native payloads"].ok is False
    assert "node_modules/x.node" in checks["no foreign native payloads"].detail


def test_verify_flags_missing_helpers(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    checks = {c.name: c for c in pack.verify_layout(stage)}
    check = checks["standalone helpers are loongarch64"]
    assert check.ok is False
    for name in STANDALONE_HELPERS:
        assert name in check.detail


def _write_asar(path: Path, packed: dict, unpacked: dict) -> None:
    header: dict = {"files": {}}

    def node_for(parts):
        current = header
        for part in parts[:-1]:
            current = current.setdefault("files", {}).setdefault(part, {"files": {}})
        return current.setdefault("files", {})

    data = b""
    offsets = {}
    for name, blob in packed.items():
        offsets[name] = len(data)
        data += blob
    for name, blob in unpacked.items():
        target = Path(str(path) + ".unpacked") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    for name, blob in packed.items():
        parts = name.split("/")
        node_for(parts)[parts[-1]] = {"size": len(blob), "offset": str(offsets[name])}
    for name, blob in unpacked.items():
        parts = name.split("/")
        node_for(parts)[parts[-1]] = {"size": len(blob), "unpacked": True}
    payload = json.dumps(header, separators=(",", ":")).encode()
    padded = payload + b"\x00" * ((-len(payload)) % 4)
    import struct

    head = struct.pack("<IIII", 4, 8 + len(padded), 4 + len(padded), len(payload))
    path.write_bytes(head + padded + data)


def test_prune_asar_payloads_drops_packed_foreign_binary(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    archive = stage.app_dir / "node_modules.asar"
    _write_asar(
        archive,
        packed={
            "index.js": b"console.log(1)",
            "@vscode/sandbox-runtime/vendor/seccomp/x64/apply-seccomp": _elf(62),
        },
        unpacked={"@vscode/vsce-sign/bin/vsce-sign": _elf()},
    )
    report = pack.prune_asar_payloads(stage)
    assert report["status"] == "pruned"
    reopened = asar.AsarArchive.open(archive)
    assert "@vscode/sandbox-runtime/vendor/seccomp/x64/apply-seccomp" not in reopened.files
    assert reopened.read("index.js") == b"console.log(1)"
    # an unpacked loongarch payload stays
    assert reopened.get("@vscode/vsce-sign/bin/vsce-sign").unpacked is True


def test_prune_asar_payloads_noop_when_clean(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    _write_asar(stage.app_dir / "node_modules.asar", packed={"index.js": b"x"}, unpacked={})
    before = (stage.app_dir / "node_modules.asar").stat().st_mtime_ns
    report = pack.prune_asar_payloads(stage)
    assert report["status"] == "unchanged"
    assert (stage.app_dir / "node_modules.asar").stat().st_mtime_ns == before


def test_prune_asar_payloads_drops_removed_unpacked_target(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    archive = stage.app_dir / "node_modules.asar"
    _write_asar(archive, packed={"index.js": b"x"}, unpacked={"pkg/runtime.node": _elf()})
    # simulate ``prune_unsupported_prebuilts`` having deleted the real file
    (Path(str(archive) + ".unpacked") / "pkg" / "runtime.node").unlink()
    report = pack.prune_asar_payloads(stage)
    assert "pkg/runtime.node" in report["removed"]


def test_prune_unsupported_prebuilts_removes_listed_patterns(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    app = stage.app_dir
    copilot = app / "extensions" / "copilot" / "node_modules" / "@github" / "copilot"
    (copilot / "sdk" / "prebuilds" / "linux-x64").mkdir(parents=True)
    (copilot / "sdk" / "prebuilds" / "linux-x64" / "runtime.node").write_bytes(_elf(62))
    (copilot / "tgrep" / "bin" / "linux-x64").mkdir(parents=True)
    (copilot / "tgrep" / "bin" / "linux-x64" / "tgrep").write_bytes(_elf(62))
    removed = pack.prune_unsupported_prebuilts(stage)
    paths = {entry["path"] for entry in removed}
    assert any(p.endswith("runtime.node") for p in paths)
    assert any(p.endswith("tgrep") for p in paths)
    assert not (copilot / "sdk" / "prebuilds" / "linux-x64" / "runtime.node").exists()


def test_prune_unsupported_prebuilts_clears_disabled_leftovers(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    leftover = stage.app_dir / "node_modules" / "x.node.x86_64.disabled"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(_elf(62))
    pack.prune_unsupported_prebuilts(stage)
    assert not leftover.exists()


def test_unsupported_patterns_are_documented() -> None:
    for pattern, reason in UNSUPPORTED_PREBUILTS.items():
        assert pattern.startswith("**/"), pattern
        assert len(reason) > 20, pattern
