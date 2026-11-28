"""Portable prebuilt bundles for runners without a LoongArch CPU.

GitHub only offers x86-64 hosted runners, so the parts of the build that need a
LoongArch toolchain (``node-gyp`` addons, the Rust helpers, the C seccomp
helper, the .NET NativeAOT verifier) cannot run there.  Everything *after* those
steps - staging, patching ``product.json`` and the workbench bundles, pruning
foreign payloads, verifying and writing the ``.deb`` - is architecture
independent.

A bundle captures exactly the architecture specific inputs of one build, keyed
by the upstream version and the Electron runtime:

``bundle.json``
    Metadata: schema version, upstream version/commit, Electron version,
    architecture and one entry per payload with size + SHA-256.
``natives/<module>/<relative-path>``
    The compiled ``.node`` addons (and the proxy resolver), addressed by the
    staging path they have to be installed to.
``helpers/<name>/<binary>``
    ``bin/code-tunnel``, ``bin/copilot-shim/copilot``, ``apply-seccomp`` and
    ``vsce-sign``.
``rg``
    The static musl ripgrep used for every bundled copy.

``pack`` produces such an archive on a LoongArch machine (or in a container with
the LoongArch plugins); ``unpack`` restores it on the packaging host, and the
pipeline then installs the payloads verbatim while still refusing anything that
is not a LoongArch64 ELF.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import utils
from .config import STANDALONE_HELPERS
from .utils import BuildError, human_size, log, warn

#: bump when the bundle layout changes; unpack refuses other versions
BUNDLE_SCHEMA = 1
BUNDLE_JSON = "bundle.json"


@dataclass
class Bundle:
    """Contents of an unpacked bundle."""

    root: Path
    version: str
    electron: str
    commit: str = ""
    arch: str = "loong64"
    helpers: Dict[str, Path] = field(default_factory=dict)
    natives: Dict[str, Path] = field(default_factory=dict)
    ripgrep: Optional[Path] = None
    entries: List[Dict[str, object]] = field(default_factory=list)

    def native_artifacts(self) -> Dict[str, Path]:
        """Map module name -> directory that holds its binding.

        Bundle directories are keyed by the encoded package name
        (:func:`safe_name`); the pipeline expects the real npm name.
        """

        from .config import NATIVE_MODULES

        by_module = {spec.name: spec for spec in NATIVE_MODULES}
        out: Dict[str, Path] = {}
        for key, root in self.natives.items():
            for module in by_module:
                if safe_name(module) == key:
                    out[module] = root
                    break
            else:
                out[key] = root
        return out

    def describe(self) -> str:
        return (
            f"VSCode {self.version} / Electron {self.electron} ({len(self.helpers)} helpers, "
            f"{len(self.natives)} addons)"
        )


def bundle_name(version: str, electron: str, arch: str = "loong64") -> str:
    return f"loong64-prebuilts-{version}-electron{electron}-{arch}.tar.gz"


def safe_name(module: str) -> str:
    """Encode a package name so it is one path segment (``@scope/name`` too)."""

    return module.replace("@", "").replace("/", "__")


def _relative_payloads(
    helper_artifacts: Dict[str, Path],
    native_artifacts: Dict[str, Path],
) -> Dict[str, Path]:
    from .config import NATIVE_MODULES

    payloads: Dict[str, Path] = {}
    for name, binary in helper_artifacts.items():
        if binary is None:
            continue
        payloads[f"helpers/{name}/{Path(binary).name}"] = Path(binary)
    bindings = {spec.name: spec.binding_path for spec in NATIVE_MODULES}
    for name, root in native_artifacts.items():
        if name == "ripgrep" or root is None:
            continue
        relative = bindings.get(name)
        if relative is None:
            # proxy resolver artifacts already point at the single .node file
            candidate = Path(root)
            if candidate.is_file():
                payloads[f"natives/{safe_name(name)}/{candidate.name}"] = candidate
                continue
            found = sorted(Path(root).glob("*.node"))
            if found:
                payloads[f"natives/{safe_name(name)}/{found[0].name}"] = found[0]
            continue
        source = Path(root) / relative
        if source.exists():
            payloads[f"natives/{safe_name(name)}/{relative}"] = source
    rg = native_artifacts.get("ripgrep")
    if rg is not None and Path(rg).exists():
        payloads["rg"] = Path(rg)
    return payloads


def pack(
    config,
    version: str,
    electron: str,
    helper_artifacts: Dict[str, Path],
    native_artifacts: Dict[str, Path],
    commit: str = "",
    arch: str = "loong64",
) -> Path:
    """Write the portable bundle for one build and return its path."""

    payloads = _relative_payloads(helper_artifacts, native_artifacts)
    missing = [name for name in STANDALONE_HELPERS if f"helpers/{name}" not in " ".join(payloads)]
    if missing:
        warn("bundle is missing helpers: %s", ", ".join(missing))
    dest = config.paths.dist / bundle_name(version, electron, arch)
    dest.parent.mkdir(parents=True, exist_ok=True)
    entries: List[Dict[str, object]] = []
    for relative, source in sorted(payloads.items()):
        digest = utils.sha256_file(source)
        entries.append(
            {
                "path": relative,
                "size": source.stat().st_size,
                "sha256": digest,
                "source": str(source),
            }
        )
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vscode": version,
        "commit": commit,
        "electron": electron,
        "arch": arch,
        "cxxAddons": "loongarch64-gnu",
        "entries": entries,
    }
    staging = config.paths.work / "bundle"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for relative, source in payloads.items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(0o755)
    (staging / BUNDLE_JSON).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with tarfile.open(dest, "w:gz") as tf:
        for entry in sorted(staging.rglob("*")):
            if entry.is_file():
                tf.add(entry, arcname=str(entry.relative_to(staging)), recursive=False)
    log(
        "prebuilt bundle %s (%s, %d payloads)",
        dest.name,
        human_size(dest.stat().st_size),
        len(entries),
    )
    return dest


def _safe_extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise BuildError(f"refusing unsafe bundle member {member.name!r}")
            if not member.isfile():
                continue
            target = dest / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tf.extractfile(member)
            if source is None:
                continue
            with open(target, "wb") as fh:
                shutil.copyfileobj(source, fh, 1 << 20)


def unpack(archive: Path, dest: Path, verify: bool = True) -> Bundle:
    """Extract ``archive`` into ``dest`` and validate every payload."""

    archive = Path(archive).expanduser().resolve()
    if not archive.exists():
        raise BuildError(f"prebuilt bundle not found: {archive}")
    dest = Path(dest).expanduser().resolve()
    if dest.exists():
        shutil.rmtree(dest)
    _safe_extract(archive, dest)
    meta_path = dest / BUNDLE_JSON
    if not meta_path.exists():
        raise BuildError(f"{archive.name} has no {BUNDLE_JSON}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if int(meta.get("schema") or 0) != BUNDLE_SCHEMA:
        raise BuildError(
            f"{archive.name} uses bundle schema {meta.get('schema')}, expected {BUNDLE_SCHEMA}"
        )
    bundle = Bundle(
        root=dest,
        version=str(meta.get("vscode") or ""),
        electron=str(meta.get("electron") or ""),
        commit=str(meta.get("commit") or ""),
        arch=str(meta.get("arch") or "loong64"),
        entries=list(meta.get("entries") or []),
    )
    from .pack import arch_label, is_loongarch_elf

    problems: List[str] = []
    for entry in bundle.entries:
        relative = str(entry["path"])
        path = dest / relative
        if not path.exists():
            problems.append(f"{relative}: missing")
            continue
        if verify:
            if entry.get("size") and path.stat().st_size != entry["size"]:
                problems.append(f"{relative}: size mismatch")
                continue
            if entry.get("sha256") and utils.sha256_file(path) != entry["sha256"]:
                problems.append(f"{relative}: sha256 mismatch")
                continue
        if not is_loongarch_elf(path):
            problems.append(f"{relative}: {arch_label(path)}")
            continue
        path.chmod(0o755)
        if relative == "rg":
            bundle.ripgrep = path
        elif relative.startswith("helpers/"):
            parts = relative.split("/")
            if len(parts) >= 2:
                bundle.helpers[parts[1]] = path
        elif relative.startswith("natives/"):
            parts = relative.split("/")
            if len(parts) >= 2:
                module = parts[1]
                bundle.natives.setdefault(module, dest / "natives" / module)
    if problems:
        raise BuildError(
            f"prebuilt bundle {archive.name} is not usable: " + "; ".join(problems[:6])
        )
    log("using %s", bundle.describe())
    return bundle


def check_compatible(bundle: Bundle, version: str, electron: Optional[str]) -> None:
    """Refuse a bundle built for a different upstream/Electron pair."""

    if bundle.version and version and bundle.version != version:
        raise BuildError(
            f"prebuilt bundle targets VSCode {bundle.version} but the payload is {version}"
        )
    if electron and bundle.electron and bundle.electron != electron:
        warn(
            "prebuilt bundle targets Electron %s but the runtime is %s",
            bundle.electron,
            electron,
        )


def install_helpers(config, bundle: Bundle, version: str) -> None:
    """Seed the helper prebuilt cache from a bundle."""

    from .helpers import store_prebuilt

    for name, binary in bundle.helpers.items():
        store_prebuilt(
            config,
            name,
            version,
            binary,
            {"bundle": str(bundle.root), "source": "prebuilt-bundle"},
        )


#: tag convention for the release that carries the bundles
BUNDLE_TAG = "loong64-prebuilts"


def fetch(
    version: Optional[str] = None,
    electron: str = "auto",
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    dest: Optional[Path] = None,
) -> Path:
    """Download a bundle uploaded to the ``loong64-prebuilts`` release.

    ``version`` may be ``None`` (or ``"latest"``), in which case the newest asset
    is taken; ``electron`` narrows that further.  The bundle is built by hand on
    a LoongArch machine and uploaded as a release asset, so the caller has to
    check compatibility with :func:`check_compatible` when the version was
    inferred instead of requested.
    """

    from . import utils

    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise BuildError("no repository: pass --repo owner/name or set GITHUB_REPOSITORY")
    tag = tag or BUNDLE_TAG
    # an unauthenticated request is rate limited to a handful of calls per
    # minute, which a CI run shares with every other job on the runner
    headers: Dict[str, str] = {}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    assets: List[Dict[str, object]] = []
    release: Optional[Dict[str, object]] = None
    try:
        release = utils.http_get_json(
            f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
            headers=headers or None,
        )
        assets = list((release or {}).get("assets") or [])  # type: ignore[arg-type]
    except BuildError as exc:
        utils.debug("release %s not found (%s); scanning all releases", tag, exc)
    if not assets:
        # the bundle is uploaded by hand, so it may live in any release (a
        # version tag, a draft, or the dedicated bundle release)
        assets = _all_assets(repo, headers)
    if not assets:
        raise BuildError(
            f"{repo} has no release assets yet; upload "
            "dist/loong64-prebuilts-*.tar.gz to a release first"
        )
    available = ", ".join(str(a.get("name")) for a in assets[:10])
    if not version or version in ("", "latest"):
        matches = [
            asset
            for asset in assets
            if str(asset.get("name", "")).startswith("loong64-prebuilts-")
            and str(asset.get("name", "")).endswith("-loong64.tar.gz")
        ]
    else:
        prefix = f"loong64-prebuilts-{version}-electron"
        matches = [
            asset
            for asset in assets
            if str(asset.get("name", "")).startswith(prefix)
            and str(asset.get("name", "")).endswith("-loong64.tar.gz")
        ]
    if electron and electron != "auto":
        matches = [a for a in matches if f"electron{electron}-" in str(a.get("name"))]
    if not matches:
        raise BuildError(
            f"no bundle for VSCode {version or 'latest'}/Electron {electron} "
            f"in {repo}#{tag} (assets: {available})"
        )
    asset = sorted(matches, key=lambda a: str(a.get("name")))[-1]
    base = Path(dest or Path("dist"))
    if base.suffix == ".tar.gz":
        target = base
    else:
        target = base / str(asset["name"])
    target.parent.mkdir(parents=True, exist_ok=True)
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise BuildError(f"asset {asset.get('name')} has no download URL")
    utils.download(url, target)
    log("downloaded bundle %s", target)
    return target


def upload(
    archive: Path,
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    token: Optional[str] = None,
    notes: Optional[str] = None,
) -> List[str]:
    """Attach a hand-built bundle to the ``loong64-prebuilts`` release.

    Uses the same ``gh``-then-REST publishing path as the release workflow, so
    a machine with a token can upload the bundle without installing anything
    else.  The release is created on first use.
    """

    from .publish import Publisher

    archive = Path(archive).expanduser().resolve()
    if not archive.exists():
        raise BuildError(f"bundle not found: {archive}")
    meta = describe(archive)
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise BuildError("no repository: pass --repo owner/name or set GITHUB_REPOSITORY")
    tag = tag or BUNDLE_TAG
    body = notes or (
        "Architecture specific payloads consumed by the packaging workflow: "
        "native addons, ripgrep (static musl) and the four standalone helpers "
        "(`bin/code-tunnel`, `bin/copilot-shim/copilot`, `apply-seccomp`, `vsce-sign`). "
        "Built on a LoongArch machine with `make bundle`."
    )
    publisher = Publisher(
        repo=repo,
        tag=tag,
        token=token,
        title="LoongArch64 payloads",
        body=body,
    )
    publisher.ensure_release()
    names = publisher.upload([archive])
    log(
        "uploaded %s (%s) to %s#%s",
        archive.name,
        human_size(archive.stat().st_size),
        repo,
        tag,
    )
    utils.debug("bundle metadata: %s", meta)
    return names


def _all_assets(repo: str, headers: Dict[str, str]) -> List[Dict[str, object]]:
    """Collect the assets of every release, newest release first."""

    from . import utils

    try:
        releases = utils.http_get_json(
            f"https://api.github.com/repos/{repo}/releases?per_page=100",
            headers=headers or None,
        )
    except BuildError as exc:
        warn("unable to list releases of %s: %s", repo, exc)
        return []
    if not isinstance(releases, list):
        return []
    assets: List[Dict[str, object]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        for asset in release.get("assets") or []:
            if isinstance(asset, dict):
                assets.append(asset)
    return assets


def describe(archive: Path) -> Dict[str, object]:
    """Print-only helper used by ``vscode-loong64 prebuilts inspect``."""

    archive = Path(archive).expanduser().resolve()
    with tarfile.open(archive) as tf:
        handle = tf.extractfile(BUNDLE_JSON)
        if handle is None:
            raise BuildError(f"{archive.name} has no {BUNDLE_JSON}")
        return json.loads(handle.read().decode("utf-8"))
