"""Build configuration, on-disk layout and the native module catalogue."""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .utils import BuildError

ARCH = "loong64"
#: Debian/Ubuntu architecture string
DEB_ARCH = "loong64"
#: RPM / generic Linux string
RPM_ARCH = "loongarch64"

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class NativeModuleSpec:
    """A native module that has to exist for LoongArch."""

    name: str
    binding: str
    required: bool = True
    shim: bool = False
    kind: str = "gyp"  # gyp | prebuilt | shim

    @property
    def binding_path(self) -> str:
        return self.binding


#: source repository for the Rust/napi proxy resolver
OS_PROXY_RESOLVER_REPO = "https://github.com/microsoft/os-proxy-resolver.git"
OS_PROXY_RESOLVER_REF = "v0.4.0"
#: the Node-API crate lives in ``npm/native`` of the Microsoft repository
OS_PROXY_RESOLVER_NATIVE_DIR = "npm/native"
#: the LoongArch Electron runtime is a glibc binary, so the Node-API addon has
#: to share that libc.  Upstream ships the same split as ``-gnu``/``-musl``
#: packages; loong64 follows the glibc variant.
OS_PROXY_RESOLVER_TARGET = "loongarch64-unknown-linux-gnu"
OS_PROXY_RESOLVER_PACKAGE = "@vscode/os-proxy-resolver-linux-loong64-gnu"
#: key used by the facade's platform table (``<platform>-<arch>-<glibc|musl>``)
OS_PROXY_RESOLVER_PLATFORM_KEY = "linux-loong64-glibc"
#: PAC backend.  ``pac-engine-wasm2c`` (upstream default) requires WABT 1.0.41
#: exactly; ``pac-engine`` embeds the same QuickJS-NG engine natively and needs
#: no extra host tooling.
OS_PROXY_RESOLVER_FEATURES = "pac-engine"

NATIVE_MODULES: List[NativeModuleSpec] = [
    NativeModuleSpec("node-pty", "build/Release/pty.node"),
    NativeModuleSpec("@parcel/watcher", "build/Release/watcher.node"),
    NativeModuleSpec("@vscode/sqlite3", "build/Release/vscode-sqlite3.node"),
    NativeModuleSpec("native-keymap", "build/Release/keymapping.node", required=False),
    NativeModuleSpec("@vscode/spdlog", "build/Release/spdlog.node", required=False),
    NativeModuleSpec("@vscode/policy-watcher", "build/Release/vscode-policy-watcher.node", required=False),
    NativeModuleSpec("kerberos", "build/Release/kerberos.node", required=False),
    NativeModuleSpec("native-is-elevated", "build/Release/iselevated.node", required=False),
    NativeModuleSpec("@vscode/native-watchdog", "build/Release/watchdog.node", required=False),
    NativeModuleSpec("vsda", "build/Release/vsda.node", required=False, shim=True, kind="shim"),
    NativeModuleSpec(
        OS_PROXY_RESOLVER_PACKAGE,
        "os_proxy_resolver.node",
        required=True,
        kind="rust",
    ),
]

#: packages whose only architecture specific content is a prebuilt binary or a
#: helper executable; they are handled by the helper/native sections below or
#: removed in :data:`UNSUPPORTED_PREBUILTS`
PREBUILT_MODULES: List[str] = [
    "@vscode/ripgrep-universal",
    "@microsoft/copilot-sdk-linux-x64",
    "@vscode/deviceid",
    "@vscode/vsce-sign",
    "@vscode/sandbox-runtime",
]

#: upstream source of the VS Code CLI / copilot shim, used to rebuild the
#: architecture specific helpers that ship in ``bin/``.  A codeload tarball is
#: fetched instead of a git checkout: only a few directories are needed and no
#: credentials or full history are involved.
VSCODE_SOURCE_TARBALL = "https://codeload.github.com/microsoft/vscode/tar.gz"
#: directories of the repository the helper builds need
VSCODE_SOURCE_PATHS = ("cli", "copilot_shim", "package.json", "product.json")

