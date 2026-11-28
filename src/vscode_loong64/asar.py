"""Minimal reader for Electron ``*.asar`` archives.

Only what the build needs: list entries, read a file, extract a subtree.  The
format is a small pickle header followed by the raw file data:

    ::

        uint32 = 4                      # pickle payload size marker
        uint32 = headerSize             # size of the header pickle
        uint32 = jsonLength             # length of the JSON header
        bytes  = JSON header            # {"files": {...}}
        bytes  = file data              # offsets are relative to data start

A tiny writer is included as well so that unpacked binaries can be replaced
inside an existing archive (used by ``--repack-asar``).
"""

from __future__ import annotations

import json
import os
import shutil
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .utils import BuildError


class AsarFile:
    """A single file entry inside an asar archive."""

    __slots__ = ("path", "size", "offset", "executable", "unpacked", "link")

    def __init__(
        self,
        path: str,
        size: int,
        offset: int,
        executable: bool = False,
        unpacked: bool = False,
        link: Optional[str] = None,
    ) -> None:
        self.path = path
        self.size = size
        self.offset = offset
        self.executable = executable
        self.unpacked = unpacked
        self.link = link

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"AsarFile({self.path!r}, size={self.size})"


@dataclass
class AsarArchive:
    path: Path
    header: Dict[str, object]
    data_offset: int
    files: Dict[str, AsarFile] = field(default_factory=dict)

    # -- construction ------------------------------------------------------------

    @classmethod
    def open(cls, path: Path) -> "AsarArchive":
        path = Path(path)
        with open(path, "rb") as fh:
            head = fh.read(16)
            if len(head) < 16:
                raise BuildError(f"{path} is not an asar archive")
            # chromium-pickle layout:
            #   u32 4 | u32 header_size | u32 pickled_header_len | u32 json_len | json...
            marker, header_size, pickled_len, json_size = struct.unpack("<IIII", head)
            if marker != 4:
                raise BuildError(f"{path} is not an asar archive (bad pickle marker {marker})")
            payload = fh.read(json_size)
        if len(payload) < json_size:
            raise BuildError(f"{path} has a truncated asar header")
        try:
            header = json.loads(payload.decode("utf-8"))
        except ValueError as exc:
            raise BuildError(f"{path} has an unreadable asar header: {exc}") from exc
        if not isinstance(header, dict) or "files" not in header:
            raise BuildError(f"{path} has an unexpected asar header")
        archive = cls(path=path, header=header, data_offset=8 + header_size)
        archive._index()
        return archive

    def _index(self, node: Optional[Dict[str, object]] = None, prefix: str = "") -> None:
        node = node or self.header
        for name, entry in (node.get("files") or {}).items():  # type: ignore[union-attr]
            entry = entry  # type: Dict[str, object]
            current = f"{prefix}/{name}" if prefix else name
            if "files" in entry:
                self._index(entry, current)
                continue
            link = entry.get("link")
            self.files[current] = AsarFile(
                path=current,
                size=int(entry.get("size") or 0),
                offset=int(entry.get("offset") or 0),
                executable=bool(entry.get("executable")),
                unpacked=bool(entry.get("unpacked")),
                link=str(link) if link else None,
            )

    # -- queries -----------------------------------------------------------------

    def __contains__(self, member: str) -> bool:
        return member.replace("\\", "/").lstrip("/") in self.files

    def list(self, prefix: str = "") -> List[AsarFile]:
        prefix = prefix.replace("\\", "/").lstrip("/").rstrip("/")
        if not prefix:
            return list(self.files.values())
        needle = prefix + "/"
        return [f for f in self.files.values() if f.path == prefix or f.path.startswith(needle)]

    def get(self, member: str) -> AsarFile:
        key = member.replace("\\", "/").lstrip("/")
        try:
            return self.files[key]
        except KeyError as exc:
            raise BuildError(f"{member} not found in {self.path}") from exc

    def read(self, member: str) -> bytes:
        entry = self.get(member)
        if entry.unpacked:
            unpacked_path = Path(str(self.path) + ".unpacked") / entry.path
            return unpacked_path.read_bytes()
        with open(self.path, "rb") as fh:
            fh.seek(self.data_offset + entry.offset)
            return fh.read(entry.size)

    def read_json(self, member: str) -> object:
        return json.loads(self.read(member).decode("utf-8"))

    def exists_unpacked(self, member: str) -> bool:
        return (Path(str(self.path) + ".unpacked") / member).exists()

    # -- extraction --------------------------------------------------------------

    def extract(self, dest: Path, prefix: str = "") -> List[Path]:
        dest = Path(dest)
        written: List[Path] = []
        for entry in self.list(prefix):
            target = dest / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read(entry.path))
            if entry.executable:
                target.chmod(target.stat().st_mode | 0o111)
            written.append(target)
        return written


def list_native_packages(archive_path: Path) -> Dict[str, str]:
    """Map ``node_modules`` package name -> version for every package in the asar."""

    archive = AsarArchive.open(archive_path)
    versions: Dict[str, str] = {}
    for name in archive.files:
        if not name.endswith("/package.json"):
            continue
        package = name[: -len("/package.json")]
        if package.count("/") > 1:  # scoped: @scope/name/package.json
            continue
        try:
            meta = archive.read_json(name)
        except (ValueError, BuildError):
            continue
        if isinstance(meta, dict) and meta.get("name") and meta.get("version"):
            versions[str(meta["name"])] = str(meta["version"])
    return versions


