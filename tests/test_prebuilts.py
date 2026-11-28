"""Tests for the portable prebuilt bundle used on non-LoongArch runners."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from vscode_loong64 import prebuilts
from vscode_loong64.config import BuildConfig, Paths


def _elf(machine: int = 258) -> bytes:
    return b"\x7fELF" + b"\x02\x01" + b"\x00" * 12 + machine.to_bytes(2, "little") + b"\x00" * 80


def _config(tmp_path: Path) -> BuildConfig:
    return BuildConfig(version="1.148.0", paths=Paths.create(tmp_path / "root"))


def _payloads(tmp_path: Path, machine: int = 258) -> dict:
    files = {
        "helpers/code-tunnel/code": _elf(machine),
        "natives/node-pty/build/Release/pty.node": _elf(machine),
        "rg": _elf(machine),
    }
    paths = {}
    for name, blob in files.items():
        path = tmp_path / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        paths[name] = path
    return paths


def test_bundle_name() -> None:
    assert (
        prebuilts.bundle_name("1.148.0", "43.7.7")
        == "loong64-prebuilts-1.148.0-electron43.7.7-loong64.tar.gz"
    )


def test_pack_and_unpack_roundtrip(tmp_path: Path) -> None:
    config = _config(tmp_path)
    payloads = _payloads(tmp_path)
    # reuse pack() by feeding its inputs
    helpers = {"code-tunnel": payloads["helpers/code-tunnel/code"]}
    natives = {"node-pty": tmp_path / "src" / "natives" / "node-pty", "ripgrep": payloads["rg"]}
    # ``natives/node-pty`` must contain the binding at the spec's relative path
    archive = prebuilts.pack(
        config, "1.148.0", "43.7.7", helpers, natives, commit="abc123"
    )
    assert archive.exists()
    meta = prebuilts.describe(archive)
    assert meta["schema"] == prebuilts.BUNDLE_SCHEMA
    assert meta["vscode"] == "1.148.0"
    assert meta["electron"] == "43.7.7"
    assert meta["commit"] == "abc123"
    assert {entry["path"] for entry in meta["entries"]} == {
        "helpers/code-tunnel/code",
        "natives/node-pty/build/Release/pty.node",
        "rg",
    }

    bundle = prebuilts.unpack(archive, tmp_path / "unpacked")
    assert bundle.version == "1.148.0"
    assert bundle.electron == "43.7.7"
    assert "code-tunnel" in bundle.helpers
    assert bundle.ripgrep is not None
    assert bundle.native_artifacts()["node-pty"] == tmp_path / "unpacked" / "natives" / "node-pty"


def test_unpack_rejects_foreign_arch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    payloads = _payloads(tmp_path, machine=62)
    archive = prebuilts.pack(
        config,
        "1.148.0",
        "43.7.7",
        {"code-tunnel": payloads["helpers/code-tunnel/code"]},
        {"ripgrep": payloads["rg"]},
    )
    with pytest.raises(Exception) as excinfo:
        prebuilts.unpack(archive, tmp_path / "unpacked")
    assert "not usable" in str(excinfo.value)


def test_unpack_rejects_tampered_payload(tmp_path: Path) -> None:
    config = _config(tmp_path)
    payloads = _payloads(tmp_path)
    archive = prebuilts.pack(
        config,
        "1.148.0",
        "43.7.7",
        {"code-tunnel": payloads["helpers/code-tunnel/code"]},
        {"ripgrep": payloads["rg"]},
    )
    # rewrite the archive with a modified payload, keeping the metadata intact
    unpacked = tmp_path / "tamper"
    prebuilts._safe_extract(archive, unpacked)
    target = unpacked / "rg"
    target.write_bytes(_elf() + b"extra")
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered, "w:gz") as tf:
        for entry in sorted(unpacked.rglob("*")):
            if entry.is_file():
                tf.add(entry, arcname=str(entry.relative_to(unpacked)), recursive=False)
    with pytest.raises(Exception) as excinfo:
        prebuilts.unpack(tampered, tmp_path / "out")
    assert "size mismatch" in str(excinfo.value) or "sha256 mismatch" in str(excinfo.value)


def test_unpack_rejects_unsafe_members(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    outside = tmp_path / "payload"
    outside.write_bytes(_elf())
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(outside, arcname="../evil", recursive=False)
    with pytest.raises(Exception) as excinfo:
        prebuilts.unpack(archive, tmp_path / "out")
    assert "unsafe" in str(excinfo.value)


def test_unpack_requires_bundle_json(tmp_path: Path) -> None:
    archive = tmp_path / "empty.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        payload = tmp_path / "x"
        payload.write_bytes(_elf())
        tf.add(payload, arcname="x", recursive=False)
    with pytest.raises(Exception) as excinfo:
        prebuilts.unpack(archive, tmp_path / "out")
    assert "bundle.json" in str(excinfo.value)


def test_unpack_rejects_other_schema(tmp_path: Path) -> None:
    archive = tmp_path / "old.tar.gz"
    payload = tmp_path / "x"
    payload.write_bytes(_elf())
    meta = tmp_path / "bundle.json"
    meta.write_text(json.dumps({"schema": 0, "vscode": "1.148.0"}), encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(payload, arcname="x", recursive=False)
        tf.add(meta, arcname="bundle.json", recursive=False)
    with pytest.raises(Exception) as excinfo:
        prebuilts.unpack(archive, tmp_path / "out")
    assert "schema" in str(excinfo.value)


def test_check_compatible_rejects_other_version() -> None:
    bundle = prebuilts.Bundle(root=Path("/x"), version="1.147.0", electron="43.7.7")
    with pytest.raises(Exception) as excinfo:
        prebuilts.check_compatible(bundle, "1.148.0", "43.7.7")
    assert "1.147.0" in str(excinfo.value)


def test_check_compatible_warns_on_electron_drift() -> None:
    bundle = prebuilts.Bundle(root=Path("/x"), version="1.148.0", electron="43.7.7")
    prebuilts.check_compatible(bundle, "1.148.0", "43.8.10")  # only warns


def test_safe_name_encodes_scoped_packages() -> None:
    assert prebuilts.safe_name("@parcel/watcher") == "parcel__watcher"
    assert prebuilts.safe_name("node-pty") == "node-pty"
    assert "/" not in prebuilts.safe_name("@vscode/sqlite3")


def test_bundle_upload_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(Exception) as excinfo:
        prebuilts.upload(tmp_path / "nope.tar.gz", repo="o/r")
    assert "not found" in str(excinfo.value)


def test_bundle_upload_requires_a_repository(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    payloads = _payloads(tmp_path)
    archive = prebuilts.pack(
        config,
        "1.148.0",
        "43.7.7",
        {"code-tunnel": payloads["helpers/code-tunnel/code"]},
        {"ripgrep": payloads["rg"]},
    )
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(Exception) as excinfo:
        prebuilts.upload(archive, token="x")
    assert "repository" in str(excinfo.value)


def test_bundle_upload_uses_the_publisher(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    payloads = _payloads(tmp_path)
    archive = prebuilts.pack(
        config,
        "1.148.0",
        "43.7.7",
        {"code-tunnel": payloads["helpers/code-tunnel/code"]},
        {"ripgrep": payloads["rg"]},
    )
    calls = {}

    class FakePublisher:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def ensure_release(self):
            calls["ensure"] = True

        def upload(self, files):
            calls["files"] = [str(f) for f in files]
            return [Path(f).name for f in files]

    import vscode_loong64.publish as publish_module

    monkeypatch.setattr(publish_module, "Publisher", FakePublisher)
    names = prebuilts.upload(archive, repo="owner/name")
    assert calls["ensure"] is True
    assert calls["init"]["tag"] == prebuilts.BUNDLE_TAG
    assert names == [archive.name]


def test_fetch_reports_a_missing_release(tmp_path: Path, monkeypatch) -> None:
    from vscode_loong64 import utils

    def boom(url, **kwargs):
        raise utils.BuildError(f"unable to fetch JSON from {url}: HTTP Error 404")

    monkeypatch.setattr(utils, "http_get_json", boom)
    with pytest.raises(Exception) as excinfo:
        prebuilts.fetch(None, repo="owner/name", dest=tmp_path)
    # a repo whose API is unreachable reports the actionable cause
    assert "no release assets" in str(excinfo.value)


def test_fetch_reports_an_empty_release(tmp_path: Path, monkeypatch) -> None:
    from vscode_loong64 import utils

    monkeypatch.setattr(utils, "http_get_json", lambda url, **kw: {"tag_name": "x", "assets": []})
    with pytest.raises(Exception) as excinfo:
        prebuilts.fetch(None, repo="owner/name", dest=tmp_path)
    assert "upload" in str(excinfo.value)


def test_fetch_picks_the_newest_bundle(tmp_path: Path, monkeypatch) -> None:
    from vscode_loong64 import utils

    assets = [
        {"name": "loong64-prebuilts-1.147.0-electron43.6.0-loong64.tar.gz",
         "browser_download_url": "https://example.invalid/old"},
        {"name": "loong64-prebuilts-1.148.0-electron43.7.7-loong64.tar.gz",
         "browser_download_url": "https://example.invalid/new"},
        {"name": "notes.txt", "browser_download_url": "https://example.invalid/txt"},
    ]
    monkeypatch.setattr(
        utils, "http_get_json", lambda url, **kw: {"tag_name": "t", "assets": assets}
    )
    downloaded = {}

    def fake_download(url, dest, **kwargs):
        downloaded["url"] = url
        Path(dest).write_bytes(b"x")
        return Path(dest)

    monkeypatch.setattr(utils, "download", fake_download)
    path = prebuilts.fetch(None, repo="owner/name", dest=tmp_path)
    assert path.name.endswith("1.148.0-electron43.7.7-loong64.tar.gz")
    assert downloaded["url"].endswith("/new")