#: standalone helpers that are not ``.node`` addons.  Each entry is rebuilt (or
#: taken from the prebuilt cache) and installed into the staged tree.
STANDALONE_HELPERS: Dict[str, Dict[str, str]] = {
    "code-tunnel": {
        "kind": "rust",
        "crate": "cli",
        "binary": "code",
        "install": "bin/code-tunnel",
        "description": "VS Code tunnel / serve-web / agent CLI (Rust)",
    },
    "copilot-shim": {
        "kind": "rust",
        "crate": "copilot_shim",
        "binary": "copilot",
        "install": "bin/copilot-shim/copilot",
        "description": "GitHub Copilot CLI bootstrapper (Rust)",
    },
    "apply-seccomp": {
        "kind": "seccomp",
        "binary": "apply-seccomp",
        "install": "resources/app/node_modules/@vscode/sandbox-runtime/vendor/seccomp/loong64/apply-seccomp",
        "description": "seccomp BPF unix-socket filter for @vscode/sandbox-runtime (C)",
    },
    "vsce-sign": {
        "kind": "dotnet",
        "crate": "vsce-sign-cs",
        "binary": "vsce-sign",
        "install": "resources/app/node_modules.asar.unpacked/@vscode/vsce-sign/bin/vsce-sign",
        "description": "extension signature verifier (.NET NativeAOT)",
    },
}

#: prebuilt binaries that upstream never published for loongarch64.  They are
#: removed from the staged tree on purpose; every other foreign binary in the
#: payload is an error, so this list is the single place that can hide one.
UNSUPPORTED_PREBUILTS: Dict[str, str] = {
    "**/@microsoft/copilot-sdk-*/**/*.node": "GitHub Copilot SDK ships runtime.node for x64/arm64 only; the extension falls back to its TypeScript implementation",
    "**/@github/copilot/**/prebuilds/**/*.node": "GitHub Copilot CLI ships cli-native.node/runtime.node for x64/arm64 only",
    "**/@github/copilot/**/tgrep/**": "GitHub Copilot CLI ships tgrep for x64/arm64 only; ripgrep is substituted",
    "**/microsoft-authentication/**/msal-node-runtime.node": "msal-node-runtime has prebuilds for win32/darwin/linux-x64 only; the extension falls back to the JS broker",
    "**/microsoft-authentication/**/libmsalruntime.so": "same as msal-node-runtime.node",
    "**/@vscode/deviceid/**/windows.node": "deviceid only ships a Windows binding; Linux uses dist/devdeviceid.js",
    "**/ms-vscode.js-debug/**/*.win32-*.node": "Windows-only debugging helper shipped for win32",
    "**/foundry-local-sdk/**/prebuilds/**": "foundry-local-sdk has no loongarch64 prebuild; the Copilot SDK feature is unavailable",
    "**/@vscode/sandbox-runtime/vendor/seccomp/x64/**": "foreign seccomp helper; vendor/seccomp/loong64/apply-seccomp is built from the bundled sources instead",
    "**/@vscode/sandbox-runtime/vendor/seccomp/arm64/**": "foreign seccomp helper; vendor/seccomp/loong64/apply-seccomp is built from the bundled sources instead",
    "**/@teddyzhu/**/*.node": "vendored clipboard native module has no loongarch64 build",
    "**/@picovoice/**/*.node": "vendored pvrecorder native module has no loongarch64 build",
    "**/napi-oop-runtime/**/*.node": "vendored napi-oop-runtime has no loongarch64 build",
}


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


