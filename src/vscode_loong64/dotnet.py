"""Build the .NET NativeAOT helpers (currently ``vsce-sign``) for LoongArch.

Upstream ``@vscode/vsce-sign`` ships a NativeAOT executable built for
win32/darwin/linux-x64/linux-arm64/linux-armhf.  On LoongArch the binary aborts
with ``Failed to create CoreCLR`` before it can verify anything, which makes
``code --install-extension`` fail for every signed marketplace extension.

The verifier itself is a small, self-contained program (PKCS#7 signature over a
JSON manifest plus a set of hashes), so it is reimplemented in C# under
``src-tools/vsce-sign-cs`` and published with NativeAOT for
``linux-loongarch64``.

Finding an SDK
--------------
NativeAOT needs a .NET SDK whose RID pack set includes
``linux-loongarch64`` and the ``ILCompiler`` runtime pack.  This module looks,
in order, at:

1. ``VSCODE_LOONG64_DOTNET_ROOT`` / ``DOTNET_ROOT`` / ``DOTNET_ROOT_*``,
2. ``<cache>/dotnet`` (``dotnet sdk install``-style layout),
3. ``dotnet`` on ``PATH``,
4. a download of the Loongson LoongArch SDK when networking is allowed.

The download is only attempted when the host really is LoongArch and the
``dotnet-install`` source is reachable; otherwise the helper is reported as
unavailable and the packager degrades (see :func:`vscode_loong64.pack.verify_layout`).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import utils
from .helpers import HelperBuild
from .utils import BuildError, human_size, log, warn

#: LoongArch .NET SDK published by Loongson (the only source of NativeAOT packs
#: for this architecture today).
DEFAULT_SDK_URL = (
    "https://github.com/loongson/dotnet/releases/download/"
    "v11.0.100-loongarch64/dotnet-sdk-11.0.100-linux-loongarch64.tar.gz"
)

RID = "linux-loongarch64"

#: projects that can be published, keyed by helper name
PROJECTS: Dict[str, str] = {
    "vsce-sign": "vsce-sign-cs/src/VsceSign/VsceSign.csproj",
}


# --------------------------------------------------------------------------------------
# SDK discovery
# --------------------------------------------------------------------------------------

def _sdk_info(root: Path) -> Optional[Dict[str, object]]:
    """Return version info when ``root`` looks like a usable LoongArch SDK."""

    dotnet = root / "dotnet"
    if not dotnet.exists():
        return None
    packs = root / "packs"
    if not packs.is_dir():
        return None
    has_aot = any(
        (packs / name).is_dir()
        for name in (
            "runtime.linux-loongarch64.Microsoft.DotNet.ILCompiler",
            "Microsoft.NETCore.App.Runtime.NativeAOT.linux-loongarch64",
        )
    )
    versions = sorted(p.name for p in (root / "sdk").glob("*") if p.is_dir())
    return {
        "root": str(root),
        "dotnet": str(dotnet),
        "sdkVersions": versions,
        "nativeAot": has_aot,
    }


def _candidate_roots(config) -> List[Path]:
    roots: List[Path] = []
    for key in ("VSCODE_LOONG64_DOTNET_ROOT", "DOTNET_ROOT", "DOTNET_ROOT_LOONGARCH64"):
        value = os.environ.get(key)
        if value:
            roots.append(Path(value).expanduser().resolve())
    roots.append(config.paths.cache / "dotnet")
    for base in ("/usr/share/dotnet", "/usr/lib/dotnet", "/opt/dotnet"):
        roots.append(Path(base))
    return roots


def find_sdk(config) -> Optional[Tuple[Path, Dict[str, object]]]:
    for root in _candidate_roots(config):
        info = _sdk_info(root)
        if info and info.get("nativeAot"):
            return root, info
        if info:
            utils.debug("SDK at %s has no NativeAOT pack for %s", root, RID)
    on_path = shutil.which("dotnet")
    if on_path:
        root = Path(on_path).resolve().parent
        info = _sdk_info(root)
        if info and info.get("nativeAot"):
            return root, info
    return None


def download_sdk(config, url: str = DEFAULT_SDK_URL) -> Optional[Tuple[Path, Dict[str, object]]]:
    """Fetch and unpack the Loongson LoongArch SDK into the cache."""

    if config.offline:
        warn("no LoongArch .NET SDK available and --offline was requested")
        return None
    dest = config.paths.cache / "dotnet"
    if (dest / "dotnet").exists():
        return find_sdk(config)
    archive = config.paths.downloads / Path(url).name
    log("no LoongArch .NET SDK found; downloading %s", url)
    try:
        utils.download(url, archive, retries=2)
    except BuildError as exc:
        warn("unable to download the LoongArch .NET SDK: %s", exc)
        return None
    dest.mkdir(parents=True, exist_ok=True)
    utils.extract(archive, dest)
    (dest / "dotnet").chmod(0o755)
    return find_sdk(config)


def sdk(config, allow_download: bool = True) -> Optional[Tuple[Path, Dict[str, object]]]:
    found = find_sdk(config)
    if found:
        return found
    if allow_download:
        return download_sdk(config)
    return None


# --------------------------------------------------------------------------------------
# publishing
# --------------------------------------------------------------------------------------

def _project_path(config, name: str) -> Optional[Path]:
    relative = PROJECTS.get(name)
    if not relative:
        return None
    for base in (config.paths.root / "src-tools", config.paths.root / "tools"):
        candidate = base / relative
        if candidate.exists():
            return candidate
    return None


def publish_args(project: Path, output: Path) -> List[str]:
    return [
        "publish",
        str(project),
        "-c",
        "Release",
        "-r",
        RID,
        "-o",
        str(output),
        "-p:PublishAot=true",
        "-p:StripSymbols=true",
        "-p:InvariantGlobalization=true",
        "-p:DebugType=none",
        "-p:GenerateDocumentationFile=false",
        "--nologo",
    ]


def build(
    config,
    workdir: Path,
    version: str,
    name: str = "vsce-sign",
    allow_download: bool = True,
) -> HelperBuild:
    """Publish ``name`` with NativeAOT and return the produced binary."""

    from .helpers import find_prebuilt, recipe_identity

    identity = recipe_identity(config, None, "vsce-sign")
    found_prebuilt = find_prebuilt(config, name, version, name, expected=identity)
    if found_prebuilt is not None:
        return found_prebuilt
    project = _project_path(config, name)
    if project is None:
        return HelperBuild(
            name=name,
            ok=False,
            detail="src-tools/vsce-sign-cs is not present in this checkout",
        )
    found = sdk(config, allow_download=allow_download)
    if found is None:
        return HelperBuild(
            name=name,
            ok=False,
            detail="no .NET SDK with a linux-loongarch64 NativeAOT pack "
            "(set VSCODE_LOONG64_DOTNET_ROOT or run without --offline)",
        )
    root, info = found
    output = workdir / "dotnet" / name
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    # Publish from a private copy: NativeAOT writes into ``obj/`` and a second
    # concurrent build (or a previously interrupted one) would otherwise leave
    # a half-written ``native/*.o`` behind and fail with a confusing ILCompiler
    # error.  The copy also keeps ``src-tools`` free of build output.
    build_dir = workdir / "dotnet-src" / project.parent.name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    shutil.copytree(
        project.parent,
        build_dir,
        ignore=shutil.ignore_patterns("bin", "obj", "out"),
    )
    project = build_dir / project.name
    env = {
        "DOTNET_ROOT": str(root),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "PATH": os.pathsep.join([str(root), os.environ.get("PATH", "")]),
    }
    log("publishing %s with NativeAOT (%s, SDK %s)", name, RID, ", ".join(info.get("sdkVersions") or ["?"]))
    proc = utils.run(
        [str(root / "dotnet"), *publish_args(project, output)],
        cwd=project.parent,
        env=env,
        check=False,
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    log_path = config.paths.logs / f"helper-{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text, encoding="utf-8", errors="replace")
    binary = output / name
    if proc.returncode != 0 or not binary.exists():
        tail = text.strip().splitlines()[-4:]
        return HelperBuild(name=name, ok=False, detail="; ".join(tail)[:500] or "dotnet publish failed", log_path=log_path)
    from .pack import arch_label, is_loongarch_elf

    if not is_loongarch_elf(binary):
        return HelperBuild(
            name=name,
            ok=False,
            detail=f"published {arch_label(binary)} instead of loongarch64",
            log_path=log_path,
        )
    binary.chmod(0o755)
    log("built %s (%s, %s)", name, arch_label(binary), human_size(binary.stat().st_size))
    from .helpers import recipe_identity, store_prebuilt

    identity = recipe_identity(config, None, "vsce-sign")
    try:
        store_prebuilt(
            config,
            name,
            version,
            binary,
            {**identity, "rid": RID, "runtime": "NativeAOT", "source": "built"},
        )
    except OSError as exc:  # pragma: no cover
        utils.debug("unable to cache %s: %s", name, exc)
    return HelperBuild(
        name=name,
        ok=True,
        binary=binary,
        source=str(project),
        detail=f"{RID} NativeAOT",
        log_path=log_path,
    )


def describe(config) -> Dict[str, object]:
    found = find_sdk(config)
    if not found:
        return {"available": False}
    root, info = found
    return {"available": bool(info.get("nativeAot")), **info}
