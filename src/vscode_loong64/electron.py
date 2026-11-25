"""Electron runtime resolution for the LoongArch (loong64) architecture.

Upstream VSCode ships an official Electron build only for x64/arm64/armhf.  The
LoongArch community provides compatible builds of the same Electron versions in
``darkyzhou/electron-loong64``.  This module:

* detects which Electron version a given VSCode build was compiled against,
* finds the closest available loong64 build of that Electron release,
* downloads (with caching) and verifies it,
* exposes the extracted runtime directory for the packager.

Environment variables understood here mirror electron-builder/``@electron/get``:

``ELECTRON_MIRROR``
    Mirror prefix, e.g. ``https://github.com/darkyzhou/electron-loong64/releases/download/``.
    ``${ELECTRON_MIRROR}v43.7.7/electron-v43.7.7-linux-loong64.zip`` is requested.
``ELECTRON_CUSTOM_DIR``
    Directory (tag) between mirror and file name, default ``v{version}``.
``ELECTRON_CUSTOM_FILENAME``
    Explicit file name template, supports ``{version}`` and ``{arch}``.
``ELECTRON_LOONG64_CACHE``
    Where downloads are cached, default ``~/.cache/electron-loong64``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import utils
from .utils import BuildError, log, warn

DEFAULT_MIRROR = "https://github.com/darkyzhou/electron-loong64/releases/download/"
RUNTIME_ARCH = "loong64"

#: files that identify a usable Electron runtime directory
REQUIRED_RUNTIME_FILES = ("electron", "resources", "locales", "icudtl.dat")

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class ElectronRuntime:
    """An extracted Electron runtime for loong64."""

    version: str
    arch: str
    root: Path
    binary: Path
    source: str = "remote"
    requested_version: Optional[str] = None

    @property
    def exact_match(self) -> bool:
        return self.requested_version in (None, self.version)

    def describe(self) -> str:
        suffix = "" if self.exact_match else f" (requested {self.requested_version})"
        return f"Electron {self.version} {self.arch}{suffix}"


def cache_dir() -> Path:
    env = os.environ.get("ELECTRON_LOONG64_CACHE") or os.environ.get("ELECTRON_CACHE")
    if env:
        candidate = Path(env).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            pass
    candidates = [
        Path.home() / ".cache" / "electron-loong64",
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "electron-loong64",
        Path.cwd() / ".cache" / "electron-loong64",
        Path(tempfile.gettempdir()) / "electron-loong64",
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise BuildError("no writable Electron cache directory; set ELECTRON_LOONG64_CACHE")


def electron_mirror() -> str:
    mirror = os.environ.get("ELECTRON_MIRROR")
    if mirror:
        if "{{" in mirror or "{version}" in mirror:
            # electron-builder style template
            return mirror
        return mirror if mirror.endswith("/") else mirror + "/"
    return DEFAULT_MIRROR


def asset_name(version: str, arch: str = RUNTIME_ARCH) -> str:
    template = os.environ.get("ELECTRON_CUSTOM_FILENAME")
    if template:
        return template.format(version=version, arch=arch)
    return f"electron-v{version}-linux-{arch}.zip"


def download_url(version: str, arch: str = RUNTIME_ARCH) -> str:
    mirror = electron_mirror()
    directory = os.environ.get("ELECTRON_CUSTOM_DIR", "v{version}").format(version=version)
    name = asset_name(version, arch)
    if "{version}" in mirror or "{{" in mirror:
        url = mirror.replace("{{ version }}", version).replace("{version}", version)
        url = url.replace("{{ arch }}", arch).replace("{arch}", arch)
        if not url.endswith("/"):
            url += "/"
        return url + name
    return f"{mirror}{directory}/{name}"


def version_tuple(version: str) -> Tuple[int, int, int]:
    match = VERSION_RE.search(version or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


# --------------------------------------------------------------------------------------
# version discovery
# --------------------------------------------------------------------------------------

def detect_required_version(product: Dict[str, object], package: Optional[Dict[str, object]] = None) -> Optional[str]:
    """Figure out which Electron version an extracted VSCode build needs."""

    for key in ("electronVersion", "electron"):
        value = (product or {}).get(key)
        if isinstance(value, str) and VERSION_RE.search(value):
            return value

    native = (package or {}).get("nativeBuild")
    if isinstance(native, dict):
        target = native.get("target")
        if isinstance(target, str) and VERSION_RE.search(target):
            return target

    return None


def list_mirror_releases(repo: str = "darkyzhou/electron-loong64", timeout: int = 60) -> List[str]:
    """List versions published on the loong64 Electron mirror (newest first)."""

    tags: List[str] = []
    try:
        payload = utils.http_get_json(
            f"https://api.github.com/repos/{repo}/releases?per_page=100", timeout=timeout
        )
        if isinstance(payload, list):
            for entry in payload:
                tag = str(entry.get("tag_name", "")).lstrip("v")
                if VERSION_RE.fullmatch(tag):
                    tags.append(tag)
    except BuildError as exc:
        warn("GitHub API unavailable (%s); falling back to release page scraping", exc)
    if tags:
        return sorted(set(tags), key=version_tuple, reverse=True)

    # Fallback: scrape the releases page (works without API quota).
    html = utils.http_get_html(f"https://github.com/{repo}/releases")
    for match in re.finditer(r"electron-v(\d+\.\d+\.\d+)-linux-loong64\.zip", html):
        tags.append(match.group(1))
    return sorted(set(tags), key=version_tuple, reverse=True)


def available_versions(arch: str = RUNTIME_ARCH) -> List[str]:
    """Versions that can actually be downloaded (probed lazily by callers)."""

    return list_mirror_releases()


def pick_best_version(requested: str, candidates: Iterable[str]) -> Tuple[Optional[str], str]:
    """Choose the Electron build closest to ``requested``.

    Preference order: exact match, then the newest build of the same minor, then
    the newest build of the same major, then any build newer than the request,
    and finally the newest available build.  Returns ``(version, reason)``.
    """

    wanted = version_tuple(requested)
    ordered = sorted({c for c in candidates if VERSION_RE.fullmatch(c)}, key=version_tuple, reverse=True)
    if not ordered:
        return None, "no candidate versions available"

    for candidate in ordered:
        if version_tuple(candidate) == wanted:
            return candidate, "exact match"

    same_minor = [c for c in ordered if version_tuple(c)[:2] == wanted[:2]]
    if same_minor:
        return same_minor[0], "same minor series"

    same_major = [c for c in ordered if version_tuple(c)[0] == wanted[0]]
    if same_major:
        newer_or_equal = [c for c in same_major if version_tuple(c) >= wanted]
        return (newer_or_equal or same_major)[0], "same major series"

    newer = [c for c in ordered if version_tuple(c) > wanted]
    if newer:
        return newer[0], "newest available below the requested major"

    return ordered[0], "newest available"


def url_exists(url: str, timeout: int = 30) -> bool:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": utils.USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code == 200
    except Exception:  # noqa: BLE001 - network probing must not crash the build
        return False


# --------------------------------------------------------------------------------------
# cache / extraction
# --------------------------------------------------------------------------------------

def _runtime_marker(root: Path) -> Path:
    return root / ".loong64-runtime.json"


def _looks_like_runtime(root: Path) -> bool:
    return all((root / name).exists() for name in REQUIRED_RUNTIME_FILES)


def find_cached(version: str, arch: str = RUNTIME_ARCH) -> Optional[Path]:
    root = cache_dir() / f"v{version}-{arch}"
    marker = _runtime_marker(root)
    if marker.exists() and _looks_like_runtime(root):
        return root
    return None


def cached_versions(arch: str = RUNTIME_ARCH) -> List[str]:
    root = cache_dir()
    if not root.exists():
        return []
    found = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        match = re.fullmatch(rf"v(.+)-{re.escape(arch)}", entry.name)
        if match and _looks_like_runtime(entry):
            found.append(match.group(1))
    return sorted(found, key=version_tuple, reverse=True)


def _extract_runtime(archive: Path, dest: Path, arch: str) -> Path:
    scratch = dest.parent / (dest.name + ".extract")
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    utils.extract(archive, scratch)
    root = scratch
    candidates = [scratch] + [p for p in scratch.iterdir() if p.is_dir()]
    for candidate in candidates:
        if _looks_like_runtime(candidate):
            root = candidate
            break
    else:
        raise BuildError(f"archive {archive.name} does not contain an Electron runtime")
    # Electron archive ships the executable as `electron`; VSCode expects its own
    # product name, the rename happens in the packager.
    if dest.exists():
        shutil.rmtree(dest)
    root.replace(dest)
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    return dest


def fetch(
    version: str,
    arch: str = RUNTIME_ARCH,
    force: bool = False,
    offline: bool = False,
    fallback: bool = True,
) -> ElectronRuntime:
    """Get a ready-to-use Electron runtime for ``version``.

    Uses the local cache when possible.  When ``fallback`` is enabled and the
    exact version cannot be downloaded, the closest available build is used and a
    warning is emitted (LoongArch Electron builds lag behind upstream).
    """

    requested = version
    cached = None if force else find_cached(version, arch)
    if cached:
        log("using cached %s", cached)
        return ElectronRuntime(version=version, arch=arch, root=cached, binary=cached / "electron", source="cache", requested_version=requested)

    if offline:
        raise BuildError(
            f"Electron {version} {arch} is not cached and --offline was requested (looked in {cache_dir()})"
        )

    attempt_version = version
    archive: Optional[Path] = None
    reason = ""
    errors: List[str] = []

    while True:
        url = download_url(attempt_version, arch)
        target = cache_dir() / "downloads" / asset_name(attempt_version, arch)
        try:
            archive = utils.download(url, target)
            break
        except BuildError as exc:
            errors.append(f"{attempt_version}: {exc}")
        if not fallback:
            raise BuildError(
                f"unable to download Electron {attempt_version} for {arch} from {url}: {errors[-1]}"
            )
        if attempt_version != version:
            raise BuildError(
                f"unable to download any loong64 Electron build for VSCode requiring {version}:\n  "
                + "\n  ".join(errors)
            )
        candidates = [c for c in list_mirror_releases() if c != version]
        best, reason = pick_best_version(version, candidates)
        if not best:
            raise BuildError(
                f"unable to download Electron {version} for {arch} and no fallback is published:\n  "
                + "\n  ".join(errors)
            )
        warn(
            "Electron %s is not published for %s (%s); falling back to %s (%s)",
            version,
            arch,
            errors[-1],
            best,
            reason,
        )
        attempt_version = best

    assert archive is not None
    dest = cache_dir() / f"v{attempt_version}-{arch}"
    log("extracting %s", archive.name)
    _extract_runtime(archive, dest, arch)
    marker = _runtime_marker(dest)
    marker.write_text(
        json.dumps(
            {
                "version": attempt_version,
                "requestedVersion": version,
                "arch": arch,
                "source": utils.sha256_file(archive),
                "url": download_url(attempt_version, arch),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    log("Electron runtime ready: %s", dest)
    return ElectronRuntime(
        version=attempt_version,
        arch=arch,
        root=dest,
        binary=dest / "electron",
        source="download",
        requested_version=version,
    )


def detect_archive_version(archive: Path) -> Optional[str]:
    """Read the ``version`` marker inside an Electron archive."""

    archive = Path(archive)
    try:
        if archive.suffix.lower() == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for name in ("version", "./version"):
                    try:
                        return zf.read(name).decode("utf-8").strip()
                    except KeyError:
                        continue
        elif archive.name.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if member.name.rstrip("/").endswith("/version"):
                        handle = tf.extractfile(member)
                        if handle:
                            return handle.read().decode("utf-8").strip()
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    return None


def import_local(archive: Path, version: Optional[str] = None, arch: str = RUNTIME_ARCH, force: bool = False) -> ElectronRuntime:
    """Register an already downloaded Electron archive in the cache.

    When ``version`` is omitted (or ``"auto"``) the version marker shipped inside
    the archive is used, which keeps the cache consistent with what is really on
    disk.
    """

    archive = Path(archive).expanduser().resolve()
    if not archive.exists():
        raise BuildError(f"Electron archive not found: {archive}")
    detected = detect_archive_version(archive)
    if version in (None, "", "auto"):
        if not detected:
            raise BuildError(f"unable to detect the Electron version of {archive.name}; pass one explicitly")
        version = detected
    elif detected and detected != version:
        warn("archive %s contains Electron %s (requested %s)", archive.name, detected, version)

    dest = cache_dir() / f"v{version}-{arch}"
    if force or not _looks_like_runtime(dest):
        log("importing local Electron runtime %s", archive)
        _extract_runtime(archive, dest, arch)
        _runtime_marker(dest).write_text(
            json.dumps(
                {
                    "version": version,
                    "requestedVersion": version,
                    "arch": arch,
                    "source": "local:" + str(archive),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return ElectronRuntime(
        version=version, arch=arch, root=dest, binary=dest / "electron", source="local", requested_version=version
    )


def verify(runtime: ElectronRuntime, run_binary: bool = True) -> Dict[str, object]:
    """Sanity check an Electron runtime directory."""

    problems: List[str] = []
    if not runtime.root.is_dir():
        raise BuildError(f"Electron runtime directory missing: {runtime.root}")
    for name in REQUIRED_RUNTIME_FILES:
        if not (runtime.root / name).exists():
            problems.append(f"missing {name}")
    binary = runtime.binary
    if not binary.exists():
        problems.append("missing electron executable")
    else:
        import struct

        with open(binary, "rb") as fh:
            head = fh.read(20)
        if head[:4] != b"\x7fELF":
            problems.append("electron executable is not an ELF binary")
        else:
            machine = struct.unpack("<H", head[18:20])[0]
            if machine != 258:  # EM_LOONGARCH
                problems.append(f"electron executable is not LoongArch (e_machine={machine})")
    if problems:
        raise BuildError("invalid Electron runtime: " + ", ".join(problems))

    info: Dict[str, object] = {"root": str(runtime.root), "version": runtime.version}
    if run_binary and shutil.which("file"):
        proc = utils.run(["file", "-b", str(binary)], check=False)
        info["file"] = (proc.stdout or "").strip()
    return info


def describe_cache() -> List[Dict[str, object]]:
    entries: List[Dict[str, object]] = []
    root = cache_dir()
    if not root.exists():
        return entries
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and entry.name != "downloads":
            marker = _runtime_marker(entry)
            data: Dict[str, object] = {}
            if marker.exists():
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            entries.append({"path": str(entry), "size": size, **data})
    return entries