def _prune(node: Dict[str, object], prefix: str, removed: Iterable[str]) -> None:
    """Drop ``removed`` members (and directories they leave empty) in place."""

    removed = set(removed)
    files = node.get("files") or {}
    for name in list(files):
        entry = files[name]
        current = f"{prefix}/{name}" if prefix else name
        if current in removed:
            del files[name]
            continue
        if "files" in entry:
            _prune(entry, current, removed)
            if not (entry.get("files") or {}):
                del files[name]


def _header_pickle(header: Dict[str, object]) -> bytes:
    """Serialise the asar header the way ``@electron/asar`` does.

    The first four little-endian ``uint32`` values are::

        4                      # pickle payload size marker
        headerSize             # 8 + padded JSON length
        pickledHeaderLen       # 4 + padded JSON length
        jsonLength             # unpadded JSON length

    followed by the JSON header padded to a 4-byte boundary.  ``data_offset``
    is ``8 + headerSize`` (see :meth:`AsarArchive.open`).
    """

    payload = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    padded = payload + b"\x00" * ((-len(payload)) % 4)
    header_size = 8 + len(padded)
    pickled_len = 4 + len(padded)
    return struct.pack("<IIII", 4, header_size, pickled_len, len(payload)) + padded


def repack(
    source: Path,
    dest: Path,
    replacements: Optional[Dict[str, Path]] = None,
    removals: Optional[Iterable[str]] = None,
) -> None:
    """Rewrite ``source`` into ``dest`` replacing/unpacking specific members.

    ``replacements`` maps archive member path -> file on disk.  Replaced members
    are written as *unpacked* entries, which is what VSCode expects for binaries.
    ``removals`` drops members (and their now-empty parent directories) from
    both the header and the data section.
    """

    replacements = {k.replace("\\", "/").lstrip("/"): Path(v) for k, v in (replacements or {}).items()}
    removed = {r.replace("\\", "/").lstrip("/") for r in (removals or ())}
    src = AsarArchive.open(source)
    header = json.loads(json.dumps(src.header))  # deep copy
    nodes: Dict[str, Dict[str, object]] = {}

    def index(node: Dict[str, object], prefix: str = "") -> None:
        for name, entry in (node.get("files") or {}).items():
            current = f"{prefix}/{name}" if prefix else name
            nodes[current] = entry  # type: ignore[assignment]
            if "files" in entry:
                index(entry, current)

    index(header)

    if removed:
        _prune(header, "", removed)
        nodes = {k: v for k, v in nodes.items() if k not in removed}

    replacements = {k: v for k, v in replacements.items() if k in nodes}
    for member in replacements:
        entry = nodes[member]
        entry.pop("offset", None)
        entry.pop("size", None)
        entry["unpacked"] = True

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    unpacked_root = Path(str(dest) + ".unpacked")

    written_unpacked: Dict[str, Path] = {}
    for member, source_file in replacements.items():
        target = unpacked_root / member
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(source_file, "rb") as fh_in, open(target, "wb") as fh_out:
            while True:
                block = fh_in.read(1 << 20)
                if not block:
                    break
                fh_out.write(block)
        target.chmod(0o755)
        written_unpacked[member] = target

    # also make sure the original unpacked entries stay unpacked
    for member, entry in nodes.items():
        if entry.get("unpacked"):
            pass

    # Packed members are laid out in the data section in the same order as they
    # are written.  Offsets are computed up front so the header can be written
    # once: its JSON length depends on the offset values, which means it must not
    # be streamed before the data (a longer header would misalign every offset).
    packed: List[Tuple[AsarFile, Dict[str, object]]] = []
    for entry in sorted(src.files.values(), key=lambda e: e.path):
        if entry.path in replacements or entry.path in removed:
            continue
        node = nodes.get(entry.path)
        if node is None:
            continue
        if entry.unpacked:
            # Unpacked members keep ``size`` in the header (Electron uses it for
            # directory listings) and have no ``offset``.  A zero size makes the
            # loader report "Could not locate the bindings file", so the real
            # size is restored from the extracted file when the header lost it.
            source_unpacked = Path(str(src.path) + ".unpacked") / entry.path
            node.pop("offset", None)
            if not source_unpacked.exists():
                # a staged build may already have pruned this binary
                continue
            node["size"] = source_unpacked.stat().st_size
            node["unpacked"] = True
            if src.path.resolve() != dest.resolve():
                target = unpacked_root / entry.path
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_unpacked, target)
            continue
        packed.append((entry, node))

    cursor = 0
    for entry, node in packed:
        node["offset"] = str(cursor)
        node["size"] = entry.size
        cursor += entry.size
    new_header = _header_pickle(header)
    with open(src.path, "rb") as fh_in, open(dest, "wb") as fh_out:
        fh_out.write(new_header)
        written = 0
        for entry, _node in packed:
            fh_in.seek(src.data_offset + entry.offset)
            payload = fh_in.read(entry.size)
            fh_out.write(payload)
            written += len(payload)
        if written != cursor:  # pragma: no cover - corrupt archive
            raise BuildError(
                f"asar repack wrote {written} bytes but the header describes {cursor}"
            )
    dest.chmod(0o644)
