"""Build LoongArch64 versions of the native modules VSCode loads at runtime.

The official Microsoft binaries ship ``.node`` addons compiled for x86-64.  They
cannot be loaded by a LoongArch Electron, so they are rebuilt from source with
``node-gyp`` against the Electron headers matching the target runtime.

Sources come from one of three places, in order:

1. ``VSCODE_LOONG64_NATIVE_ARCHIVE`` – a tarball of an already prepared
   ``node_modules`` tree (used by CI to reuse a cache).
2. The staged application itself (``node_modules.asar.unpacked`` keeps the
   upstream sources for some packages).
3. npm (``npm pack`` / ``npm install --ignore-scripts``).

Each module is built in an isolated directory so a broken module never poisons
the others.  Failures are recorded and either abort the build (required modules)
or degrade gracefully with a warning (optional modules).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import musltoolchain, sysroot, utils
from .config import (
    BuildConfig,
    NATIVE_MODULES,
    OS_PROXY_RESOLVER_NATIVE_DIR,
    OS_PROXY_RESOLVER_PACKAGE,
    OS_PROXY_RESOLVER_REF,
    OS_PROXY_RESOLVER_REPO,
    OS_PROXY_RESOLVER_TARGET,
    NativeModuleSpec,
)
from .pack import arch_label, is_loongarch_elf
from .utils import BuildError, human_size, log, warn

#: extra packages that must be present for the builds to succeed
BUILD_TOOLS = ("node", "npm", "make", "g++", "python3", "cargo", "rustc")


@dataclass
class NativeBuild:
    """Result of building one native module."""

    name: str
    ok: bool
    binding: Optional[Path] = None
    detail: str = ""
    log_path: Optional[Path] = None
    source: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "binding": str(self.binding) if self.binding else None,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class NativeBuilder:
    """Builds LoongArch native modules for a given Electron version."""

    config: BuildConfig
    electron_version: str
    workdir: Path
    versions: Dict[str, str] = field(default_factory=dict)
    results: Dict[str, NativeBuild] = field(default_factory=dict)
    node_gyp: Optional[str] = None
    sysroots: Dict[str, sysroot.Sysroot] = field(default_factory=dict)

    # -- helpers -----------------------------------------------------------------

    @property
    def env(self) -> Dict[str, str]:
        env = {
            "npm_config_target": self.electron_version,
            "npm_config_runtime": "electron",
            "npm_config_disturl": "https://electronjs.org/headers",
            "npm_config_arch": "loong64",
            "npm_config_target_arch": "loong64",
            "npm_config_build_from_source": "true",
            "npm_config_ignore_scripts": "false",
            "npm_config_jobs": str(self.config.nproc),
            "JOBS": str(self.config.nproc),
            "HOME": os.environ.get("HOME", str(Path.home())),
        }
        for key in ("PATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "npm_config_registry"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def module_env(self, module: str) -> Dict[str, str]:
        """Environment for compiling ``module``, including its header sysroot."""

        env = dict(self.env)
        packages = sysroot.packages_for(module)
        if not packages:
            return env
        root = self.sysroots.get(module)
        if root is None:
            root = sysroot.provision(self.config.paths.cache, self._unique_packages(), force=False)
            for key in list(self.sysroots):
                self.sysroots[key] = root
            self.sysroots[module] = root
        env.update(root.env(env))
        return env

    def _unique_packages(self) -> List[str]:
        seen: List[str] = []
        for spec in NATIVE_MODULES:
            for package in sysroot.packages_for(spec.name):
                if package not in seen:
                    seen.append(package)
        return seen

    def _log(self, name: str, text: str) -> Path:
        path = self.config.paths.logs / f"native-{name.replace('/', '_').replace('@', '')}.log"
        path.write_text(text, encoding="utf-8", errors="replace")
        return path

    def _node_gyp(self) -> str:
        """Use a cached node-gyp so offline rebuilds do not invoke ``npx``."""

        if self.node_gyp:
            return self.node_gyp
        found = shutil.which("node-gyp")
        if found:
            self.node_gyp = found
            return found
        cached = sorted(
            Path(os.environ.get("HOME", str(Path.home()))).glob(".npm/_npx/*/node_modules/node-gyp/bin/node-gyp.js"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if cached:
            self.node_gyp = str(cached[0])
            return self.node_gyp
        raise BuildError("node-gyp is required for native module builds (npm install -g node-gyp@11)")

    # -- source acquisition -------------------------------------------------------

    def read_versions(self, app_dir: Path) -> Dict[str, str]:
        """Resolve the exact versions the application expects."""

        versions: Dict[str, str] = {}
        asar_path = app_dir / "node_modules.asar"
        if asar_path.exists():
            try:
                versions.update(asar_read_versions(asar_path))
            except BuildError as exc:
                warn("unable to read %s: %s", asar_path, exc)
        for extra in (app_dir / "node_modules.asar.unpacked", app_dir / "node_modules"):
            if not extra.is_dir():
                continue
            for package_json in extra.glob("*/package.json"):
                try:
                    data = json.loads(package_json.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("name") and data.get("version"):
                    versions.setdefault(str(data["name"]), str(data["version"]))
            for package_json in extra.glob("@*/*/package.json"):
                try:
                    data = json.loads(package_json.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("name") and data.get("version"):
                    versions.setdefault(str(data["name"]), str(data["version"]))
        self.versions = versions
        return versions

    def fetch_sources(self, spec: NativeModuleSpec) -> Optional[Path]:
        """Download the source tarball of ``spec`` from npm and extract it."""

        version = self.versions.get(spec.name)
        if not version:
            warn("no version known for %s, skipping", spec.name)
            return None
        dest_root = self.workdir / "src"
        dest_root.mkdir(parents=True, exist_ok=True)
        sanitized = spec.name.replace("/", "__").replace("@", "")
        dest = dest_root / f"{sanitized}-{version}"
        if dest.is_dir():
            return dest
        tarball_dir = self.workdir / "tarballs"
        tarball_dir.mkdir(parents=True, exist_ok=True)
        proc = utils.run(
            ["npm", "pack", f"{spec.name}@{version}", "--pack-destination", str(tarball_dir), "--silent"],
            env=self.env,
            check=False,
        )
        if proc.returncode != 0:
            self._log(spec.name, (proc.stdout or "") + (proc.stderr or ""))
            warn("unable to download sources for %s@%s", spec.name, version)
            return None
        candidates = sorted(tarball_dir.glob(f"*{spec.name.split('/')[-1]}-{version}.tgz"))
        if not candidates:
            candidates = sorted(tarball_dir.glob("*.tgz"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            warn("npm pack produced no tarball for %s", spec.name)
            return None
        archive = candidates[-1]
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tf:
            utils_members = [m for m in tf.getmembers() if m.name.startswith("package/")]
            for member in utils_members:
                member.name = member.name[len("package/"):]
                if not member.name:
                    continue
                tf.extract(member, dest)
        return dest

    def install_dependencies(self, source_dir: Path, spec: NativeModuleSpec) -> None:
        """Install the module's own build dependencies (node-addon-api etc.)."""

        package_json = source_dir / "package.json"
        if not package_json.exists():
            return
        data = json.loads(package_json.read_text(encoding="utf-8"))
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        wanted = {k: v for k, v in deps.items() if k in ("node-addon-api", "node-gyp-build", "nan", "bindings", "prebuild-install", "node-gyp")}
        if not wanted:
            return
        if any((source_dir / "node_modules" / dep / "package.json").exists() for dep in wanted):
            return
        log("installing build dependencies for %s: %s", spec.name, ", ".join(sorted(wanted)))
        proc = utils.run(
            ["npm", "install", "--no-audit", "--no-fund", "--ignore-scripts", "--no-save", *sorted(wanted)],
            cwd=source_dir,
            env=self.env,
            check=False,
        )
        if proc.returncode != 0:
            self._log(spec.name + "-deps", (proc.stdout or "") + (proc.stderr or ""))
            warn("dependency installation failed for %s", spec.name)

    # -- build -------------------------------------------------------------------

    def build_one(self, spec: NativeModuleSpec) -> NativeBuild:
        if spec.kind == "shim":
            return NativeBuild(name=spec.name, ok=True, detail="shim handled by the packager", source="shim")
        if spec.kind == "rust":
            return self.build_proxy_resolver(spec)
        source_dir = self.fetch_sources(spec)
        if source_dir is None:
            return NativeBuild(name=spec.name, ok=False, detail="source unavailable")
        if not (source_dir / "binding.gyp").exists():
            return NativeBuild(name=spec.name, ok=False, detail="package has no binding.gyp", source=str(source_dir))
        self.install_dependencies(source_dir, spec)
        cmd = [
            "node",
            self._node_gyp(),
            "rebuild",
            f"--target={self.electron_version}",
            "--arch=loong64",
            "--dist-url=https://electronjs.org/headers",
            f"--jobs={self.config.nproc}",
        ]
        log("building %s for Electron %s", spec.name, self.electron_version)
        env = {**self.module_env(spec.name), "npm_config_jobs": str(self.config.nproc)}
        proc = utils.run(
            cmd,
            cwd=source_dir,
            env=env,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        log_path = self._log(spec.name, output)
        binding = source_dir / spec.binding_path
        if not binding.exists():
            tail = output.strip().splitlines()[-4:]
            return NativeBuild(
                name=spec.name,
                ok=False,
                detail="; ".join(tail)[:400] or "node-gyp failed",
                log_path=log_path,
                source=str(source_dir),
            )
        if not is_loongarch_elf(binding):
            return NativeBuild(
                name=spec.name,
                ok=False,
                detail=f"produced {arch_label(binding)} instead of loongarch64",
                log_path=log_path,
                source=str(source_dir),
            )
        log("built %s (%s, %s)", spec.name, arch_label(binding), human_size(binding.stat().st_size))
        return NativeBuild(name=spec.name, ok=True, binding=binding, log_path=log_path, source=str(source_dir))

    def _proxy_source(self) -> Path:
        """Return a writable Microsoft ``os-proxy-resolver`` checkout.

        Only a real checkout (the one with the ``npm/native`` Node-API crate)
        is accepted, so a hand-written stand-in crate can never end up in a
        package.
        """

        managed = self.workdir / "src" / "os-proxy-resolver"
        marker = managed / OS_PROXY_RESOLVER_NATIVE_DIR / "Cargo.toml"

        def validate(source: Path) -> None:
            if not (source / OS_PROXY_RESOLVER_NATIVE_DIR / "Cargo.toml").exists():
                raise BuildError(
                    f"{source} is not a microsoft/os-proxy-resolver checkout "
                    "(npm/native/Cargo.toml is missing)"
                )

        explicit = self.config.os_proxy_resolver_source
        if explicit:
            source = Path(explicit).expanduser().resolve()
            validate(source)
            # always refresh so an updated checkout is really used, and patch
            # only the private copy
            if managed.exists():
                shutil.rmtree(managed)
            log("copying proxy sources from %s", source)
            shutil.copytree(
                source,
                managed,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", "target", "node_modules"),
            )
            validate(managed)
            return managed
        if marker.exists():
            return managed
        if managed.exists():
            # leftover from an older revision (or a stand-in crate): start over
            shutil.rmtree(managed)
        if self.config.offline:
            raise BuildError(
                "os-proxy-resolver sources are required for the proxy addon; "
                "pass --os-proxy-src or pre-populate "
                "work/build/natives/<electron>/src/os-proxy-resolver"
            )
        if not shutil.which("git"):
            raise BuildError("git is required to fetch microsoft/os-proxy-resolver")
        managed.parent.mkdir(parents=True, exist_ok=True)
        proc = utils.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                OS_PROXY_RESOLVER_REF,
                OS_PROXY_RESOLVER_REPO,
                str(managed),
            ],
            env=self.env,
            check=False,
        )
        if proc.returncode != 0:
            raise BuildError(
                "unable to clone microsoft/os-proxy-resolver "
                + ((proc.stdout or "") + (proc.stderr or ""))[-1200:]
            )
        validate(managed)
        return managed

    def _proxy_backend(self, source: Path) -> str:
        """Pin the PAC backend used by the Node-API crate.

        Upstream's ``npm/native/Cargo.toml`` hardcodes ``pac-engine-wasm2c``,
        which needs WABT 1.0.41 exactly.  The native QuickJS-NG backend
        evaluates the same PAC scripts without any extra host tooling.  Set
        ``VSCODE_LOONG64_PROXY_BACKEND=wasm2c`` to keep the wasm sandbox when
        the pinned WABT is available.
        """

        manifest = source / OS_PROXY_RESOLVER_NATIVE_DIR / "Cargo.toml"
        wanted = os.environ.get("VSCODE_LOONG64_PROXY_BACKEND", self.config.os_proxy_features)
        feature = "pac-engine-wasm2c" if wanted == "wasm2c" else wanted
        text = manifest.read_text(encoding="utf-8")
        pattern = r'features = \["pac-engine(?:-wasm2c|-wasmtime|-wasmtime-jit)?"\]'
        if not re.search(pattern, text):
            raise BuildError(f"unrecognised os-proxy-resolver dependency features in {manifest}")
        text = re.sub(pattern, f'features = ["{feature}"]', text)
        manifest.write_text(text, encoding="utf-8")
        return feature

    def build_proxy_resolver(self, spec: NativeModuleSpec) -> NativeBuild:
        """Build the Microsoft Node-API proxy resolver for LoongArch glibc."""

        source = self._proxy_source()
        manifest = source / OS_PROXY_RESOLVER_NATIVE_DIR / "Cargo.toml"
        before = manifest.read_text(encoding="utf-8")
        backend = self._proxy_backend(source)
        backend_changed = manifest.read_text(encoding="utf-8") != before
        target_dir = self.workdir / "target" / "os-proxy-resolver"
        env = dict(self.env)
        env.update(musltoolchain.gnu_rust_env())
        cargo_home = musltoolchain.cargo_home(self.config.paths.cache, self.config.offline)
        env["CARGO_HOME"] = str(cargo_home)
        env["CARGO_NET_OFFLINE"] = "true" if self.config.offline else "false"
        env["CARGO_TARGET_DIR"] = str(target_dir)
        cmd = [
            "cargo",
            "build",
            "--release",
            "--target",
            OS_PROXY_RESOLVER_TARGET,
            "--manifest-path",
            str(manifest),
        ]
        # ``--locked`` only works while the checked-in Cargo.lock still matches
        # the dependency set; after ``_proxy_backend`` rewrites the feature list
        # cargo has to update the lock file, so the flag is opt-in.
        lockfile = manifest.parent / "Cargo.lock"
        if lockfile.exists() and not backend_changed:
            cmd.append("--locked")
        log(
            "building os-proxy-resolver %s for %s (%s backend)",
            OS_PROXY_RESOLVER_REF,
            OS_PROXY_RESOLVER_TARGET,
            backend,
        )
        proc = utils.run(cmd, cwd=source, env=env, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        log_path = self._log(spec.name, output)
        release_dir = target_dir / OS_PROXY_RESOLVER_TARGET / "release"
        candidates = sorted(release_dir.glob("libos_proxy_resolver_node.so")) + sorted(
            release_dir.glob("*.so")
        )
        binding = candidates[0] if candidates else None
        if proc.returncode != 0 or binding is None:
            tail = output.strip().splitlines()[-1][:300] if output.strip() else "cargo build failed"
            return NativeBuild(
                name=spec.name,
                ok=False,
                detail="cargo build failed or produced no .so: " + tail,
                log_path=log_path,
                source=str(source),
            )
        if not is_loongarch_elf(binding):
            return NativeBuild(
                name=spec.name,
                ok=False,
                detail=f"produced {arch_label(binding)} instead of loongarch64",
                log_path=log_path,
                source=str(source),
            )
        problems = musltoolchain.glibc_addon_problems(binding)
        if problems:
            return NativeBuild(
                name=spec.name,
                ok=False,
                detail="proxy addon cannot load into the glibc runtime: " + "; ".join(problems),
                log_path=log_path,
                source=str(source),
            )
        log(
            "built os-proxy-resolver (%s, %s, %s)",
            arch_label(binding),
            backend,
            human_size(binding.stat().st_size),
        )
        return NativeBuild(name=spec.name, ok=True, binding=binding, log_path=log_path, source=str(source))

    def build_ripgrep(self) -> Path:
        """Build ripgrep from crates.io as a static LoongArch/musl executable."""

        if self.config.ripgrep_binary:
            binary = Path(self.config.ripgrep_binary).expanduser().resolve()
            musltoolchain.verify_static(binary, "ripgrep")
            return binary
        cached = self.config.paths.cache / "tools" / "rg"
        if cached.exists() and not self.config.force and not musltoolchain.static_problems(cached):
            return cached
        musltoolchain.verify_rust_target()
        work = self.workdir / "ripgrep"
        source = work / "source"
        if not (source / "Cargo.toml").exists():
            candidates = [p for p in work.rglob("Cargo.toml") if "target" not in p.parts]
            if candidates:
                source = candidates[0].parent
        work.mkdir(parents=True, exist_ok=True)
        if not source.is_dir():
            tarball = work / "ripgrep-14.1.1.crate"
            if not tarball.exists():
                cache = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
                cached = list((cache / "registry/cache").glob("*/ripgrep-14.1.1.crate"))
                if cached:
                    shutil.copy2(cached[0], tarball)
                elif self.config.offline:
                    raise BuildError("ripgrep source is not cached for an offline build")
                else:
                    proc = utils.run(
                        ["cargo", "download", "ripgrep@14.1.1", "--output", str(tarball)],
                        cwd=work,
                        env=self.env,
                        check=False,
                    )
                    if proc.returncode != 0:
                        raise BuildError("unable to download ripgrep source")
            source.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tarball) as tf:
                tf.extractall(source, filter="fully_trusted")
            if not (source / "Cargo.toml").exists():
                nested = sorted(source.glob("*/Cargo.toml"))
                if not nested:
                    raise BuildError(f"ripgrep archive has no Cargo.toml: {tarball}")
                source = nested[0].parent
        source = source.resolve()
        manifest = source / "Cargo.toml"
        if not (source / ".musl-static").exists():
            text = manifest.read_text(encoding="utf-8")
            marker = "[target.'cfg(all(target_env = \"musl\", target_pointer_width = \"64\"))'.dependencies.jemallocator]"
            text = text.replace(marker + "\nversion = \"0.5.0\"\n", "")
            text = text.replace(marker + '\nversion = "0.5.0"\n', "")
            manifest.write_text(text, encoding="utf-8")
            main_rs = source / "crates/core/main.rs"
            if main_rs.exists():
                text = main_rs.read_text(encoding="utf-8")
                text = re.sub(
                    r'#\[cfg\(all\(target_env = "musl", target_pointer_width = "64"\)\)\]\n#\[global_allocator\]\nstatic ALLOC: jemallocator::Jemalloc = jemallocator::Jemalloc;\n',
                    "",
                    text,
                )
                main_rs.write_text(text, encoding="utf-8")
            lockfile = source / "Cargo.lock"
            if lockfile.exists():
                lockfile.unlink()
            (source / ".musl-static").write_text("patched\n", encoding="utf-8")
        target_dir = (work / "target").resolve()
        cargo_home = musltoolchain.cargo_home(self.config.paths.cache, self.config.offline)
        env = dict(self.env)
        env.update(musltoolchain.static_rust_env())
        env["CARGO_TARGET_DIR"] = str(target_dir)
        env["CARGO_HOME"] = str(cargo_home)
        env["CARGO_NET_OFFLINE"] = "true" if self.config.offline else "false"
        cmd = [
            "cargo",
            "build",
            "--release",
            "--target",
            musltoolchain.MUSL_RUST_TARGET,
            "--manifest-path",
            str(manifest),
        ]
        log("building static ripgrep %s", "14.1.1")
        proc = utils.run(cmd, cwd=source, env=env, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        self._log("ripgrep", output)
        built = target_dir / musltoolchain.MUSL_RUST_TARGET / "release" / "rg"
        if proc.returncode != 0 or not built.exists():
            raise BuildError("ripgrep build failed: " + (output.strip().splitlines()[-1] if output.strip() else "cargo error"))
        musltoolchain.verify_static(built, "ripgrep")
        cached.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, cached)
        cached.chmod(0o755)
        return cached

    def build_all(self, specs: Sequence[NativeModuleSpec] = tuple(NATIVE_MODULES)) -> Dict[str, NativeBuild]:
        if not self.config.rebuild_natives or self.config.skip_natives:
            log("native module rebuild disabled, keeping upstream binaries")
            return {}
        missing = [tool for tool in BUILD_TOOLS if not shutil.which(tool)]
        if missing:
            if self.config.static_rust and {"cargo", "rustc"}.intersection(missing):
                raise BuildError("the static Rust build requires: " + ", ".join(sorted({"cargo", "rustc"} & set(missing))))
            if any(spec.required for spec in specs):
                raise BuildError(
                    "native modules are required but the toolchain is incomplete: " + ", ".join(missing)
                )
            return {}
        self.workdir.mkdir(parents=True, exist_ok=True)
        packages = self._unique_packages()
        if packages:
            shared = sysroot.provision(self.config.paths.cache, packages)
            for spec in specs:
                if sysroot.packages_for(spec.name):
                    self.sysroots[spec.name] = shared
        for spec in specs:
            result = self.build_one(spec)
            self.results[spec.name] = result
            if not result.ok:
                if spec.required:
                    raise BuildError(
                        f"failed to build required native module {spec.name}: {result.detail}"
                        + (f" (log: {result.log_path})" if result.log_path else "")
                    )
                warn("optional native module %s could not be built: %s", spec.name, result.detail)
        return self.results

    def artifacts(self) -> Dict[str, Path]:
        """Map module name -> root directory that contains its binding."""

        out: Dict[str, Path] = {}
        for name, result in self.results.items():
            if result.ok and result.binding and result.source and result.source != "shim":
                if name == OS_PROXY_RESOLVER_PACKAGE:
                    staging = self.workdir / "proxy-artifact"
                    staging.mkdir(parents=True, exist_ok=True)
                    target = staging / "os_proxy_resolver.node"
                    shutil.copy2(result.binding, target)
                    out[name] = staging
                else:
                    out[name] = Path(result.source)
        return out

def asar_read_versions(asar_path: Path) -> Dict[str, str]:
    """Read the installed package versions out of ``node_modules.asar``."""

    from . import asar

    archive = asar.AsarArchive.open(asar_path)
    versions: Dict[str, str] = {}
    for name in archive.files:
        if not name.endswith("/package.json"):
            continue
        package_json = name[: -len("/package.json")]
        depth = package_json.count("/")
        if depth > 1:  # ignore nested node_modules
            continue
        try:
            data = archive.read_json(name)
        except BuildError:
            continue
        if isinstance(data, dict) and data.get("name") and data.get("version"):
            versions[str(data["name"])] = str(data["version"])
    return versions
