"""The end-to-end build pipeline.

The pipeline is a sequence of explicit phases.  Every phase is idempotent so a
failed run can be resumed with ``--resume`` (or simply by re-running it, because
downloads and native builds are cached on disk).

    1. resolve      – figure out which upstream VSCode/Electron versions to use
    2. download     – fetch the official VSCode binaries and the loong64 Electron
    3. extract      – unpack both archives
    4. natives      – rebuild x64 native modules for loong64
    5. stage        – assemble the loong64 application tree
    6. verify       – structural checks (+ optional headless smoke test)
    7. package      – produce tar.gz/deb/rpm artifacts
    8. manifest     – write build metadata for the release job
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import artifacts, dotnet, electron, helpers, pack, prebuilts, seccomp, upstream, utils
from .config import BuildConfig, is_loong64_host
from .natives import NativeBuild, NativeBuilder
from .config import NATIVE_MODULES as config_native_specs
from .utils import BuildError, human_size, log, section, warn


@dataclass
class BuildResult:
    config: BuildConfig
    release: upstream.ReleaseInfo
    stage: Optional[pack.Stage] = None
    artifacts: List[artifacts.Artifact] = field(default_factory=list)
    manifest_path: Optional[Path] = None
    timings: Dict[str, float] = field(default_factory=dict)
    checks: List[Dict[str, object]] = field(default_factory=list)
    change_report: Dict[str, object] = field(default_factory=dict)
    helper_results: Dict[str, object] = field(default_factory=dict)
    bundle: Optional[Path] = None

    def manifest(self) -> Dict[str, object]:
        stage = self.stage
        return {
            "schemaVersion": 1,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "vscode": self.release.as_dict(),
            "electron": (
                {
                    "version": stage.electron_runtime.version,
                    "requested": stage.electron_runtime.requested_version,
                    "arch": stage.electron_runtime.arch,
                    "source": stage.electron_runtime.source,
                }
                if stage and stage.electron_runtime
                else None
            ),
            "arch": "loong64",
            "config": {
                "quality": self.config.quality,
                "sourcePlatform": self.config.source_platform,
                "formats": self.config.formats,
                "rebuildNatives": self.config.rebuild_natives,
                "rebuildHelpers": self.config.rebuild_helpers,
                "binaryName": self.config.binary_name,
                "staticRust": self.config.static_rust,
                "cxxAddons": "loongarch64-gnu",
                "proxyResolver": {
                    "target": "loongarch64-unknown-linux-gnu",
                    "backend": self.config.os_proxy_features,
                },
            },
            "artifacts": [a.as_dict() for a in self.artifacts],
            "prebuiltBundle": str(self.bundle) if self.bundle else None,
            "prebuiltBundleInput": (
                str(self.config.prebuilt_bundle) if self.config.prebuilt_bundle else None
            ),
            "upstreamChange": getattr(self, "change_report", {}),
            "helperBuilds": self._helper_report(),
            "nativeModules": stage.native_report if stage else {},
            "checks": self.checks,
            "warnings": stage.warnings if stage else [],
            "timings": {k: round(v, 2) for k, v in self.timings.items()},
        }

    def _helper_report(self) -> List[Dict[str, object]]:
        return [
            result.as_dict()
            for result in self.helper_results.values()
            if hasattr(result, "as_dict")
        ]

    def write_manifest(self, path: Optional[Path] = None) -> Path:
        target = path or (self.config.paths.dist / "manifest.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.manifest(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
        self.manifest_path = target
        log("manifest written to %s", target)
        return target


class Pipeline:
    """Drives one build end to end."""

    def __init__(self, config: BuildConfig) -> None:
        config.validate()
        self.config = config
        self.paths = config.paths
        self.result: Optional[BuildResult] = None
        self.change_report: Dict[str, object] = {}
        self.bundle = None
        self._started = time.time()

    # -- helpers -----------------------------------------------------------------

    def _phase(self, name: str, started: float) -> None:
        elapsed = time.time() - started
        if self.result is not None:
            self.result.timings[name] = elapsed
        log("%s finished in %.1fs", name, elapsed)

    # -- phases ------------------------------------------------------------------

    def resolve(self) -> upstream.ReleaseInfo:
        section("Resolving upstream release")
        started = time.time()
        if self.config.upstream_archive and (self.config.offline or self.config.upstream_dir):
            artifact = Path(self.config.upstream_archive).expanduser().resolve()
            if artifact.exists():
                payload = (
                    pack.load_payload(self.config.upstream_dir.expanduser().resolve())
                    if self.config.upstream_dir
                    else None
                )
                if payload is None:
                    # A local archive is enough for an offline repack: version
                    # and commit are preserved in the resulting manifest.
                    name = artifact.name
                    version = self.config.version
                    if version in ("", "latest"):
                        version = "0.0.0"
                    release = upstream.ReleaseInfo(
                        version=version,
                        commit="",
                        quality=self.config.quality,
                        platform=self.config.source_platform,
                        url=str(artifact),
                        sha256=utils.sha256_file(artifact),
                    )
                    self._phase("resolve", started)
                    return release
                release = upstream.ReleaseInfo(
                    version=payload.version,
                    commit=payload.commit,
                    quality=self.config.quality,
                    platform=self.config.source_platform,
                    url=str(artifact),
                    sha256=utils.sha256_file(artifact),
                    product_version=payload.version,
                )
                self._phase("resolve", started)
                return release
        try:
            release = upstream.resolve_version(
                self.config.version,
                platform=self.config.source_platform,
                quality=self.config.quality,
            )
        except BuildError as exc:
            if self.config.offline:
                warn("offline mode: %s", exc)
                raise
            raise
        log("upstream: VSCode %s (%s)", release.version, release.commit[:8])
        if release.product_version and release.product_version != release.version:
            release.version = release.product_version
        self._phase("resolve", started)
        return release

    def check_upstream(self, release: upstream.ReleaseInfo) -> Dict[str, object]:
        """Record whether the payload is the newest upstream release.

        The helper sources are keyed by the *payload* version, so an upstream
        update automatically re-fetches ``cli/`` and ``copilot_shim/`` instead of
        rebuilding against stale sources.  This method only reports the delta;
        the actual resolution happens in :class:`helpers.HelperBuilder`.
        """

        latest = None
        if not self.config.offline:
            latest = upstream.latest_or_none(
                platform=self.config.source_platform, quality=self.config.quality
            )
        latest_version = latest.version if latest else None
        report = helpers.check_source_update(release.version, latest_version, self.config.quality)
        report["sourcesCached"] = (self.paths.cache / "sources" / f"vscode-{release.version}").exists()
        if report.get("update"):
            log(
                "upstream moved to %s (payload is %s): the helper sources will be re-fetched",
                latest_version,
                release.version,
            )
        elif latest_version:
            log("payload %s is the newest upstream release", release.version)
        self.change_report = report
        return report

    def download(self, release: upstream.ReleaseInfo) -> Path:
        section("Downloading upstream sources")
        started = time.time()
        config = self.config
        if config.upstream_archive:
            archive = Path(config.upstream_archive).expanduser().resolve()
            if not archive.exists():
                raise BuildError(f"--upstream given but {archive} does not exist")
            log("using local upstream archive %s", archive.name)
        else:
            if config.offline:
                candidates = sorted(self.paths.downloads.glob("code*.tar.gz"))
                if not candidates:
                    raise BuildError("--offline requested but no upstream archive is cached")
                archive = candidates[-1]
                log("offline: using cached %s", archive.name)
            else:
                archive = self._download_upstream(release)
        self._phase("download", started)
        return archive

    def _download_upstream(self, release: upstream.ReleaseInfo) -> Path:
        config = self.config
        package = config.source_package
        url = release.url
        if not url:
            url = upstream.download_url_for(release.version, platform=config.source_platform, package=package, quality=config.quality)
        suffix = ".tar.gz" if package == "tar.gz" else f".{package}"
        dest = self.paths.downloads / f"code-{release.version}-{config.source_platform}{suffix}"
        log("fetching %s", url)
        return utils.download(url, dest, expected_sha256=release.sha256)

    def resolve_electron(self, payload: pack.UpstreamPayload) -> electron.ElectronRuntime:
        section("Resolving Electron runtime")
        started = time.time()
        config = self.config
        required = config.electron_version or payload.electron_version
        if not required:
            raise BuildError(
                "unable to determine the Electron version required by this VSCode build "
                "(neither product.json nor package.json declares one); pass --electron-version"
            )
        log("VSCode %s requires Electron %s", payload.version, required)
        if config.electron_archive:
            runtime = electron.import_local(
                Path(config.electron_archive), version="auto", force=config.force
            )
            runtime.requested_version = required
            if runtime.version != required:
                warn(
                    "local Electron archive provides %s while VSCode %s was built against %s",
                    runtime.version,
                    payload.version,
                    required,
                )
        else:
            runtime = electron.fetch(
                required,
                force=False,
                offline=config.offline,
                fallback=config.allow_electron_fallback,
            )
        electron.verify(runtime, run_binary=False)
        if not runtime.exact_match:
            warn(
                "using Electron %s instead of %s (the exact LoongArch build is not published)",
                runtime.version,
                runtime.requested_version,
            )
        self._phase("electron", started)
        return runtime

    def extract(self, archive: Path, release: upstream.ReleaseInfo) -> pack.UpstreamPayload:
        section("Extracting upstream payload")
        started = time.time()
        if self.config.upstream_dir:
            payload = pack.load_payload(Path(self.config.upstream_dir).expanduser().resolve())
            log("using pre-extracted payload: %s", payload.describe())
            self._phase("extract", started)
            return payload
        target = self.paths.upstream / f"code-{release.version}-{self.config.source_platform}"
        marker = target / ".extracted"
        if marker.exists() and marker.read_text().strip() == release.commit:
            log("reusing previously extracted payload at %s", target)
        else:
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
            log("unpacking %s", archive.name)
            utils.extract(archive, target)
            marker.write_text(release.commit or release.version, encoding="utf-8")
        for directory in target.iterdir():
            if directory.is_dir() and (directory / "resources").is_dir():
                return pack.load_payload(directory)
        return pack.load_payload(target)

    def load_bundle(self, payload: pack.UpstreamPayload, runtime: electron.ElectronRuntime):
        """Unpack ``--prebuilt-bundle`` and seed the helper cache from it."""

        button = self.config.prebuilt_bundle
        if not button:
            return None
        section("Loading prebuilt payload bundle")
        started = time.time()
        bundle = prebuilts.unpack(
            Path(button),
            self.paths.work / "bundle-current",
        )
        prebuilts.check_compatible(bundle, payload.version, runtime.version)
        prebuilts.install_helpers(self.config, bundle, payload.version)
        self.bundle = bundle
        self._phase("bundle", started)
        return bundle

    def build_natives(self, payload: pack.UpstreamPayload, runtime: electron.ElectronRuntime) -> Dict[str, Path]:
        section("Building native modules")
        started = time.time()
        bundle = getattr(self, "bundle", None)
        if bundle is not None:
            artifacts_map = bundle.native_artifacts()
            if bundle.ripgrep is not None:
                artifacts_map["ripgrep"] = bundle.ripgrep
            log(
                "using %d prebuilt addon(s) and %s from the bundle",
                len(bundle.natives),
                "ripgrep" if bundle.ripgrep else "no ripgrep",
            )
            self._phase("natives", started)
            return artifacts_map
        if not self.config.rebuild_natives:
            log("native rebuild disabled (--no-natives), keeping upstream binaries")
            self._phase("natives", started)
            raise BuildError("--no-natives cannot produce a release: native addons and ripgrep must be rebuilt")
        builder = NativeBuilder(
            config=self.config,
            electron_version=runtime.version,
            workdir=self.paths.natives / f"electron-{runtime.version}",
        )
        builder.read_versions(payload.app_dir)
        existing = self._reuse_native_builds(builder)
        specs = tuple(spec for spec in config_native_specs if spec.name not in existing)
        builder.build_all(specs=specs)
        for name, binding in existing.items():
            builder.results[name] = binding
        self._phase("natives", started)
        result = builder.artifacts()
        result["ripgrep"] = builder.build_ripgrep()
        return result

    def build_helpers(self, payload: pack.UpstreamPayload) -> Dict[str, Path]:
        """Build the standalone helpers (tunnel CLI, copilot shim, ...)."""

        section("Building standalone helpers")
        started = time.time()
        bundle = getattr(self, "bundle", None)
        if bundle is not None:
            log("using %d prebuilt helper(s) from the bundle", len(bundle.helpers))
            self._phase("helpers", started)
            return dict(bundle.helpers)
        if not self.config.rebuild_helpers:
            warn("helper rebuild disabled: bin/code-tunnel and bin/copilot-shim stay x86-64")
            self._phase("helpers", started)
            return {}
        builder = helpers.HelperBuilder(
            config=self.config,
            version=payload.version,
            quality=self.config.quality,
        )
        # ``apply-seccomp`` is compiled from sources shipped inside the payload,
        # so it needs the extracted app directory rather than the checkout.
        builder.build_all(payload.app_dir)
        if self.result is not None:
            self.result.helper_results = dict(builder.results)
        self._phase("helpers", started)
        return builder.artifacts()

    def _reuse_native_builds(self, builder: NativeBuilder) -> Dict[str, object]:
        """Reuse addons that are already compiled for this Electron/runtime."""

        reused: Dict[str, object] = {}
        if self.config.force:
            return reused
        for spec in config_native_specs:
            if spec.kind in ("shim", "rust"):
                continue
            version = builder.versions.get(spec.name)
            if not version:
                continue
            source = builder.workdir / "src" / f"{spec.name.replace('/', '__').replace('@', '')}-{version}"
            binding = source / spec.binding_path
            if binding.exists() and pack.is_loongarch_elf(binding):
                reused[spec.name] = NativeBuild(name=spec.name, ok=True, binding=binding, detail="reused loongarch64 build", source=str(source))
        return reused

    def assemble(
        self,
        payload: pack.UpstreamPayload,
        runtime: electron.ElectronRuntime,
        native_artifacts: Dict[str, Path],
        helper_artifacts: Optional[Dict[str, Path]] = None,
    ) -> pack.Stage:
        section("Assembling LoongArch64 application")
        started = time.time()
        stage = pack.stage_payload(payload, self.paths.stage, self.config)
        stage.payload = payload
        stage.electron_runtime = runtime
        pack.apply_runtime(stage)
        pack.install_natives(stage, native_artifacts, strict=self.config.rebuild_natives)
        pack.disable_unloadable_natives(stage)
        replaced = pack.replace_ripgrep(stage, native_artifacts.get("ripgrep"))
        # Drop the payloads upstream never built for LoongArch *before* the
        # foreign-helper sweep so the sweep only reports real mistakes.
        pack.prune_unsupported_prebuilts(stage)
        helpers.install(stage, helper_artifacts or {})
        # the asar keeps its own copy of the seccomp helper packed inside it
        pack.prune_asar_payloads(stage)
        pack.disable_foreign_helpers(stage, replaced)
        pack.patch_product(stage)
        self._phase("stage", started)
        return stage

    def verify(self, stage: pack.Stage, run_app: bool = True) -> List[Dict[str, object]]:
        section("Verifying build")
        started = time.time()
        checks = [c.as_dict() for c in pack.verify_layout(stage)]
        failures = [c for c in checks if not c["ok"]]
        for check in checks:
            if not check["ok"]:
                warn("check failed: %s (%s)", check["name"], check.get("detail", ""))
        if run_app and is_loong64_host():
            checks.append(self._smoke_test(stage))
        elif run_app:
            warn("host is %s, skipping the executable smoke test", utils_run_platform())
        self._phase("verify", started)
        if failures:
            raise BuildError(f"{len(failures)} verification check(s) failed: " + ", ".join(c["name"] for c in failures))
        return checks

    def _smoke_test(self, stage: pack.Stage) -> Dict[str, object]:
        """Run the packaged CLI without a display and compare the version.

        The launcher (``bin/code``) starts the binary with
        ``ELECTRON_RUN_AS_NODE=1`` and feeds it ``out/cli.js``; invoking the binary
        directly with ``--version`` would boot the whole workbench instead.
        """

        launcher = stage.root / "bin" / stage.config.binary_name
        binary = stage.executable
        expected = stage.payload.version if stage.payload else None
        if launcher.exists():
            cmd = [str(launcher), "--version"]
        else:
            cmd = [str(binary), "--no-sandbox", "--disable-gpu", "resources/app/out/cli.js", "--version"]
        env = os.environ.copy()
        env.setdefault("ELECTRON_RUN_AS_NODE", "1" if launcher.exists() else "0")
        log("smoke test: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(stage.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return {"name": "smoke test", "ok": False, "detail": "timed out after 180s"}
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        reported = stdout.splitlines()[0].strip() if stdout else ""
        ok = proc.returncode == 0 and (expected is None or expected in stdout)
        detail = f"reported {reported or 'nothing'}"
        if proc.returncode != 0:
            tail = (stderr or stdout).splitlines()[-1] if (stderr or stdout) else ""
            detail += f", exit {proc.returncode}: {tail[:300]}"
        return {"name": "smoke test", "ok": ok, "detail": detail}

    def package(self, stage: pack.Stage) -> List[artifacts.Artifact]:
        section("Packaging artifacts")
        started = time.time()
        produced = artifacts.build_all(stage.root, self.config)
        for artifact in produced:
            log("%s (%s)", artifact.path.name, human_size(artifact.size))
        self._phase("package", started)
        return produced

    # -- entry point -------------------------------------------------------------

    def run(self, verify_app: bool = True) -> BuildResult:
        started = time.time()
        config = self.config
        log("build started: version=%s quality=%s formats=%s", config.version, config.quality, ",".join(config.formats))
        log("paths: %s", json.dumps(config.paths.describe()))
        release = self.resolve()
        self.result = BuildResult(config=config, release=release)
        archive = self.download(release)
        payload = self.extract(archive, release)
        self.result.release = upstream.ReleaseInfo(
            version=payload.version or release.version,
            commit=payload.commit or release.commit,
            quality=release.quality,
            platform=release.platform,
            timestamp=release.timestamp,
            product_version=payload.version,
            url=release.url,
            sha256=release.sha256,
            sha1=release.sha1,
        )
        log("payload: %s", payload.describe())
        self.result.change_report = self.check_upstream(self.result.release)
        runtime = self.resolve_electron(payload)
        self.load_bundle(payload, runtime)
        native_artifacts = self.build_natives(payload, runtime)
        helper_artifacts = self.build_helpers(payload)
        stage = self.assemble(payload, runtime, native_artifacts, helper_artifacts)
        self.result.stage = stage
        if self.config.emit_prebuilt_bundle:
            self.result.bundle = prebuilts.pack(
                self.config,
                version=self.result.release.version,
                electron=runtime.version,
                helper_artifacts=helper_artifacts,
                native_artifacts=native_artifacts,
                commit=self.result.release.commit,
            )
        self.result.checks = self.verify(stage, run_app=verify_app)
        # artifact names carry the real upstream version, not the selector
        if self.result.release.version and self.result.release.version != "latest":
            config.version = self.result.release.version
        self.result.artifacts = self.package(stage)
        self.result.timings["total"] = time.time() - started
        self.result.write_manifest()
        section("Build complete")
        log("total time %.1fs", self.result.timings["total"])
        for artifact in self.result.artifacts:
            log("%s  %s  %s", artifact.sha256[:16], human_size(artifact.size), artifact.path.name)
        return self.result


def utils_run_platform() -> str:
    return platform.machine()


def build(config: BuildConfig, verify_app: bool = True) -> BuildResult:
    return Pipeline(config).run(verify_app=verify_app)
