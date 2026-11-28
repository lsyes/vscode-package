"""Tests for the asar reader/writer used to drop packed native payloads."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from vscode_loong64 import asar


def _build_archive(path: Path, files: dict, unpacked: dict) -> None:
    """Write a minimal asar with the given packed/unpacked members."""

    header: dict = {"files": {}}

    def node_for(parts):
        current = header
        for part in parts[:-1]:
            entry = current.setdefault("files", {}).setdefault(part, {"files": {}})
            current = entry
        return current.setdefault("files", {})

    data = b""
    offsets = {}
    for name, blob in files.items():
        offsets[name] = len(data)
        data += blob
    for name, blob in unpacked.items():
        (Path(str(path) + ".unpacked") / name).parent.mkdir(parents=True, exist_ok=True)
        (Path(str(path) + ".unpacked") / name).write_bytes(blob)
    for name, blob in files.items():
        parts = name.split("/")
        node_for(parts)[parts[-1]] = {
            "size": len(blob),
            "offset": str(offsets[name]),
        }
    for name, blob in unpacked.items():
        parts = name.split("/")
        node_for(parts)[parts[-1]] = {"size": len(blob), "unpacked": True}

    payload = json.dumps(header, separators=(",", ":")).encode()
    padded = payload + b"\x00" * ((-len(payload)) % 4)
    head = struct.pack("<IIII", 4, 8 + len(padded), 4 + len(padded), len(payload))
    path.write_bytes(head + padded + data)


def test_repack_removes_member_and_keeps_offsets_valid(tmp_path: Path) -> None:
    src = tmp_path / "node_modules.asar"
    files = {f"pkg{i}/f{i}.js": (f"content-{i}".encode() * 10) for i in range(20)}
    files["native/x64/apply-seccomp"] = b"\x7fELF" + b"\x00" * 40
    _build_archive(src, files, {"pkg0/unpacked.node": b"\x7fELF" + b"\x00" * 8})
    dst = tmp_path / "out.asar"
    asar.repack(src, dst, removals=["native/x64/apply-seccomp"])

    archive = asar.AsarArchive.open(dst)
    assert "native/x64/apply-seccomp" not in archive.files
    for name, blob in files.items():
        if name == "native/x64/apply-seccomp":
            continue
        assert archive.read(name) == blob, name


def test_repack_keeps_unpacked_entries_and_copies_them(tmp_path: Path) -> None:
    src = tmp_path / "node_modules.asar"
    _build_archive(src, {"a.js": b"aaa"}, {"native/b.node": b"\x7fELF" + b"\x00" * 8})
    dst = tmp_path / "out.asar"
    asar.repack(src, dst)
    archive = asar.AsarArchive.open(dst)
    entry = archive.get("native/b.node")
    assert entry.unpacked is True
    assert "offset" not in archive.header  # sanity: header is a dict
    assert (tmp_path / "out.asar.unpacked" / "native" / "b.node").exists()


def test_repack_replacements_become_unpacked(tmp_path: Path) -> None:
    src = tmp_path / "node_modules.asar"
    _build_archive(src, {"native/x.node": b"old"}, {})
    replacement = tmp_path / "new.node"
    replacement.write_bytes(b"new-payload")
    dst = tmp_path / "out.asar"
    asar.repack(src, dst, replacements={"native/x.node": replacement})
    archive = asar.AsarArchive.open(dst)
    assert archive.get("native/x.node").unpacked is True
    assert (tmp_path / "out.asar.unpacked" / "native" / "x.node").read_bytes() == b"new-payload"


def test_prune_drops_empty_directories() -> None:
    header = {"files": {"a": {"files": {"b": {"size": 1, "offset": "0"}}}}}
    asar._prune(header, "", {"a/b"})
    assert header == {"files": {}}


def test_header_pickle_roundtrip(tmp_path: Path) -> None:
    header = {"files": {"x.js": {"size": 3, "offset": "0"}}}
    blob = asar._header_pickle(header)
    marker, header_size, pickled_len, json_len = struct.unpack("<IIII", blob[:16])
    assert marker == 4
    assert json_len < pickled_len <= header_size
    assert json.loads(blob[16 : 16 + json_len].decode()) == header


def test_real_archive_roundtrip_when_available() -> None:
    path = Path("work/build/stage/resources/app/node_modules.asar")
    if not path.exists():
        pytest.skip("no staged node_modules.asar")
    archive = asar.AsarArchive.open(path)
    assert len(archive.files) > 100
