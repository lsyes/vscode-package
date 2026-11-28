"""Rebuild the standalone helpers that ship next to the application.

Besides the ``.node`` addons there are four self-contained executables in a
VS Code Linux build that Microsoft only publishes for x64/arm64:

============  =========================================================
``bin/code-tunnel``          the Rust CLI behind ``code tunnel``,
                             ``code serve-web`` and ``code agent``
``bin/copilot-shim/copilot`` the Rust bootstrapper that starts the
                             GitHub Copilot CLI from a terminal
``apply-seccomp``            a static C helper used by
                             ``@vscode/sandbox-runtime``
``vsce-sign``                the .NET NativeAOT extension signature
                             verifier
============  =========================================================

All four are buildable from sources that either ship inside the payload
(``apply-seccomp``, ``vendor/seccomp-src``) or live in the VS Code repository
(``cli/`` and ``copilot_shim/``).  ``vsce-sign`` is C# and needs a NativeAOT
toolchain for LoongArch; a C# reimplementation lives in ``src-tools``.

Resolution order for every helper (first match wins):

1. an explicit ``--prebuilt`` directory (``VSCODE_LOONG64_PREBUILTS``),
2. the local prebuilt cache (``<cache>/prebuilts``), keyed by upstream
   version and architecture, so CI can reuse a previous build,
3. the repository's ``prebuilts/`` directory, which CI publishes,
4. compiling from the upstream sources for the *exact* version of the
   payload being repackaged.

The source tarball of the matching VS Code release is fetched from
``codeload.github.com`` (no git checkout needed) and cached by version, so an
upstream update is picked up automatically the next time the builder runs:
``:func:`upstream_tag_for`` always resolves to the release the payload came
from, and ``:func:`check_source_update``` reports when a newer tag exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import musltoolchain, patches, seccomp, sysroot, utils
from .config import STANDALONE_HELPERS, VSCODE_SOURCE_PATHS, VSCODE_SOURCE_TARBALL
from .utils import BuildError, human_size, log, warn

#: helpers that are compiled with cargo, with their target and link model
RUST_HELPERS: Dict[str, Dict[str, object]] = {
    "code-tunnel": {
        "crate_dir": "cli",
        "binary": "code",
        "target": musltoolchain.GNU_RUST_TARGET,
        "openssl": True,
        "static": True,
    },
    "copilot-shim": {
        "crate_dir": "copilot_shim",
        "binary": "copilot",
        "target": musltoolchain.MUSL_RUST_TARGET,
        "openssl": False,
        "static": True,
    },
}

#: ``-dev`` packages the Rust helpers need when they link native libraries
CARGO_SYSROOT_PACKAGES = ["libssl-dev", "libzstd-dev", "zlib1g-dev"]

MARKER_NAME = ".helper.json"

#: bump when the helper build recipe changes so cached prebuilts are rebuilt
RECIPE_VERSION = "2"


@dataclass
class HelperBuild:
    """Result of one helper build (or prebuilt lookup)."""

    name: str
    ok: bool
    binary: Optional[Path] = None
    source: str = ""
    detail: str = ""
    log_path: Optional[Path] = None
    prebuilt: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "binary": str(self.binary) if self.binary else None,
            "source": self.source,
            "detail": self.detail,
            "prebuilt": self.prebuilt,
        }


# --------------------------------------------------------------------------------------
# upstream source acquisition
# --------------------------------------------------------------------------------------

def upstream_tag_for(version: str, quality: str = "stable") -> str:
    """Map a product version to the git tag the sources live under."""

    version = (version or "").strip()
    if not version or version == "0.0.0":
        raise BuildError("cannot derive the VS Code source tag without an upstream version")
    return version if quality == "stable" else f"{version}"


def source_tarball_url(tag: str) -> str:
    return f"{VSCODE_SOURCE_TARBALL}/{tag}"


def check_source_update(
    current: str,
    latest: Optional[str],
    quality: str = "stable",
) -> Dict[str, object]:
    """Compare the payload version with the newest upstream release.

    Returns a small report; ``update`` is true when the upstream release moved
    ahead of the version that is being repackaged, which is also the moment the
    helper sources have to be fetched again.
    """

    from .upstream import change_report

    report = change_report(current, latest, quality)
    if current and current != "0.0.0":
        report["currentTag"] = upstream_tag_for(current, quality)
    if latest:
        report["latestTag"] = upstream_tag_for(latest, quality)
    return report


def _vscode_sources_dir(cache: Path, version: str) -> Path:
    return cache / "sources" / f"vscode-{version}"


def extract_source_subset(archive: Path, dest: Path, wanted: Sequence[str]) -> List[str]:
    """Extract only ``wanted`` top-level paths from a GitHub tarball."""

    dest.mkdir(parents=True, exist_ok=True)
    wanted_set = {w.strip("/") for w in wanted}
    written: List[str] = []
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) != 2 or parts[1] not in wanted_set and not any(
                parts[1].startswith(w + "/") for w in wanted_set
            ):
                continue
            member.name = parts[1]
            if member.isdir():
                continue
            if not member.name:
                continue
            try:
                tf.extract(member, dest, filter="fully_trusted")
            except TypeError:  # Python < 3.12
                tf.extract(member, dest)
            written.append(member.name)
    return written


def ensure_vscode_sources(
    version: str,
    cache: Path,
    quality: str = "stable",
    offline: bool = False,
    force: bool = False,
    paths: Sequence[str] = VSCODE_SOURCE_PATHS,
    timeout: int = 300,
) -> Path:
    """Return a directory holding ``cli/`` and ``copilot_shim/`` for ``version``."""

    tag = upstream_tag_for(version, quality)
    dest = _vscode_sources_dir(cache, tag)
    marker = dest / ".source.json"
    if marker.exists() and not force:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        if data.get("tag") == tag and (dest / "cli" / "Cargo.toml").exists():
            return dest
    if dest.exists() and force:
        shutil.rmtree(dest)
    downloads = cache / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"vscode-{tag}-source.tar.gz"
    url = source_tarball_url(tag)
    if not archive.exists():
        if offline:
            raise BuildError(
                f"VS Code {tag} sources are needed for the helper builds but "
                f"{archive} is missing and --offline was requested"
            )
        log("downloading VS Code %s sources (cli/, copilot_shim/)", tag)
        utils.download(url, archive, retries=2)
    written = extract_source_subset(archive, dest, paths)
    if not any(name.startswith("cli/") for name in written):
        raise BuildError(f"{archive.name} does not contain the cli/ crate")
    marker.write_text(
        json.dumps({"tag": tag, "url": url, "files": len(written)}, indent=2) + "\n",
        encoding="utf-8",
    )
    log("VS Code %s sources ready at %s", tag, dest)
    return dest


# --------------------------------------------------------------------------------------
# prebuilt lookup
# --------------------------------------------------------------------------------------

def _prebuilt_roots(config) -> List[Path]:
    roots: List[Path] = []
    explicit = os.environ.get("VSCODE_LOONG64_PREBUILTS")
    if explicit:
        roots.append(Path(explicit).expanduser().resolve())
    for candidate in getattr(config, "prebuilt_dirs", None) or []:
        roots.append(Path(candidate).expanduser().resolve())
    roots.append(config.paths.cache / "prebuilts")
    roots.append(config.paths.root / "prebuilts")
    return roots


def _helper_marker(data: Dict[str, object], name: str, version: str, arch: str) -> bool:
    return (
        str(data.get("name") or "") == name
        and str(data.get("version") or "") == version
        and str(data.get("arch") or "") == arch
    )


def recipe_identity(config, app_dir: Optional[Path], crate: str) -> Dict[str, object]:
    """Identity keys that make a prebuilt helper reusable.

    A prebuilt is only valid for the *same* upstream product metadata (the
    tunnel CLI bakes ``product.json`` values into the binary) and the same build
    recipe; :data:`RECIPE_VERSION` is bumped whenever the recipe changes.
    """

    identity: Dict[str, object] = {
        "recipe": RECIPE_VERSION,
        "crate": crate,
    }
    product = Path(app_dir) / "product.json" if app_dir else None
    if product is not None and product.exists():
        identity["productSha256"] = utils.sha256_file(product)
    else:
        identity["productSha256"] = None
    return identity


def _identity_mismatch(
    data: Dict[str, object], expected: Optional[Dict[str, object]]
) -> Optional[str]:
    """Return a description of the first identity key that does not match."""

    for key, value in (expected or {}).items():
        if value is None:
            continue
        if data.get(key) != value:
            return f"{key}={data.get(key)!r}, expected {value!r}"
    return None


def find_prebuilt(
    config,
    name: str,
    version: str,
    binary: str,
    arch: str = "loong64",
    expected: Optional[Dict[str, object]] = None,
) -> Optional[HelperBuild]:
    """Look for an already built helper matching ``version``/``arch``."""

    for root in _prebuilt_roots(config):
        for layout in (
            root / name / version / arch / binary,
            root / name / arch / version / binary,
            root / arch / name / binary,
            root / name / binary,
        ):
            if not layout.is_file():
                continue
            marker = layout.parent / f"{binary}{MARKER_NAME}"
            if marker.exists():
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
                if not _helper_marker(data, name, version, arch):
                    utils.debug("ignoring stale prebuilt %s", layout)
                    continue
                mismatch = _identity_mismatch(data, expected)
                if mismatch is not None:
                    utils.debug("ignoring prebuilt %s: %s", layout, mismatch)
                    continue
            if not _valid_helper_binary(layout):
                utils.debug("ignoring prebuilt %s: not a LoongArch64 executable", layout)
                continue
            log("using prebuilt %s from %s", name, layout)
            return HelperBuild(
                name=name,
                ok=True,
                binary=layout,
                source=f"prebuilt:{layout.parent}",
                detail="prebuilt loongarch64",
                prebuilt=True,
            )
    return None


def _valid_helper_binary(path: Path) -> bool:
    from .pack import elf_machine

    return elf_machine(path) == 258


def store_prebuilt(
    config,
    name: str,
    version: str,
    binary: Path,
    identity: Dict[str, object],
    arch: str = "loong64",
) -> Path:
    """Copy a freshly built helper into the prebuilt cache."""

    root = config.paths.cache / "prebuilts" / name / version / arch
    root.mkdir(parents=True, exist_ok=True)
    target = root / binary.name
    shutil.copy2(binary, target)
    target.chmod(0o755)
    marker = root / f"{binary.name}{MARKER_NAME}"
    payload = {
        "name": name,
        "version": version,
        "arch": arch,
        "sha256": utils.sha256_file(target),
        "size": target.stat().st_size,
        **identity,
    }
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    utils.debug("cached prebuilt %s at %s", name, target)
    return target


# --------------------------------------------------------------------------------------
# cargo environment
# --------------------------------------------------------------------------------------

def _multiarch_dir(root: Path) -> Optional[Path]:
    for candidate in (
        sysroot.LIB_SUBDIRS + ("usr/lib/loongarch64-linux-gnu",)
    ):
        path = root / candidate
        if path.is_dir():
            return path
    return None


def _normalise_openssl_include(root: Path) -> None:
    """Flatten the per-multiarch OpenSSL headers into ``usr/include/openssl``.

    Debian installs ``opensslconf.h``/``configuration.h`` under
    ``usr/include/<multiarch>/openssl``, while ``openssl-sys`` only looks at
    ``$OPENSSL_DIR/include/openssl``.
    """

    include = root / "usr" / "include" / "openssl"
    if not include.is_dir():
        return
    for extra in (root / "usr" / "include").glob("*-linux-*/openssl"):
        for header in extra.glob("*.h"):
            target = include / header.name
            if target.exists():
                continue
            try:
                shutil.copy2(header, target)
            except OSError:
                pass


class HelperBuilder:
    """Builds (or reuses) the standalone helpers for one upstream version."""

    def __init__(self, config, version: str, quality: str = "stable") -> None:
        self.config = config
        self.version = version
        self.quality = quality
        self.workdir = config.paths.natives / f"helpers-{version}"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.results: Dict[str, HelperBuild] = {}
        self.app_dir: Optional[Path] = None
        self._sources: Optional[Path] = None
        self._cargo_env: Optional[Dict[str, str]] = None
        self.prebuilt_enabled = not os.environ.get("VSCODE_LOONG64_NO_PREBUILT")

    # -- sources -----------------------------------------------------------------

    @property
    def sources(self) -> Path:
        if self._sources is None:
            self._sources = ensure_vscode_sources(
                self.version,
                self.config.paths.cache,
                self.quality,
                offline=self.config.offline,
                force=self.config.force,
            )
        return self._sources

    # -- environment -------------------------------------------------------------

    def cargo_env(self, openssl: bool) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")}
        env["CARGO_HOME"] = str(musltoolchain.cargo_home(self.config.paths.cache, self.config.offline))
        env["CARGO_NET_OFFLINE"] = "true" if self.config.offline else "false"
        env["CARGO_TARGET_DIR"] = str(self.workdir / "target")
        # ``cli/build.rs`` bakes product.json values (name, commit, tunnel
        # config) into the binary; without this the tunnel CLI would identify
        # itself as "code-oss" with an unknown commit.
        product = self.product_json()
        if product is not None:
            env["VSCODE_CLI_PRODUCT_JSON"] = str(product)
            utils.debug("stamping helpers with %s", product)
        if openssl:
            opened = self._openssl_sysroot()
            if opened is not None:
                root, libdir = opened
                env["OPENSSL_DIR"] = str(root / "usr")
                env["OPENSSL_INCLUDE_DIR"] = str(root / "usr" / "include")
                env["OPENSSL_LIB_DIR"] = str(libdir)
                env["OPENSSL_STATIC"] = "1"
                env["OPENSSL_NO_VENDOR"] = "1"
                env["ZSTD_LIB_DIR"] = str(libdir)
                env["Z_LIB_DIR"] = str(libdir)
        return env

    def _openssl_sysroot(self) -> Optional[Tuple[Path, Path]]:
        if self._cargo_env is not None and "OPENSSL_DIR" in self._cargo_env:
            return Path(self._cargo_env["OPENSSL_DIR"]).parent, Path(self._cargo_env["OPENSSL_LIB_DIR"])
        try:
            root = sysroot.provision(self.config.paths.cache / "cargo-sysroot", CARGO_SYSROOT_PACKAGES)
        except BuildError as exc:  # pragma: no cover - depends on host
            warn("unable to provision the OpenSSL sysroot: %s", exc)
            return None
        if not root.available:
            return None
        _normalise_openssl_include(root.root)
        libdir = _multiarch_dir(root.root)
        if libdir is None:
            return None
        self._cargo_env = {
            "OPENSSL_DIR": str(root.root / "usr"),
            "OPENSSL_LIB_DIR": str(libdir),
        }
        return root.root, libdir

    def rustflags(self, spec: Dict[str, object], env: Dict[str, str]) -> str:
        flags: List[str] = []
        if spec.get("static"):
            flags.append("-C target-feature=+crt-static")
        libdir = env.get("OPENSSL_LIB_DIR")
        if libdir:
            flags.extend([f"-C link-arg=-L{libdir}", "-C link-arg=-lzstd", "-C link-arg=-lz"])
        return " ".join(flags)

    # -- build -------------------------------------------------------------------

    def product_json(self) -> Optional[Path]:
        """The payload's ``product.json`` used to stamp the helper binaries.

        The path must be absolute: ``cli/build.rs`` runs with the crate
        directory as its working directory and would otherwise resolve a
        relative path against the wrong tree.
        """

        if self.app_dir is not None:
            candidate = Path(self.app_dir).resolve() / "product.json"
            if candidate.exists():
                return candidate
        return None

    def _stamp_build_script(self, crate: Path) -> None:
        """Force ``build.rs`` to run when the stamped product metadata changed.

        Cargo only re-runs a build script when it declares
        ``rerun-if-env-changed`` (``cli/build.rs`` does not) or when a source
        file changes, so switching to a new ``product.json`` would otherwise
        keep a binary that reports the previous product version.
        """

        product = self.product_json()
        if product is None:
            return
        stamp = self.workdir / f".product-{utils.sha256_file(product)[:16]}.stamp"
        marker = self.workdir / ".product.stamp"
        build_script = crate / "build.rs"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == stamp.name:
            return
        if build_script.exists():
            os.utime(build_script, None)
            utils.debug("re-running %s/build.rs for the new product metadata", crate.name)
        marker.write_text(stamp.name + "\n", encoding="utf-8")

    def build_one(self, name: str) -> HelperBuild:
        spec = RUST_HELPERS.get(name)
        if spec is None:
            return HelperBuild(name=name, ok=False, detail="no build recipe")
        identity = recipe_identity(self.config, self.app_dir, str(spec["crate_dir"]))
        if self.prebuilt_enabled:
            found = find_prebuilt(
                self.config, name, self.version, str(spec["binary"]), expected=identity
            )
            if found is not None:
                return found
        crate = self.sources / str(spec["crate_dir"])
        manifest = crate / "Cargo.toml"
        if not manifest.exists():
            return HelperBuild(name=name, ok=False, detail=f"{manifest} is missing from the sources")
        patch_report = patches.apply(crate, str(spec["crate_dir"]))
        patches.require_applied(patch_report)
        if any(entry.get("status") == "applied" for entry in patch_report):
            log(
                "applied %d source patch(es) to %s",
                sum(1 for e in patch_report if e.get("status") == "applied"),
                name,
            )
        if self.config.offline and not (self.config.paths.cache / "cargo").exists():
            return HelperBuild(name=name, ok=False, detail="cargo registry cache is missing for an offline build")
        self._stamp_build_script(crate)
        target = str(spec["target"])
        env = self.cargo_env(bool(spec.get("openssl")))
        env["RUSTFLAGS"] = self.rustflags(spec, env)
        cmd = [
            "cargo",
            "build",
            "--release",
            "--target",
            target,
            "--manifest-path",
            str(manifest),
            "--bin",
            str(spec["binary"]),
        ]
        log("building %s for %s (static=%s)", name, target, bool(spec.get("static")))
        proc = utils.run(cmd, cwd=crate, env=env, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        log_path = self.config.paths.logs / f"helper-{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8", errors="replace")
        binary = self.workdir / "target" / target / "release" / str(spec["binary"])
        if proc.returncode != 0 or not binary.exists():
            tail = output.strip().splitlines()[-3:] if output.strip() else []
            detail = "; ".join(tail)[:400] or "cargo build failed"
            return HelperBuild(name=name, ok=False, detail=detail, log_path=log_path, source=str(crate))
        if not _valid_helper_binary(binary):
            return HelperBuild(
                name=name,
                ok=False,
                detail="cargo produced a non-LoongArch binary",
                log_path=log_path,
                source=str(crate),
            )
        problems: List[str] = []
        if spec.get("static"):
            problems = musltoolchain.static_problems(binary)
        if problems:
            # A dynamic build is still usable: the package depends on glibc anyway.
            warn("%s is not fully static (%s); keeping the dynamic build", name, "; ".join(problems))
        self._smoke_test(binary, name)
        log("built %s (%s, %s)", name, target, human_size(binary.stat().st_size))
        try:
            store_prebuilt(
                self.config,
                name,
                self.version,
                binary,
                {**identity, "target": target, "static": spec.get("static"), "source": "built"},
            )
        except OSError as exc:  # pragma: no cover - cache is best effort
            utils.debug("unable to cache %s: %s", name, exc)
        return HelperBuild(
            name=name,
            ok=True,
            binary=binary,
            source=str(crate),
            detail=f"{target}{' static' if spec.get('static') else ''}",
            log_path=log_path,
        )

    def _smoke_test(self, binary: Path, name: str) -> None:
        """Run ``--version`` so a broken binary never reaches the package."""

        args = ["--version"] if name == "code-tunnel" else []
        proc = utils.run([str(binary), *args], check=False, quiet=True)
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if name == "code-tunnel" and self.version not in output:
            warn("code-tunnel reports %r instead of %s", output.splitlines()[:1], self.version)
        utils.debug("smoke test %s: %s", name, output.splitlines()[:1])

    def build_seccomp(self, app_dir: Path) -> HelperBuild:
        identity = recipe_identity(self.config, self.app_dir, "apply-seccomp")
        if self.prebuilt_enabled:
            found = find_prebuilt(
                self.config,
                "apply-seccomp",
                self.version,
                "apply-seccomp",
                expected=identity,
            )
            if found is not None:
                return found
        sources = seccomp.find_sources(app_dir)
        if sources is None:
            return HelperBuild(name="apply-seccomp", ok=False, detail="vendor/seccomp-src is missing")
        result = seccomp.compile_apply_seccomp(
            sources,
            self.workdir,
            self.config.paths.logs,
        )
        build = HelperBuild(
            name="apply-seccomp",
            ok=result.ok,
            binary=result.binary,
            source=result.source,
            detail=result.detail or seccomp.describe_binary(result.binary) if result.binary else result.detail,
            log_path=result.log_path,
        )
        if build.ok and build.binary:
            try:
                store_prebuilt(
                    self.config,
                    "apply-seccomp",
                    self.version,
                    build.binary,
                    {**identity, "target": "loongarch64-linux-gnu", "static": True, "source": "built"},
                )
            except OSError as exc:  # pragma: no cover
                utils.debug("unable to cache apply-seccomp: %s", exc)
        return build

    def build_dotnet(self, name: str) -> HelperBuild:
        """Build a .NET NativeAOT helper from ``src-tools/<crate>``.

        Prebuilt resolution happens inside :mod:`vscode_loong64.dotnet` because
        the expected identity (recipe + SDK) is part of the lookup.
        """

        from . import dotnet

        if not self.config.dotnet_download:
            found = find_prebuilt(
                self.config, name, self.version, name, expected=recipe_identity(self.config, None, name)
            )
            if found is not None:
                return found
        return dotnet.build(
            self.config,
            self.workdir,
            self.version,
            name,
            allow_download=self.config.dotnet_download,
        )

    def build_all(self, app_dir: Path, names: Sequence[str] = ()) -> Dict[str, HelperBuild]:
        self.app_dir = Path(app_dir)
        wanted = list(names or STANDALONE_HELPERS.keys())
        for name in wanted:
            spec = STANDALONE_HELPERS.get(name)
            if spec is None:
                warn("unknown helper %s", name)
                continue
            kind = spec["kind"]
            if kind == "rust":
                result = self.build_one(name)
            elif kind == "seccomp":
                result = self.build_seccomp(app_dir)
            elif kind == "dotnet":
                result = self.build_dotnet(name)
            else:  # pragma: no cover - config guard
                result = HelperBuild(name=name, ok=False, detail=f"unsupported kind {kind}")
            self.results[name] = result
            if not result.ok:
                warn("helper %s is unavailable: %s", name, result.detail)
        return self.results

    def artifacts(self) -> Dict[str, Path]:
        return {name: result.binary for name, result in self.results.items() if result.ok and result.binary}


def install(stage, helpers: Dict[str, Path]) -> List[Dict[str, object]]:
    """Copy the helpers into the staged tree at their upstream paths."""

    installed: List[Dict[str, object]] = []
    for name, spec in STANDALONE_HELPERS.items():
        source = helpers.get(name)
        already = Path(getattr(stage, "root", ".")) / str(spec["install"])
        if source is None or not Path(source).exists():
            if already.exists() and _valid_helper_binary(already):
                installed.append(
                    {"name": name, "status": "kept", "path": str(spec["install"]), "detail": "already staged"}
                )
            else:
                installed.append({"name": name, "status": "missing", "detail": "not built"})
            continue
        relative = str(spec["install"])
        target = stage.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        shutil.copy2(source, target)
        target.chmod(0o755)
        extra: List[str] = []
        if name == "apply-seccomp":
            # the JS lookup only knows x64/arm64: teach it the loong64 directory
            app_dir = Path(getattr(stage, "app_dir", Path(getattr(stage, "root", ".")) / "resources" / "app"))
            extra = [
                str(path.relative_to(stage.root))
                for path in seccomp.install(app_dir, target, "loong64")
                if Path(path) != target
            ]
        from .pack import arch_label

        log("installed helper %s -> %s (%s)", name, relative, arch_label(target))
        entry: Dict[str, object] = {
            "name": name,
            "status": "built",
            "path": relative,
            "arch": arch_label(target),
            "detail": spec.get("description", ""),
        }
        if extra:
            entry["patched"] = extra
        installed.append(entry)
    stage.native_report["helpers"] = installed
    return installed