@dataclass
class Paths:
    """All directories a build touches."""

    root: Path
    work: Path
    cache: Path
    downloads: Path
    electron_cache: Path
    upstream: Path
    natives: Path
    stage: Path
    dist: Path
    logs: Path

    @classmethod
    def create(cls, root: Path) -> "Paths":
        root = Path(root).expanduser().resolve()
        work = root / "work"
        build = work / "build"
        cache = _env_path("VSCODE_LOONG64_CACHE", root / ".cache")
        electron_cache = _env_path("ELECTRON_LOONG64_CACHE", Path.home() / ".cache" / "electron-loong64")
        paths = cls(
            root=root,
            work=work,
            cache=cache,
            downloads=cache / "downloads",
            electron_cache=electron_cache,
            upstream=build / "upstream",
            natives=build / "natives",
            stage=build / "stage",
            dist=root / "dist",
            logs=work / "logs",
        )
        for directory in (
            paths.work,
            paths.cache,
            paths.downloads,
            paths.upstream,
            paths.natives,
            paths.stage,
            paths.dist,
            paths.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return paths

    def describe(self) -> Dict[str, str]:
        return {
            "root": str(self.root),
            "downloads": str(self.downloads),
            "electron_cache": str(self.electron_cache),
            "stage": str(self.stage),
            "dist": str(self.dist),
            "logs": str(self.logs),
        }


@dataclass
class BuildConfig:
    """Everything the pipeline needs to know about one build."""

    version: str = "latest"
    quality: str = "stable"
    paths: Optional[Paths] = None
    #: official binary used as the payload source
    source_platform: str = "linux-x64"
    source_package: str = "tar.gz"
    #: rebuild native modules instead of reusing upstream x64 binaries
    rebuild_natives: bool = True
    #: allow the closest Electron build when the exact one is missing
    allow_electron_fallback: bool = True
    #: branding of the produced build
    app_name: str = "Code"
    app_name_long: str = "Visual Studio Code"
    binary_name: str = "code"
    #: packaging formats to emit
    formats: List[str] = field(default_factory=lambda: ["tar.gz", "deb"])
    artifact_suffix: str = "loong64"
    offline: bool = False
    force: bool = False
    jobs: int = 0
    keep_work: bool = True
    #: explicit inputs (used by ``build --from-dir`` / CI)
    electron_archive: Optional[Path] = None
    upstream_archive: Optional[Path] = None
    upstream_dir: Optional[Path] = None
    electron_version: Optional[str] = None
    #: optional LoongArch ripgrep binary
    ripgrep_binary: Optional[Path] = None
    #: checkout of microsoft/os-proxy-resolver (heads containing this version)
    os_proxy_resolver_source: Optional[Path] = None
    #: build the standalone Rust helpers (ripgrep) as static musl binaries;
    #: Node-API addons always follow the Electron runtime's glibc ABI
    static_rust: bool = True
    #: PAC backend feature used when compiling the proxy addon
    os_proxy_features: str = OS_PROXY_RESOLVER_FEATURES
    #: skip the native module build entirely
    skip_natives: bool = False
    #: rebuild the standalone helpers (code-tunnel, copilot shim, apply-seccomp,
    #: vsce-sign) instead of keeping the upstream x64 binaries
    rebuild_helpers: bool = True
    #: extra directories searched for prebuilt helpers, in order
    prebuilt_dirs: List[Path] = field(default_factory=list)
    #: allow the builder to download a LoongArch .NET SDK for NativeAOT
    dotnet_download: bool = True
    #: portable archive with the architecture specific payloads of a previous
    #: build (see :mod:`vscode_loong64.prebuilts`); skips every compile step
    prebuilt_bundle: Optional[Path] = None
    #: write such a bundle after a successful build
    emit_prebuilt_bundle: bool = False

    @property
    def nproc(self) -> int:
        return self.jobs or os.cpu_count() or 1

    def artifact_name(self, fmt: str) -> str:
        stem = f"{self.binary_name}-{self.version}-{self.artifact_suffix}"
        if fmt in ("deb", "rpm"):
            return f"{self.binary_name}_{self.version}_{self.artifact_suffix}.{fmt}"
        return f"{stem}.{fmt}"

    def __post_init__(self) -> None:
        if self.paths is None:
            self.paths = Paths.create(PROJECT_ROOT)

    def validate(self) -> None:
        if self.quality not in ("stable", "insider"):
            raise BuildError(f"unsupported quality: {self.quality}")
        for fmt in self.formats:
            if fmt not in ("tar.gz", "deb", "rpm", "zip"):
                raise BuildError(f"unsupported package format: {fmt}")
        if self.paths is None:
            raise BuildError("paths are not configured")

    def describe(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "quality": self.quality,
            "sourcePlatform": self.source_platform,
            "electronFallback": self.allow_electron_fallback,
            "rebuildNatives": self.rebuild_natives,
            "rebuildHelpers": self.rebuild_helpers,
            "prebuiltBundle": str(self.prebuilt_bundle) if self.prebuilt_bundle else None,
            "formats": self.formats,
            "paths": self.paths.describe() if self.paths else {},
            "prebuiltDirs": [str(p) for p in self.prebuilt_dirs],
            "host": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        }


def host_machine() -> str:
    return platform.machine()


def is_loong64_host() -> bool:
    return host_machine() in ("loongarch64", "loong64")


def missing_tools(required: List[str]) -> List[str]:
    return [tool for tool in required if not shutil.which(tool)]


def ensure_tools(required: List[str]) -> None:
    missing = missing_tools(required)
    if missing:
        raise BuildError(
            "missing required tools: "
            + ", ".join(missing)
            + " (install build-essential, binutils, zstd, npm)"
        )
