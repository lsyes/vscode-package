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

#: packages whose only architecture specific content is a prebuilt executable
PREBUILT_MODULES: List[str] = [
    "@vscode/ripgrep-universal",
    "@microsoft/copilot-sdk-linux-x64",
    "@vscode/deviceid",
    "@vscode/vsce-sign",
]


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
            "formats": self.formats,
            "paths": self.paths.describe() if self.paths else {},
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
