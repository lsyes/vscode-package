"""Publish build artifacts to a GitHub release."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import utils
from .utils import BuildError, human_size, log, run, warn

API_ROOT = "https://api.github.com"


@dataclass
class ReleaseAsset:
    path: Path
    content_type: str = "application/octet-stream"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


@dataclass
class Publisher:
    """Uploads artifacts with the ``gh`` CLI (preferred) or the REST API."""

    repo: str
    tag: str
    token: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    draft: bool = False
    prerelease: bool = False
    clobber: bool = True

    # -- helpers -----------------------------------------------------------------

    @property
    def slug(self) -> str:
        return self.repo

    def _token(self) -> str:
        token = self.token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise BuildError("no GitHub token: pass --token or set GH_TOKEN/GITHUB_TOKEN")
        return token

    def _gh(self) -> Optional[str]:
        return utils.which("gh")

    # -- release management -------------------------------------------------------

    def ensure_release(self) -> Dict[str, object]:
        """Create the release if it does not exist yet and return its metadata."""

        gh = self._gh()
        if gh:
            proc = run([gh, "release", "view", self.tag, "--repo", self.slug, "--json", "databaseId,url,isDraft,tagName"], check=False)
            if proc.returncode == 0 and proc.stdout:
                try:
                    data = json.loads(proc.stdout)
                    log("release %s already exists: %s", self.tag, data.get("url"))
                    return data
                except ValueError:
                    pass
            args = [gh, "release", "create", self.tag, "--repo", self.slug, "--title", self.title or self.tag]
            if self.body:
                args += ["--notes", self.body]
            if self.draft:
                args.append("--draft")
            if self.prerelease:
                args.append("--prerelease")
            proc = run(args, check=False)
            if proc.returncode != 0:
                raise BuildError(f"gh release create failed: {(proc.stderr or proc.stdout or '').strip()}")
            log("created release %s", self.tag)
            return {"tagName": self.tag, "url": (proc.stdout or "").strip()}
        return self._ensure_release_rest()

    def _ensure_release_rest(self) -> Dict[str, object]:
        import urllib.error
        import urllib.request

        token = self._token()
        url = f"{API_ROOT}/repos/{self.slug}/releases/tags/{self.tag}"
        request = urllib.request.Request(url, headers=self._headers(token))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            log("release %s already exists", self.tag)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise BuildError(f"unable to query release {self.tag}: {exc}") from exc
        payload = {
            "tag_name": self.tag,
            "name": self.title or self.tag,
            "body": self.body or "",
            "draft": self.draft,
            "prerelease": self.prerelease,
        }
        request = urllib.request.Request(
            f"{API_ROOT}/repos/{self.slug}/releases",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(token),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
        log("created release %s", self.tag)
        return data

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": utils.USER_AGENT,
            "Content-Type": "application/json",
        }

    # -- assets -------------------------------------------------------------------

    def upload(self, files: Sequence[Path]) -> List[str]:
        gh = self._gh()
        missing = [str(f) for f in files if not Path(f).exists()]
        if missing:
            raise BuildError("cannot upload missing files: " + ", ".join(missing))
        if gh:
            return self._upload_gh(gh, files)
        return self._upload_rest(files)

    def _upload_gh(self, gh: str, files: Sequence[Path]) -> List[str]:
        names: List[str] = []
        for path in files:
            path = Path(path)
            if self.clobber:
                run([gh, "release", "delete-asset", self.tag, path.name, "--repo", self.slug, "--yes"], check=False)
            proc = run(
                [gh, "release", "upload", self.tag, str(path), "--repo", self.slug, "--clobber"],
                check=False,
            )
            if proc.returncode != 0:
                raise BuildError(f"uploading {path.name} failed: {(proc.stderr or proc.stdout or '').strip()}")
            log("uploaded %s (%s)", path.name, human_size(path.stat().st_size))
            names.append(path.name)
        return names

    def _upload_rest(self, files: Sequence[Path]) -> List[str]:
        import urllib.error
        import urllib.request

        token = self._token()
        release = self._ensure_release_rest()
        upload_url = re.sub(r"\{[^}]*\}", "", str(release["upload_url"]))
        names: List[str] = []
        for path in files:
            path = Path(path)
            if self.clobber:
                self._delete_asset_rest(token, release, path.name)
            url = f"{upload_url}?name={path.name}"
            with open(path, "rb") as fh:
                body = fh.read()
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "User-Agent": utils.USER_AGENT,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=1200) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                raise BuildError(f"uploading {path.name} failed ({exc.code}): {exc.read()[:400]!r}") from exc
            log("uploaded %s (%s)", path.name, human_size(path.stat().st_size))
            names.append(path.name)
        return names

    def _delete_asset_rest(self, token: str, release: Dict[str, object], name: str) -> None:
        import urllib.error
        import urllib.request

        assets = release.get("assets") or []
        if not isinstance(assets, list):
            return
        for asset in assets:
            if asset.get("name") != name:
                continue
            try:
                request = urllib.request.Request(
                    f"{API_ROOT}/repos/{self.slug}/releases/assets/{asset['id']}",
                    headers={"Authorization": f"Bearer {token}", "User-Agent": utils.USER_AGENT},
                    method="DELETE",
                )
                urllib.request.urlopen(request, timeout=60)
                log("removed previous asset %s", name)
            except urllib.error.HTTPError as exc:
                warn("unable to delete old asset %s: %s", name, exc)


def release_notes(manifest: Dict[str, object]) -> str:
    """Render a GitHub release body from a build manifest."""

    vscode = manifest.get("vscode") or {}
    electron = manifest.get("electron") or {}
    artifacts = manifest.get("artifacts") or []
    natives = (manifest.get("nativeModules") or {}).get("modules") or []
    lines: List[str] = []
    lines.append(f"## Visual Studio Code {vscode.get('version', '?')} for LoongArch64")
    lines.append("")
    lines.append(f"- Upstream commit: `{vscode.get('commit', '?')}`")
    lines.append(f"- Quality: `{vscode.get('quality', 'stable')}`")
    if electron:
        requested = electron.get("requested")
        version = electron.get("version")
        suffix = f" (upstream target {requested})" if requested and requested != version else ""
        lines.append(f"- Electron runtime: `{version}` loong64{suffix}")
    lines.append("")
    if artifacts:
        lines.append("### Artifacts")
        lines.append("")
        for artifact in artifacts:
            size = artifact.get("size") or 0
            sha = (artifact.get("sha256") or "")[:16]
            lines.append(f"- `{artifact.get('name')}` ({human_size(size)}, sha256 `{sha}…`)")
        lines.append("")
    if natives:
        lines.append("### Native modules")
        lines.append("")
        for entry in natives:
            lines.append(f"- `{entry.get('name')}`: {entry.get('status')}")
        lines.append("")
    warnings = manifest.get("warnings") or []
    if warnings:
        lines.append("### Notes")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")
    lines.append("Built from the official Microsoft binaries with the Electron LoongArch runtime.")
    lines.append("")
    lines.append("```bash")
    lines.append("# install the Debian package")
    lines.append("sudo apt install ./code_*_loong64.deb")
    lines.append("```")
    return "\n".join(lines)


def publish(
    manifest_path: Path,
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    token: Optional[str] = None,
    draft: bool = False,
    prerelease: bool = False,
    files: Optional[Sequence[Path]] = None,
) -> List[str]:
    """Upload the artifacts described by ``manifest_path``."""

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    vscode = manifest.get("vscode") or {}
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise BuildError("no target repository: pass --repo owner/name or set GITHUB_REPOSITORY")
    tag = tag or vscode.get("tag") or f"v{main_version(vscode)}-loong64"
    upload_files = list(files or [])
    if not upload_files:
        for artifact in manifest.get("artifacts") or []:
            candidate = Path(str(artifact.get("path")))
            if candidate.exists():
                upload_files.append(candidate)
    publisher = Publisher(
        repo=repo,
        tag=tag,
        token=token,
        title=f"VSCode {vscode.get('version')} for LoongArch64",
        body=release_notes(manifest),
        draft=draft,
        prerelease=prerelease,
    )
    publisher.ensure_release()
    return publisher.upload(upload_files)


def main_release(version: str) -> str:
    return version


def main_version(vscode: Dict[str, object]) -> str:
    return str(vscode.get("version") or "0.0.0")
