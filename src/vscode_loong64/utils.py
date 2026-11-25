"""Small shared helpers: logging, process execution, hashing, downloads, archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional, Sequence

# --------------------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------------------

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_LEVEL = _LEVELS[os.environ.get("VSCODE_LOONG64_LOG", "info").lower()]
_QUIET = os.environ.get("VSCODE_LOONG64_QUIET") == "1"
_ANSI = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _colorize(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI else text


def log(message: str, *args: object, level: str = "info") -> None:
    if _LEVELS[level] < _LEVEL:
        return
    if _QUIET and level != "error":
        return
    if args:
        message = message % args
    prefix = {
        "debug": _colorize("·", "90"),
        "info": _colorize("→", "36"),
        "warn": _colorize("!", "33"),
        "error": _colorize("✗", "31"),
    }[level]
    print(f"{prefix} {message}", file=sys.stderr, flush=True)


def debug(message: str, *args: object) -> None:
    log(message, *args, level="debug")


def warn(message: str, *args: object) -> None:
    log(message, *args, level="warn")


class BuildError(RuntimeError):
    """Raised when a build step cannot be completed."""


def section(title: str) -> None:
    if _QUIET:
        return
    sys.stdout.flush()  # keep stdout/stderr interleaving readable
    line = "─" * max(8, 72 - len(title))
    print(f"\n{_colorize(title, '1;35')} {_colorize(line, '90')}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------------------

def run(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = True,
    capture: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command, streaming nothing but returning captured output."""
    cmd = [str(c) for c in cmd]
    debug("exec: %s (cwd=%s)", " ".join(cmd), cwd or os.getcwd())
    full_env = os.environ.copy()
    if env:
        full_env.update({k: str(v) for k, v in env.items()})
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        errors="replace",
    )
    if proc.returncode != 0:
        if check:
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
            raise BuildError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{tail}")
        if not quiet:
            warn("command failed (%s): %s", proc.returncode, " ".join(cmd))
    elif not quiet and capture:
        out = (proc.stdout or "").strip()
        if out:
            debug(out[-2000:])
    return proc


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def require_tool(name: str, hint: str = "") -> str:
    path = shutil.which(name)
    if not path:
        msg = f"required tool not found in PATH: {name}"
        if hint:
            msg += f" ({hint})"
        raise BuildError(msg)
    return path


# --------------------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}PiB"


def find_checksum(checksums: Mapping[str, str], relative_path: str) -> Optional[str]:
    """VSCode product.json checksums are base64 sha256, keyed by path inside out/."""
    value = checksums.get(relative_path.replace(os.sep, "/"))
    if value is None:
        return None
    try:
        import base64

        return base64.b64decode(value).hex()
    except Exception:  # noqa: BLE001 - malformed checksum should never break a build
        return None


# --------------------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------------------

USER_AGENT = "vscode-loong64-builder/0.1 (+https://github.com)"

DEFAULT_MIRRORS = {
    # prefix -> list of mirrors tried in order after the original URL
    "https://update.code.visualstudio.com": ["https://update.code.visualstudio.com"],
    "https://vscode.download.prss.microsoft.com": [
        "https://vscode.download.prss.microsoft.com",
    ],
    "https://github.com/darkyzhou/electron-loong64": [
        "https://github.com/darkyzhou/electron-loong64",
        "https://gh-proxy.com/https://github.com/darkyzhou/electron-loong64",
        "https://ghfast.top/https://github.com/darkyzhou/electron-loong64",
    ],
    "https://api.github.com": ["https://api.github.com"],
}


