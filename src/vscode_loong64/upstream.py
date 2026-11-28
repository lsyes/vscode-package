"""Version discovery for upstream Visual Studio Code.

Two independent upstream sources are supported:

``update`` (default)
    The public update API at ``https://update.code.visualstudio.com``.  It is the
    same endpoint the shipped application uses, therefore it always reflects what
    Microsoft currently publishes for the stable channel.

``releases``
    Scrapes ``https://code.visualstudio.com/updates`` / the release feed. Used as a
    fallback when the update API is unreachable.

The module is also able to answer "which versions are published already" so that
CI runs can skip work when nothing changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import utils
from .utils import BuildError, log, warn

UPDATE_BASE = "https://update.code.visualstudio.com/api"
RELEASES_URL = "https://update.code.visualstudio.com/api/releases"

#: official binary "flavours" that can be used as a source for a loong64 repack
LINUX_SOURCE_TARGETS = ("linux-x64", "linux-arm64", "linux-armhf")

#: package formats we know how to unpack from the official downloads
SOURCE_PACKAGES = ("tar.gz", "deb", "rpm")


@dataclass
class ReleaseInfo:
    """Metadata for one published upstream VSCode release."""

    version: str
    commit: str
    quality: str = "stable"
    platform: str = "linux-x64"
    timestamp: int = 0
    product_version: Optional[str] = None
    url: Optional[str] = None
    sha256: Optional[str] = None
    #: SHA-1 of the installer (``hash`` in the update API); *not* the commit
    sha1: Optional[str] = None
    notes: Optional[str] = None
    raw: Dict[str, object] = field(default_factory=dict)

    @property
    def short_commit(self) -> str:
        return self.commit[:8]

    @property
    def tag(self) -> str:
        """Release tag used on GitHub for the loong64 build."""
        return f"v{self.version}-loong64"

    def as_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "commit": self.commit,
            "quality": self.quality,
            "platform": self.platform,
            "timestamp": self.timestamp,
            "productVersion": self.product_version,
            "url": self.url,
            "sha256": self.sha256,
            "sha1": self.sha1,
            "tag": self.tag,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ReleaseInfo":
        return cls(
            version=str(data["version"]),
            commit=str(data["commit"]),
            quality=str(data.get("quality", "stable")),
            platform=str(data.get("platform", "linux-x64")),
            timestamp=int(data.get("timestamp") or 0),
            product_version=data.get("productVersion"),  # type: ignore[arg-type]
            url=data.get("url"),  # type: ignore[arg-type]
            sha256=data.get("sha256"),  # type: ignore[arg-type]
            sha1=data.get("sha1"),  # type: ignore[arg-type]
            notes=data.get("notes"),  # type: ignore[arg-type]
        )


def latest_release(platform: str = "linux-x64", quality: str = "stable") -> ReleaseInfo:
    """Return the newest published release for ``platform``."""

    url = f"{UPDATE_BASE}/update/{platform}/{quality}/latest"
    data = utils.http_get_json(url)
    if not isinstance(data, dict) or "version" not in data:
        raise BuildError(f"unexpected payload from {url}: {str(data)[:200]}")
    # ``version`` is the source commit; ``hash`` is the SHA-1 of the installer
    # and must never be used as the commit.
    return ReleaseInfo(
        version=str(data.get("productVersion") or data.get("name") or ""),
        commit=str(data.get("version") or ""),
        quality=quality,
        platform=platform,
        timestamp=int(data.get("timestamp") or 0),
        product_version=data.get("productVersion"),  # type: ignore[arg-type]
        url=data.get("url"),  # type: ignore[arg-type]
        sha256=data.get("sha256hash"),  # type: ignore[arg-type]
        sha1=str(data.get("hash") or "") or None,
        notes=data.get("notes"),  # type: ignore[arg-type]
        raw=data,
    )


def specific_release(version: str, platform: str = "linux-x64", quality: str = "stable") -> ReleaseInfo:
    """Return metadata for an explicitly requested version.

    The endpoint is ``/api/versions/<version>/<platform>/<quality>``.  The older
    ``/api/update/<platform>/<quality>/<version>`` shape takes a *commit* in its
    last segment, so passing a marketing version there always returns 404.
    """

    url = f"{UPDATE_BASE}/versions/{version}/{platform}/{quality}"
    data = utils.http_get_json(url)
    if not isinstance(data, dict) or "url" not in data:
        raise BuildError(f"version {version} is not available for {platform}/{quality}")
    info = ReleaseInfo(
        version=str(data.get("productVersion") or version),
        commit=str(data.get("version") or ""),
        quality=quality,
        platform=platform,
        timestamp=int(data.get("timestamp") or 0),
        product_version=data.get("productVersion"),  # type: ignore[arg-type]
        url=data.get("url"),  # type: ignore[arg-type]
        sha256=data.get("sha256hash"),  # type: ignore[arg-type]
        sha1=str(data.get("hash") or "") or None,
        notes=data.get("notes"),  # type: ignore[arg-type]
        raw=data,
    )
    return info


def list_releases(quality: str = "stable", limit: Optional[int] = None) -> List[str]:
    data = utils.http_get_json(f"{RELEASES_URL}/{quality}")
    if not isinstance(data, list):
        raise BuildError(f"unexpected releases payload: {str(data)[:200]}")
    versions = [str(v) for v in data]
    return versions[:limit] if limit else versions


def resolve_version(
    version: Optional[str] = None,
    platform: str = "linux-x64",
    quality: str = "stable",
) -> ReleaseInfo:
    """Resolve ``version`` (``latest``/``none``/explicit) into a ReleaseInfo."""

    if version in (None, "", "latest"):
        return latest_release(platform=platform, quality=quality)
    if version == "releases":
        raise BuildError("'releases' is not a valid version selector")
    return specific_release(version, platform=platform, quality=quality)


def download_url_for(version: str, platform: str = "linux-x64", package: str = "tar.gz", quality: str = "stable") -> str:
    """Build a direct download URL (used when the API cannot be reached).

    This endpoint redirects to the current installer for ``version`` and also
    accepts a commit in place of the marketing version, so it keeps working when
    the metadata API is down.
    """

    return f"https://update.code.visualstudio.com/{version}/{platform}/{quality}?packageType={package}"


def parse_checksum_file(text: str, filename: str) -> Optional[str]:
    """Extract the sha256 of ``filename`` from a ``SHASUMS256.txt`` style file."""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*./") == filename:
            return parts[0].lower()
    return None


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 comparing dotted version strings (missing parts sort as 0)."""

    def parts(value: str) -> List[int]:
        return [int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", value) if p]

    a, b = parts(left), parts(right)
    length = max(len(a), len(b))
    a += [0] * (length - len(a))
    b += [0] * (length - len(b))
    return (a > b) - (a < b)


def should_build(
    wanted: ReleaseInfo,
    published_tags: Sequence[str],
    force: bool = False,
) -> bool:
    """Decide whether a new build has to run."""

    if force:
        return True
    if not published_tags:
        return True
    normalised = {t.lstrip("v") for t in published_tags}
    for candidate in (
        f"{wanted.version}-loong64",
        f"{wanted.version}-loong64-beta",
    ):
        if candidate in normalised:
            return False
    return True


def latest_or_none(platform: str = "linux-x64", quality: str = "stable") -> Optional[ReleaseInfo]:
    """Like :func:`latest_release` but returns ``None`` instead of raising.

    Used by the pipeline to report "upstream moved ahead" without turning a
    transient API outage into a failed build.
    """

    try:
        return latest_release(platform=platform, quality=quality)
    except BuildError as exc:
        warn("unable to query the newest upstream release: %s", exc)
        return None


def change_report(current: str, latest: Optional[str], quality: str = "stable") -> Dict[str, object]:
    """Describe whether ``current`` is behind ``latest`` and which tag sources use."""

    report: Dict[str, object] = {
        "current": current,
        "latest": latest,
        "quality": quality,
        "currentTag": None,
        "latestTag": None,
        "update": False,
    }
    if current and current not in ("", "latest", "0.0.0"):
        report["currentTag"] = current
    if latest:
        report["latestTag"] = latest
    if not current or current in ("", "latest"):
        report["update"] = bool(latest)
    elif latest:
        report["update"] = compare_versions(latest, current) > 0
    return report


def load_state(path: Path) -> Dict[str, object]:
    import json

    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        warn("ignoring unreadable state file %s: %s", path, exc)
        return {}


def save_state(path: Path, data: Dict[str, object]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)
    log("state written to %s", path)
