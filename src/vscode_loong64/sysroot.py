"""Header-only sysroot provisioning for native module builds.

Several VSCode native modules link against system libraries whose ``-dev``
packages may be missing on a build host (``libxkbfile`` for ``native-keymap``,
``libkrb5`` for ``kerberos``).  Instead of requiring root or a fully populated
toolchain, the required ``-dev`` packages are downloaded with ``apt-get download``
and unpacked into a private sysroot that is passed to ``node-gyp`` through
``PKG_CONFIG_PATH``/``CPLUS_INCLUDE_PATH``/``LIBRARY_PATH``.

Everything is cached under ``<cache>/sysroot`` so the download happens once.
When ``apt-get`` is unavailable the sysroot silently stays empty and the
affected modules fail exactly as they would without this helper.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import utils
from .utils import log, warn

#: packages that provide the headers/libs VSCode's native modules need
DEFAULT_PACKAGES: Dict[str, List[str]] = {
    "xkbcommon": ["libxkbcommon-dev"],
    "xkbfile": ["libxkbfile-dev"],
    "krb5": ["libkrb5-dev", "krb5-multidev"],
    "sqlite3": ["libsqlite3-dev"],
    "curl": ["libcurl4-openssl-dev"],
}

#: multiarch library directories probed inside the sysroot
LIB_SUBDIRS = (
    "usr/lib/loongarch64-linux-gnu",
    "usr/lib64",
    "usr/lib",
    "usr/lib/x86_64-linux-gnu",
    "usr/lib/aarch64-linux-gnu",
)


@dataclass
class Sysroot:
    """An unpacked set of ``-dev`` packages used to build native modules."""

    root: Path
    packages: List[str] = field(default_factory=list)
    available: bool = False

    @property
    def include_dir(self) -> Path:
        return self.root / "usr" / "include"

    def pkg_config_paths(self) -> List[Path]:
        paths: List[Path] = []
        for sub in LIB_SUBDIRS:
            candidate = self.root / sub / "pkgconfig"
            if candidate.is_dir():
                paths.append(candidate)
        for candidate in (self.root / "usr" / "share" / "pkgconfig",):
            if candidate.is_dir():
                paths.append(candidate)
        return paths

    def library_paths(self) -> List[Path]:
        return [self.root / sub for sub in LIB_SUBDIRS if (self.root / sub).is_dir()]

    def env(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Environment additions that make ``node-gyp`` find the sysroot."""

        if not self.available:
            return {}
        env: Dict[str, str] = {}
        pkg_config = [str(p) for p in self.pkg_config_paths()]
        existing = (base or os.environ).get("PKG_CONFIG_PATH")
        if existing:
            pkg_config.append(existing)
        if pkg_config:
            env["PKG_CONFIG_PATH"] = os.pathsep.join(pkg_config)
        include = str(self.include_dir)
        env["CPLUS_INCLUDE_PATH"] = os.pathsep.join([include, (base or os.environ).get("CPLUS_INCLUDE_PATH", "")]).strip(os.pathsep)
        env["C_INCLUDE_PATH"] = os.pathsep.join([include, (base or os.environ).get("C_INCLUDE_PATH", "")]).strip(os.pathsep)
        libraries = [str(p) for p in self.library_paths()]
        if libraries:
            lib_path = os.pathsep.join(libraries)
            existing_lib = (base or os.environ).get("LIBRARY_PATH")
            if existing_lib:
                lib_path = os.pathsep.join([lib_path, existing_lib])
            env["LIBRARY_PATH"] = lib_path
            env["LD_LIBRARY_PATH"] = os.pathsep.join(
                [lib_path, (base or os.environ).get("LD_LIBRARY_PATH", "")]
            ).strip(os.pathsep)
        return env

    def describe(self) -> str:
        if not self.available:
            return "sysroot: unavailable"
        return f"sysroot: {self.root} ({', '.join(self.packages)})"


def _apt_download(packages: Sequence[str], dest: Path) -> List[Path]:
    if not shutil.which("apt-get"):
        return []
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["apt-get", "download", *packages]
    log("downloading headers: %s", " ".join(packages))
    proc = subprocess.run(
        cmd,
        cwd=str(dest),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        utils.debug("apt-get download failed: %s", (proc.stdout or "")[-500:])
        return []
    return sorted(dest.glob("*.deb"))


def _extract_debs(debs: Sequence[Path], root: Path) -> bool:
    extracted = False
    for deb in debs:
        try:
            _dpkg_extract(Path(deb), root)
            extracted = True
        except Exception as exc:  # noqa: BLE001 - never fail the build over headers
            utils.debug("unable to unpack %s: %s", deb.name, exc)
    return extracted


def _dpkg_extract(deb: Path, root: Path) -> None:
    if shutil.which("dpkg-deb"):
        utils.run(["dpkg-deb", "-x", str(deb), str(root)])
        return
    utils.extract(deb, root)


def provision(
    cache_root: Path,
    packages: Sequence[str] = (),
    force: bool = False,
) -> Sysroot:
    """Create (once) and return a sysroot with the requested ``-dev`` packages."""

    root = Path(cache_root) / "sysroot"
    marker = root / ".provisioned"
    wanted = [p for p in packages]
    if marker.exists() and not force:
        known = set(marker.read_text(encoding="utf-8").split())
        if set(wanted).issubset(known):
            return Sysroot(root=root, packages=sorted(wanted), available=True)
    if not wanted:
        return Sysroot(root=root, packages=[], available=root.exists() and any(root.iterdir()))
    debs_dir = Path(cache_root) / "debs"
    debs = _apt_download(wanted, debs_dir)
    if not debs:
        warn("unable to download build headers (%s); modules needing them will be skipped", ", ".join(wanted))
        return Sysroot(root=root, packages=[], available=False)
    root.mkdir(parents=True, exist_ok=True)
    extracted = _extract_debs(debs, root)
    if not extracted:
        warn("unable to unpack build headers for %s", ", ".join(wanted))
        return Sysroot(root=root, packages=[], available=False)
    marker.write_text("\n".join(sorted(wanted)) + "\n", encoding="utf-8")
    log("sysroot ready at %s", root)
    return Sysroot(root=root, packages=list(wanted), available=True)


def packages_for(module: str) -> List[str]:
    """Return the ``-dev`` packages a given native module needs."""

    mapping = {
        "native-keymap": ["xkbcommon", "xkbfile"],
        "kerberos": ["krb5"],
        "@vscode/sqlite3": [],
        "@parcel/watcher": [],
        "node-pty": [],
        "@vscode/spdlog": [],
        "@vscode/policy-watcher": [],
        "native-is-elevated": [],
        "@vscode/native-watchdog": [],
    }
    result: List[str] = []
    for key in mapping.get(module, []):
        result.extend(DEFAULT_PACKAGES.get(key, []))
    return result