def _mirror_candidates(url: str) -> Iterator[str]:
    seen = set()
    yield_candidates = []
    yield_candidates.append(url)
    for prefix, mirrors in DEFAULT_MIRRORS.items():
        if url.startswith(prefix):
            for mirror in mirrors:
                yield_candidates.append(mirror + url[len(prefix):])
    env_proxy = os.environ.get("VSCODE_LOONG64_DOWNLOAD_PROXY")
    if env_proxy:
        for candidate in list(yield_candidates):
            if candidate.startswith(("http://", "https://")) and "github" in candidate:
                yield_candidates.append(f"{env_proxy.rstrip('/')}/{candidate}")
    for candidate in yield_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def http_get_json(url: str, timeout: int = 60, headers: Optional[Mapping[str, str]] = None) -> object:
    last_error: Optional[Exception] = None
    for candidate in _mirror_candidates(url):
        req = urllib.request.Request(candidate, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
            return json.loads(payload.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            last_error = exc
            debug("GET %s failed: %s", candidate, exc)
            continue
    raise BuildError(f"unable to fetch JSON from {url}: {last_error}")


def http_get_text(url: str, timeout: int = 60) -> str:
    last_error: Optional[Exception] = None
    for candidate in _mirror_candidates(url):
        req = urllib.request.Request(candidate, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            debug("GET %s failed: %s", candidate, exc)
            continue
    raise BuildError(f"unable to fetch {url}: {last_error}")


def http_get_html(url: str, timeout: int = 60) -> str:
    """Fetch an HTML page following redirects (thin alias, kept for readability)."""

    return http_get_text(url, timeout=timeout)


def download(
    url: str,
    dest: Path,
    expected_sha256: Optional[str] = None,
    expected_size: Optional[int] = None,
    retries: int = 3,
) -> Path:
    """Download ``url`` to ``dest``, resuming and verifying when possible.

    Returns the path to the downloaded file (``dest``).  Files that already exist
    and match the expected digest are reused, which makes local rebuilds cheap.
    """
    dest = Path(dest)
    if dest.exists() and expected_sha256 and sha256_file(dest) == expected_sha256:
        log("cached %s (%s)", dest.name, human_size(dest.stat().st_size))
        return dest
    if dest.exists() and expected_size and dest.stat().st_size == expected_size and not expected_sha256:
        log("cached %s (%s)", dest.name, human_size(dest.stat().st_size))
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_error: Optional[Exception] = None

    for candidate in _mirror_candidates(url):
        for attempt in range(1, retries + 1):
            offset = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": USER_AGENT}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            req = urllib.request.Request(candidate, headers=headers)
            log("downloading %s%s", candidate, f" (resume @{human_size(offset)})" if offset else "")
            try:
                started = time.time()
                with urllib.request.urlopen(req, timeout=120) as resp:
                    mode = "ab" if offset and resp.status == 206 else "wb"
                    if mode == "wb":
                        offset = 0
                    total = resp.headers.get("Content-Length")
                    total = int(total) + offset if total else (expected_size or 0)
                    done = offset
                    last_report = 0.0
                    with open(part, mode) as fh:
                        while True:
                            block = resp.read(1 << 20)
                            if not block:
                                break
                            fh.write(block)
                            done += len(block)
                            now = time.time()
                            if now - last_report > 5:
                                last_report = now
                                speed = done / max(now - started, 0.001)
                                pct = f"{done * 100 / total:.1f}%" if total else "?"
                                debug("  %s / %s (%s, %s/s)", human_size(done), human_size(total) if total else "?", pct, human_size(speed))
                if expected_size and part.stat().st_size != expected_size:
                    raise BuildError(
                        f"size mismatch for {dest.name}: got {part.stat().st_size}, expected {expected_size}"
                    )
                digest = sha256_file(part)
                if expected_sha256 and digest != expected_sha256:
                    part.unlink(missing_ok=True)
                    raise BuildError(
                        f"sha256 mismatch for {dest.name}: got {digest}, expected {expected_sha256}"
                    )
                part.replace(dest)
                log("downloaded %s (%s)", dest.name, human_size(dest.stat().st_size))
                return dest
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403, 404):
                    warn("mirror rejected request (%s): %s", exc.code, candidate)
                    break  # try the next mirror, retrying will not help
                warn("download attempt %s/%s failed: %s", attempt, retries, exc)
                time.sleep(min(2 ** attempt, 10))
    raise BuildError(f"unable to download {url}: {last_error}")


# --------------------------------------------------------------------------------------
# archives
# --------------------------------------------------------------------------------------

def extract_zip(archive: Path, dest: Path, members: Optional[Iterable[str]] = None) -> None:
    """Extract a zip archive preserving unix mode bits.

    ``zipfile.ZipFile.extractall`` drops permissions, which breaks the Electron
    runtime (``chrome_crashpad_handler`` and friends must stay executable).
    """

    dest.mkdir(parents=True, exist_ok=True)
    wanted = set(members) if members is not None else None
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename
            if wanted is not None and not any(
                name == w or name.startswith(w.rstrip("/") + "/") for w in wanted
            ):
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            is_link = (mode & 0o170000) == 0o120000
            target = dest / name
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if is_link:
                link_target = zf.read(info).decode("utf-8")
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(link_target, target)
                continue
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            if mode & 0o7777:
                os.chmod(target, mode & 0o7777)
            elif mode:
                os.chmod(target, 0o644)


def extract_tar(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="fully_trusted")
        except TypeError:  # Python < 3.12
            tf.extractall(dest)


def extract(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    if name.endswith(".zip"):
        extract_zip(archive, dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")):
        extract_tar(archive, dest)
    elif name.endswith(".deb"):
        if not shutil.which("ar"):
            raise BuildError("`ar` is required to unpack .deb archives (binutils)")
        dest.mkdir(parents=True, exist_ok=True)
        run(["ar", "x", str(archive.absolute())], cwd=dest)
        for member in ("data.tar.xz", "data.tar.zst", "data.tar.gz", "data.tar"):
            candidate = dest / member
            if candidate.exists():
                data_dir = dest / "_data"
                data_dir.mkdir(exist_ok=True)
                if member.endswith(".zst"):
                    require_tool("zstd", "apt install zstd")
                    run(["tar", "-xf", str(candidate), "-C", str(data_dir)])
                else:
                    run(["tar", "-xf", str(candidate), "-C", str(data_dir)])
                return
        raise BuildError(f"no data.tar.* member found in {archive}")
    elif name.endswith(".rpm"):
        require_tool("rpm2cpio", "apt install rpm2cpio rpm")
        require_tool("cpio")
        dest.mkdir(parents=True, exist_ok=True)
        with open(dest / "payload.cpio", "wb") as fh:
            subprocess.run(["rpm2cpio", str(archive.absolute())], stdout=fh, check=True)
        run(["cpio", "-idmu", "--quiet"], cwd=dest, capture=False)
        # ignore failures on device nodes etc.
    elif name.endswith(".dmg"):
        raise BuildError("macOS images cannot be repacked by this tool")
    else:
        raise BuildError(f"unsupported archive type: {archive.name}")


def copy_tree(src: Path, dst: Path, symlinks: bool = True) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=symlinks, dirs_exist_ok=False)


def ensure_dir(path: Path, clean: bool = False) -> Path:
    if clean and path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------------------

ARCHIVE_RE = re.compile(r"(?i)(\.tar\.(gz|xz|bz2)|\.tgz|\.txz|\.zip|\.deb|\.rpm|\.dmg)$")


def strip_archive_suffix(name: str) -> str:
    return ARCHIVE_RE.sub("", name)
