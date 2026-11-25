"""Repackage the official Microsoft VSCode binaries for LoongArch64.

The official Linux builds embed an x64/arm64 Electron runtime.  Everything that is
architecture independent (the compiled ``out/`` JavaScript, extensions, product
metadata, resources) is reused verbatim, while the runtime pieces are replaced:

* the Electron runtime is swapped for a LoongArch build,
* native ``.node`` addons and helper executables are rebuilt for loong64,
* ``product.json`` is adjusted so the build does not advertise x64 updates.

The module never shells out to ``electron-builder`` for the repack itself, which
keeps the pipeline reproducible on any Linux host; ``electron-builder`` (the
``@loongdotjs`` fork) is used optionally for producing installers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from . import asar, electron, musltoolchain, utils
from .config import (
    TEMPLATE_DIR,
    BuildConfig,
    NATIVE_MODULES,
    OS_PROXY_RESOLVER_PACKAGE,
    OS_PROXY_RESOLVER_PLATFORM_KEY,
    NativeModuleSpec,
)
from .utils import BuildError, log, warn

#: ELF machine id for LoongArch
EM_LOONGARCH = 258
ELF_MAGIC = b"\x7fELF"

#: files of the official runtime directory replaced by the Electron runtime
RUNTIME_FILES = {
    "code",
    "electron",
    "chrome-sandbox",
    "chrome_crashpad_handler",
    "libEGL.so",
    "libGLESv2.so",
    "libffmpeg.so",
    "libvk_swiftshader.so",
    "libvulkan.so.1",
    "vk_swiftshader_icd.json",
    "icudtl.dat",
    "resources.pak",
    "chrome_100_percent.pak",
    "chrome_200_percent.pak",
    "snapshot_blob.bin",
    "v8_context_snapshot.bin",
    "LICENSES.chromium.html",
    "version",
}

#: directories that come from the Electron runtime
RUNTIME_DIRS = {"locales", "resources"}


# --------------------------------------------------------------------------------------
# ELF helpers
# --------------------------------------------------------------------------------------

def elf_machine(path: Path) -> Optional[int]:
    """Return the ELF ``e_machine`` of ``path`` or ``None`` when it is not ELF."""

    try:
        with open(path, "rb") as fh:
            header = fh.read(20)
    except OSError:
        return None
    if len(header) < 20 or header[:4] != ELF_MAGIC:
        return None
    endian = "<" if header[5] == 1 else ">"
    return struct.unpack(endian + "H", header[18:20])[0]


def is_loongarch_elf(path: Path) -> bool:
    return elf_machine(path) == EM_LOONGARCH


def arch_label(path: Path) -> str:
    return {
        3: "i386",
        62: "x86-64",
        40: "arm",
        183: "arm64",
        258: "loongarch64",
    }.get(elf_machine(path) or 0, f"e_machine={elf_machine(path)}")


# --------------------------------------------------------------------------------------
# upstream payload
# --------------------------------------------------------------------------------------

@dataclass
class UpstreamPayload:
    """The architecture independent part of an official VSCode build."""

    root: Path
    version: str
    commit: str
    product: Dict[str, object]
    package: Dict[str, object]

    @property
    def app_dir(self) -> Path:
        return self.root / "resources" / "app"

    @property
    def app_asar(self) -> Path:
        return self.app_dir / "node_modules.asar"

    @property
    def electron_version(self) -> Optional[str]:
        return electron.detect_required_version(self.product, self.package)

    def describe(self) -> str:
        return f"VSCode {self.version} ({self.commit[:8]})"


def load_payload(root: Path) -> UpstreamPayload:
    root = Path(root).resolve()
    if not (root / "resources" / "app").is_dir():
        candidates = sorted(root.glob("*/resources/app"))
        if not candidates:
            raise BuildError(f"{root} does not look like an extracted VSCode distribution")
        root = candidates[0].parent.parent
    app_dir = root / "resources" / "app"
    product_path = app_dir / "product.json"
    package_path = app_dir / "package.json"
    if not product_path.exists() or not package_path.exists():
        raise BuildError(f"{app_dir} is missing product.json/package.json")
    product = json.loads(product_path.read_text(encoding="utf-8"))
    package = json.loads(package_path.read_text(encoding="utf-8"))
    version = str(product.get("version") or package.get("version") or "0.0.0")
    commit = str(product.get("commit") or "")
    return UpstreamPayload(root=root, version=version, commit=commit, product=product, package=package)


# --------------------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------------------

@dataclass
class Stage:
    """A staging directory holding a loong64-ready VSCode tree."""

    root: Path
    config: BuildConfig
    payload: Optional[UpstreamPayload] = None
    electron_runtime: Optional[electron.ElectronRuntime] = None
    native_report: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def app_dir(self) -> Path:
        return self.root / "resources" / "app"

    @property
    def executable(self) -> Path:
        return self.root / self.config.binary_name

    def warn(self, message: str, *args: object) -> None:
        text = message % args if args else message
        self.warnings.append(text)
        warn(text)


def stage_payload(payload: UpstreamPayload, stage_dir: Path, config: BuildConfig) -> Stage:
    """Copy the architecture independent payload into ``stage_dir``."""

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage = Stage(root=stage_dir, config=config, payload=payload)
    stage_dir.mkdir(parents=True, exist_ok=True)
    for entry in payload.root.iterdir():
        name = entry.name
        if name == "resources":
            target_resources = stage_dir / "resources"
            target_resources.mkdir(exist_ok=True)
            for child in entry.iterdir():
                if child.name == "app":
                    utils.copy_tree(child, target_resources / "app")
                elif child.name in ("completions",):
                    utils.copy_tree(child, target_resources / child.name)
            continue
        if name in RUNTIME_FILES or name in RUNTIME_DIRS:
            continue
        if entry.is_dir():
            utils.copy_tree(entry, stage_dir / name)
        else:
            shutil.copy2(entry, stage_dir / name)
    return stage


def apply_runtime(stage: Stage) -> None:
    """Overlay the LoongArch Electron runtime onto the stage."""

    runtime = stage.electron_runtime
    if runtime is None:
        raise BuildError("no Electron runtime configured")
    root = runtime.root
    binary_name = stage.config.binary_name
    log("applying %s from %s", runtime.describe(), root)

    # The upstream payload is a different Electron build.  Its V8 compile
    # cache is keyed by the Node version and architecture, so it cannot be
    # used by the replacement runtime and only wastes tens of MB in the
    # package.
    compile_cache = stage.app_dir / "node-compile-cache"
    if compile_cache.is_dir():
        shutil.rmtree(compile_cache)

    for entry in root.iterdir():
        name = entry.name
        if name == "electron":
            target = stage.root / binary_name
            if target.exists() or target.is_symlink():
                target.unlink()
            shutil.copy2(entry, target)
            target.chmod(0o755)
            continue
        if name in RUNTIME_DIRS:
            dest = stage.root / name
            dest.mkdir(exist_ok=True)
            for child in entry.iterdir():
                if name == "resources" and child.name == "app":
                    continue  # keep the VSCode payload
                target = dest / child.name
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target, symlinks=True)
                else:
                    shutil.copy2(child, target, follow_symlinks=True)
            continue
        target = stage.root / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=True)
            if entry.stat().st_mode & stat.S_IXUSR:
                target.chmod(0o755)

    # Electron ships LICENSE and a `version` marker used by @electron/get
    electron_license = stage.root / "LICENSE"
    if electron_license.exists() and not (stage.root / "LICENSE.electron.txt").exists():
        electron_license.replace(stage.root / "LICENSE.electron.txt")
    version_marker = stage.root / "version"
    if version_marker.exists():
        version_marker.unlink()
    default_app = stage.root / "resources" / "default_app.asar"
    if default_app.exists():
        default_app.unlink()

    _rewrite_launchers(stage)
    _patch_package_json(stage)


def _rewrite_launchers(stage: Stage) -> None:
    bin_dir = stage.root / "bin"
    if not bin_dir.is_dir():
        return
    binary = stage.config.binary_name
    display = stage.config.app_name
    for script in list(bin_dir.iterdir()):
        if not script.is_file() or script.suffix in (".node", ".so"):
            continue
        if not script.name.startswith("code"):
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rewritten = re.sub(r"\bElectron\b", display, text)
        rewritten = rewritten.replace("/usr/share/code/code", f"/usr/share/{binary}/{binary}")
        rewritten = rewritten.replace('"$CODE_DIR"/code', f'"$CODE_DIR"/{binary}')
        rewritten = rewritten.replace('$(dirname "$0")/../code', f'$(dirname "$0")/../{binary}')
        if rewritten != text:
            script.write_text(rewritten, encoding="utf-8")
        script.chmod(0o755)


def _patch_package_json(stage: Stage) -> None:
    package_path = stage.app_dir / "package.json"
    if not package_path.exists():
        return
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except ValueError:
        return
    native = package.get("nativeBuild")
    if isinstance(native, dict) and stage.electron_runtime:
        native["target"] = stage.electron_runtime.version
    scripts = package.get("scripts")
    if isinstance(scripts, dict):
        for key in ("postinstall", "preinstall"):
            scripts.pop(key, None)
    package_path.write_text(json.dumps(package, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def patch_product(stage: Stage) -> None:
    """Adjust ``product.json`` so the build does not advertise x64 updates."""

    product_path = stage.app_dir / "product.json"
    if not product_path.exists():
        return
    product = json.loads(product_path.read_text(encoding="utf-8"))
    for key in ("updateUrl", "win32VersionedUpdate"):
        product.pop(key, None)
    if stage.config.quality != "stable":
        product["quality"] = stage.config.quality
    product["loong64Build"] = True
    product_path.write_text(json.dumps(product, indent="\t", ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------
# native modules
# --------------------------------------------------------------------------------------

@dataclass
class NativeResult:
    name: str
    status: str  # built | kept | missing | failed | shim
    binding: Optional[Path] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "binding": str(self.binding) if self.binding else None,
            "detail": self.detail,
        }


def install_natives(
    stage: Stage,
    artifacts: Dict[str, Path],
    specs: Sequence[NativeModuleSpec] = tuple(NATIVE_MODULES),
    strict: bool = False,
) -> List[NativeResult]:
    """Copy freshly built native modules into the staged application."""

    results: List[NativeResult] = []
    unpacked = stage.app_dir / "node_modules.asar.unpacked"
    for spec in specs:
        if spec.shim:
            results.append(_install_vsda_shim(stage))
            continue
        artifact_root = artifacts.get(spec.name)
        artifact_binding = Path(artifact_root) / spec.binding_path if artifact_root is not None else stage.app_dir / "node_modules.asar.unpacked" / spec.name / spec.binding_path
        if artifact_root is None:
            # A batch produced by NativeBuilder.artifacts() always points at the
            # module source directory; derive the binding in the staging tree
            # when the caller supplies only a pre-built artifact map.
            if artifact_binding.exists() and is_loongarch_elf(artifact_binding):
                results.append(NativeResult(name=spec.name, status="built", binding=artifact_binding, detail="prebuilt loongarch64"))
                continue
            if spec.required and strict:
                raise BuildError(f"required native module {spec.name} has no loong64 build")
            results.append(NativeResult(name=spec.name, status="missing", detail="no loong64 build available"))
            continue
        if not artifact_binding.exists():
            message = f"missing {spec.binding_path}"
            if spec.required and strict:
                raise BuildError(f"required native module {spec.name}: {message}")
            results.append(NativeResult(name=spec.name, status="failed", detail=message))
            continue
        if not is_loongarch_elf(artifact_binding):
            message = f"{spec.binding_path} is {arch_label(artifact_binding)}"
            if spec.required and strict:
                raise BuildError(f"required native module {spec.name}: {message}")
            results.append(NativeResult(name=spec.name, status="failed", detail=message))
            continue
        target = unpacked / spec.name / spec.binding_path
        if spec.name == OS_PROXY_RESOLVER_PACKAGE:
            native_dir = (
                stage.app_dir
                / "node_modules"
                / "@vscode"
                / OS_PROXY_RESOLVER_PACKAGE.split("/", 1)[1]
            )
            target = native_dir / "os_proxy_resolver.node"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact_binding, target)
        target.chmod(0o755)
        if spec.name == OS_PROXY_RESOLVER_PACKAGE:
            module_dir = stage.app_dir / "node_modules" / "@vscode" / "os-proxy-resolver"
            module_dir.mkdir(parents=True, exist_ok=True)
            archive = asar.AsarArchive.open(stage.app_dir / "node_modules.asar")
            archive.extract(stage.app_dir / "node_modules", "@vscode/os-proxy-resolver")
            facade = module_dir / "index.js"
            if facade.exists():
                text = facade.read_text(encoding="utf-8")
                # The platform helper computes "<platform>-<arch>-<libc>" and
                # already resolves this build to linux-loong64-glibc; only the
                # package table has to learn about the new key.
                if f"'{OS_PROXY_RESOLVER_PLATFORM_KEY}'" not in text:
                    marker = "const packages = {\n"
                    if marker not in text:
                        raise BuildError(
                            "unexpected @vscode/os-proxy-resolver facade; cannot register "
                            f"{OS_PROXY_RESOLVER_PLATFORM_KEY}"
                        )
                    text = text.replace(
                        marker,
                        marker
                        + f"\t'{OS_PROXY_RESOLVER_PLATFORM_KEY}': '{OS_PROXY_RESOLVER_PACKAGE}',\n",
                        1,
                    )
                facade.write_text(text, encoding="utf-8")
            old_binding = unpacked / "@vscode" / "os-proxy-resolver-linux-x64-gnu" / "os_proxy_resolver.node"
            for candidate in (
                old_binding,
                old_binding.with_suffix(".node.x86_64.disabled"),
                old_binding.with_suffix(".node.foreign.disabled"),
            ):
                if candidate.exists():
                    candidate.unlink()
            target.chmod(0o755)
            (native_dir / "package.json").write_text(
                json.dumps(
                    {
                        "name": OS_PROXY_RESOLVER_PACKAGE,
                        "version": _proxy_version(stage),
                        "description": "Native binding for @vscode/os-proxy-resolver on LoongArch64 (glibc)",
                        "main": "os_proxy_resolver.node",
                        "os": ["linux"],
                        "cpu": ["loong64"],
                        "libc": ["glibc"],
                        "license": "MIT",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        results.append(NativeResult(name=spec.name, status="built", binding=target, detail="loongarch64"))
    stage.native_report["modules"] = [r.as_dict() for r in results]
    return results


def _install_vsda_shim(stage: Stage) -> NativeResult:
    """Replace the closed-source ``vsda`` native module with a JS implementation."""

    module_dir = stage.app_dir / "node_modules" / "vsda"
    module_dir.mkdir(parents=True, exist_ok=True)
    shim_src = TEMPLATE_DIR / "vsda_shim.js"
    if not shim_src.exists():
        stage.warn("vsda shim template missing; keeping the upstream x64 binding")
        return NativeResult(name="vsda", status="missing", detail="shim template not found")
    shutil.copy2(shim_src, module_dir / "index.js")
    types_src = TEMPLATE_DIR / "vsda_shim.d.ts"
    if types_src.exists():
        shutil.copy2(types_src, module_dir / "index.d.ts")
    (module_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "vsda",
                "description": "VS Code debug handshake module (loong64 shim)",
                "version": _vsda_version(stage),
                "main": "index.js",
                "types": "index.d.ts",
                "os": ["linux"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # drop the unusable native binary and the encrypted wasm
    for stale in ("build", "rust/web/vsda_bg.wasm", "rust/bundler"):
        candidate = module_dir / stale
        if candidate.is_dir():
            shutil.rmtree(candidate)
        elif candidate.exists():
            candidate.unlink()
    for unpacked in (stage.app_dir / "node_modules.asar.unpacked" / "vsda",):
        if unpacked.is_dir():
            shutil.rmtree(unpacked)
    stage.native_report["vsda"] = "shim"
    return NativeResult(name="vsda", status="shim", binding=module_dir / "index.js", detail="javascript shim")


def _proxy_version(stage: Stage) -> str:
    asar_path = stage.app_dir / "node_modules.asar"
    if asar_path.exists():
        try:
            data = asar.AsarArchive.open(asar_path).read_json(
                "@vscode/os-proxy-resolver/package.json"
            )
            if isinstance(data, dict) and data.get("version"):
                return str(data["version"])
        except (BuildError, ValueError):
            pass
    return "0.4.0"


def _vsda_version(stage: Stage) -> str:
    asar_path = stage.app_dir / "node_modules.asar"
    if not asar_path.exists():
        return "1.4.4"
    try:
        archive = asar.AsarArchive.open(asar_path)
        data = archive.read_json("vsda/package.json")
        if isinstance(data, dict) and data.get("version"):
            return str(data["version"])
    except (BuildError, ValueError):
        pass
    return "1.4.4"


def disable_unloadable_natives(stage: Stage) -> List[str]:
    """Neutralise ``.node`` binaries that are still x86-64 after the rebuild pass."""

    disabled: List[str] = []
    app_dir = stage.app_dir
    stale_bindings = (
        "node_modules.asar.unpacked/@vscode/os-proxy-resolver-linux-x64-gnu/os_proxy_resolver.node",
        "node_modules.asar.unpacked/@vscode/os-proxy-resolver-linux-x64-gnu/os_proxy_resolver.node.x86_64.disabled",
        "node_modules.asar.unpacked/@vscode/os-proxy-resolver-linux-x64-gnu/os_proxy_resolver.node.foreign.disabled",
    )
    for relative in stale_bindings:
        candidate = app_dir / relative
        if candidate.exists():
            candidate.unlink()
    candidates: List[Path] = []
    for base in (app_dir / "node_modules.asar.unpacked", app_dir / "node_modules"):
        if base.is_dir():
            candidates.extend(base.rglob("*.node"))
    for path in candidates:
        machine = elf_machine(path)
        if machine is None or machine == EM_LOONGARCH:
            continue
        disabled.append(str(path.relative_to(app_dir)))
        target = path.with_suffix(path.suffix + ".x86_64.disabled")
        if target.exists():
            target.unlink()
        path.replace(target)
    if disabled:
        stage.native_report["disabled"] = disabled
        warn("%d native module(s) are still x86-64 and were disabled", len(disabled))
    return disabled


def disable_foreign_helpers(stage: Stage, replacements: Sequence[Path] = ()) -> List[str]:
    """Disable prebuilt ELF helpers that remain foreign to the target."""

    disabled: List[str] = []
    app_dir = stage.app_dir
    replaced = {path.resolve() for path in replacements}
    for base in (app_dir / "node_modules.asar.unpacked", app_dir / "extensions"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.resolve() in replaced:
                continue
            if path.name in ("rg", "tgrep") or path.suffix in (".node", ".so"):
                machine = elf_machine(path)
                if machine is None or machine == EM_LOONGARCH:
                    continue
                disabled.append(str(path.relative_to(app_dir)))
                target = path.with_suffix(path.suffix + ".foreign.disabled")
                if target.exists():
                    target.unlink()
                path.replace(target)
    if disabled:
        stage.native_report.setdefault("disabled", []).extend(disabled)
        warn("%d foreign helper(s) were disabled", len(disabled))
    return disabled


def disable_unsupported_copilot(stage: Stage) -> List[str]:
    """Hide Copilot SDK binaries for architectures it has no LoongArch build for.

    The extension is not usable with an x86-64 runtime.node.  Hide the
    platform-specific package so VS Code falls back to the TS implementation.
    """

    disabled: List[str] = []
    app_dir = stage.app_dir
    for candidate in app_dir.rglob("runtime.node.x86_64.disabled"):
        candidate.unlink()
    for candidate in app_dir.rglob("runtime.node.foreign.disabled"):
        candidate.unlink()
    for candidate in app_dir.rglob("*copilot-sdk*.unsupported"):
        if candidate.is_dir():
            shutil.rmtree(candidate)
    for candidate in app_dir.rglob("runtime.node.x86_64.disabled"):
        candidate.unlink()
    for candidate in app_dir.rglob("runtime.node.foreign.disabled"):
        candidate.unlink()
    for base in (app_dir / "node_modules.asar.unpacked", app_dir / "node_modules", app_dir / "extensions"):
        if not base.is_dir():
            continue
        for package in list(base.rglob("@microsoft/copilot-sdk-*")) + list(base.rglob("@github/copilot-sdk-*")):
            if not package.is_dir():
                continue
            shutil.rmtree(package)
            disabled.append(str(package.relative_to(app_dir)))
    if disabled:
        stage.native_report.setdefault("unsupported", []).extend(disabled)
        warn("disabled Copilot SDK runtime for unsupported architecture")
    return disabled


def replace_ripgrep(stage: Stage, binary: Optional[Path]) -> List[Path]:
    """Swap every ripgrep helper for a static LoongArch build.

    ``@vscode/ripgrep-universal`` resolves its binary from
    ``bin/<platform>-<arch>/rg`` using ``process.arch``; the copilot extension and
    the copilot SDK bundle their own copies.  All of them are replaced, and a
    ``linux-loong64`` directory is created so the lookup works natively.
    """

    app_dir = stage.app_dir
    targets: List[Path] = []
    for base in (app_dir / "node_modules.asar.unpacked", app_dir / "node_modules", app_dir / "extensions"):
        if not base.is_dir():
            continue
        targets.extend(
            path / "rg"
            for path in base.rglob("*")
            if path.is_dir() and path.name == "linux-x64" and (path / "rg").is_file()
        )
    # de-duplicate while keeping order
    unique: List[Path] = []
    seen = set()
    for target in targets:
        key = str(target)
        if key in seen or not target.exists():
            continue
        seen.add(key)
        unique.append(target)
    if not unique:
        raise BuildError("upstream payload contains no ripgrep binary to replace")
    if binary is None:
        raise BuildError("a static LoongArch ripgrep binary is required")
    if not binary.exists():
        raise BuildError(f"ripgrep binary not found: {binary}")
    if not is_loongarch_elf(binary):
        raise BuildError(f"ripgrep is not LoongArch: {arch_label(binary)}")
    problems = musltoolchain.static_problems(binary)
    if problems:
        raise BuildError("ripgrep is not static: " + "; ".join(problems))
    managed = [
        path
        for base in (
            app_dir / "node_modules.asar.unpacked" / "@vscode" / "ripgrep-universal",
            app_dir / "extensions" / "copilot" / "node_modules" / "@github" / "copilot",
        )
        if base.is_dir()
        for path in (base / "bin" / "linux-loong64" / "rg",)
    ]
    primary = app_dir / "node_modules.asar.unpacked" / "@vscode" / "ripgrep-universal" / "bin" / "linux-loong64" / "rg"
    primary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, primary)
    primary.chmod(0o755)

    for dest in managed:
        if dest == primary:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy(primary, dest)
    for target in unique:
        if target != primary:
            _link_or_copy(primary, target)
        target_x64 = target.parent.parent / "linux-x64" / target.name
        if target_x64.exists() and target_x64 != target:
            _link_or_copy(primary, target_x64)
        loong64_dir = target.parent.parent / "linux-loong64"
        loong64_dir.mkdir(parents=True, exist_ok=True)
        alias = loong64_dir / target.name
        if alias != target and alias != primary:
            _link_or_copy(primary, alias)
    stage.native_report["ripgrep"] = {
        "status": "static",
        "binary": str(binary),
        "replaced": len(unique) + len(managed),
        "storage": "hardlinked",
    }
    log("replaced %d ripgrep binary/binaries", len(unique))
    return unique


def _link_or_copy(source: Path, dest: Path) -> None:
    """Replace ``dest`` with a hardlink to ``source``, falling back to a copy."""

    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.link(source, dest)
    except OSError:
        shutil.copy2(source, dest)
    dest.chmod(0o755)


# --------------------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------------------

@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def verify_layout(stage: Stage) -> List[Check]:
    """Structural checks that do not require a display server."""

    checks: List[Check] = []
    binary = stage.executable
    checks.append(Check("executable exists", binary.exists(), str(binary)))
    if binary.exists():
        checks.append(Check("executable is loongarch64", is_loongarch_elf(binary), arch_label(binary)))
    app_dir = stage.app_dir
    for required in ("package.json", "product.json", "out/main.js", "node_modules.asar", "extensions"):
        checks.append(Check(f"app/{required}", (app_dir / required).exists()))
    checks.append(
        Check(
            "electron default_app removed",
            not (stage.root / "resources" / "default_app.asar").exists(),
        )
    )
    for literal in ("chrome-sandbox", "chrome_crashpad_handler", "icudtl.dat", "resources.pak", "locales/en-US.pak"):
        checks.append(Check(f"runtime/{literal}", (stage.root / literal).exists()))
    for runtime in ("chrome-sandbox", "chrome_crashpad_handler"):
        path = stage.root / runtime
        checks.append(
            Check(
                f"runtime/{runtime} is loongarch64",
                path.exists() and is_loongarch_elf(path),
                arch_label(path) if path.exists() else "missing",
            )
        )
    launcher = stage.root / "bin" / stage.config.binary_name
    checks.append(Check("bin launcher", launcher.exists(), str(launcher)))
    shim = app_dir / "node_modules" / "vsda" / "index.js"
    checks.append(Check("vsda shim", shim.exists(), str(shim)))
    proxy = (
        app_dir
        / "node_modules"
        / "@vscode"
        / OS_PROXY_RESOLVER_PACKAGE.split("/", 1)[1]
        / "os_proxy_resolver.node"
    )
    checks.append(Check("proxy resolver binding", proxy.exists() and is_loongarch_elf(proxy), str(proxy)))
    if proxy.exists() and is_loongarch_elf(proxy):
        problems = musltoolchain.glibc_addon_problems(proxy)
        checks.append(
            Check(
                "proxy resolver links glibc dynamically",
                not problems,
                ", ".join(problems) if problems else "libc.so dynamic",
            )
        )
    rg_binaries = [
        path
        for base in (app_dir / "node_modules.asar.unpacked", app_dir / "extensions")
        if base.is_dir()
        for path in base.rglob("rg")
        if path.is_file()
    ]
    bad_rg = [path for path in rg_binaries if musltoolchain.static_problems(path)]
    checks.append(Check("ripgrep binaries are static", bool(rg_binaries) and not bad_rg, str(len(rg_binaries)) + " binaries"))
    foreign = []
    for base in (app_dir / "node_modules.asar.unpacked", app_dir / "extensions"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name in ("rg", "tgrep") or path.suffix in (".node", ".so"):
                machine = elf_machine(path)
                if machine is not None and machine != EM_LOONGARCH:
                    foreign.append(str(path.relative_to(app_dir)))
    checks.append(Check("no foreign native payloads", not foreign, ", ".join(foreign[:5])))
    return checks
