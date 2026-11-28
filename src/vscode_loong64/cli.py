"""Command line interface for the VSCode LoongArch64 builder."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, artifacts, electron, pack, publish, upstream, utils
from .config import PROJECT_ROOT, BuildConfig, Paths, ensure_tools, is_loong64_host, missing_tools
from .pipeline import Pipeline, build
from .utils import BuildError, human_size, log, section, warn

EPILOG = """examples:
  # build the latest stable release for loong64
  vscode-loong64 build

  # build a specific version into ./dist
  vscode-loong64 build --version 1.148.0 --formats tar.gz,deb

  # rebuild from archives that are already on disk (offline)
  vscode-loong64 build --offline --upstream ./code-1.148.0.tar.gz \\
      --electron-archive ~/.cache/electron-loong64/downloads/electron-v43.7.7-linux-loong64.zip

  # inspect the environment
  vscode-loong64 doctor

  # publish everything described by dist/manifest.json
  vscode-loong64 publish --repo me/vscode-loong64

  # build the proxy addon from a local Microsoft source checkout
  vscode-loong64 build --os-proxy-src ../os-proxy-resolver --formats deb
"""


# --------------------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vscode-loong64",
        description="Build upstream Visual Studio Code for the LoongArch (loong64) architecture.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug output")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="project root (default: %(default)s)")
    sub = parser.add_subparsers(dest="command")

    # -- build -------------------------------------------------------------------
    build_cmd = sub.add_parser("build", help="build a loong64 release")
    build_cmd.add_argument("version", nargs="?", default="latest", help="upstream version or 'latest'")
    build_cmd.add_argument("--quality", default="stable", choices=("stable", "insider"))
    build_cmd.add_argument("--formats", default="tar.gz,deb", help="comma separated: tar.gz,deb,rpm,zip")
    build_cmd.add_argument("--source-platform", default="linux-x64", choices=("linux-x64", "linux-arm64", "linux-armhf"))
    build_cmd.add_argument("--source-package", default="tar.gz", choices=("tar.gz", "deb", "rpm"))
    build_cmd.add_argument("--app-name", default="Code")
    build_cmd.add_argument("--app-name-long", default="Visual Studio Code")
    build_cmd.add_argument("--binary-name", default="code")
    build_cmd.add_argument("--artifact-suffix", default="loong64")
    build_cmd.add_argument("--electron-version", default=None, help="override the detected Electron version")
    build_cmd.add_argument("--electron-archive", type=Path, default=None, help="use a local loong64 Electron zip")
    build_cmd.add_argument("--upstream", type=Path, default=None, help="use a local official VSCode archive")
    build_cmd.add_argument("--upstream-dir", type=Path, default=None, help="use an already extracted VSCode tree")
    build_cmd.add_argument("--ripgrep", type=Path, default=None, help="prebuilt static LoongArch/musl ripgrep")
    build_cmd.add_argument("--os-proxy-src", type=Path, default=None, help="checkout of microsoft/os-proxy-resolver")
    build_cmd.add_argument("--os-proxy-features", default="pac-engine", help="PAC backend feature for the proxy addon (pac-engine or wasm2c)")
    build_cmd.add_argument("--no-natives", action="store_true", help="do not rebuild native modules")
    build_cmd.add_argument("--no-helpers", action="store_true", help="do not rebuild bin/code-tunnel, the copilot shim, apply-seccomp or vsce-sign")
    build_cmd.add_argument(
        "--prebuilt",
        type=Path,
        action="append",
        default=None,
        help="directory with prebuilt helpers (repeatable, searched before compiling)",
    )
    build_cmd.add_argument(
        "--no-prebuilt",
        action="store_true",
        help="ignore the prebuilt cache and compile every helper from source",
    )
    build_cmd.add_argument(
        "--no-dotnet-download",
        action="store_true",
        help="never download the LoongArch .NET SDK (vsce-sign then stays x64)",
    )
    build_cmd.add_argument(
        "--prebuilt-bundle",
        type=Path,
        default=None,
        help="tarball with the LoongArch addons/helpers of a previous build "
        "(skips every compile step, for x64 runners)",
    )
    build_cmd.add_argument(
        "--emit-prebuilt-bundle",
        action="store_true",
        help="write dist/loong64-prebuilts-<version>-electron<version>.tar.gz after the build",
    )
    build_cmd.add_argument("--no-electron-fallback", action="store_true", help="fail when the exact Electron build is missing")
    build_cmd.add_argument("--no-smoke-test", action="store_true", help="skip running the built application")
    build_cmd.add_argument("--offline", action="store_true", help="never touch the network")
    build_cmd.add_argument("--force", action="store_true", help="ignore caches")
    build_cmd.add_argument("-j", "--jobs", type=int, default=0)
    build_cmd.add_argument("--clean", action="store_true", help="remove work/ and dist/ before building")

    # -- doctor ------------------------------------------------------------------
    doctor = sub.add_parser("doctor", help="check the build environment")
    doctor.add_argument("--json", action="store_true", help="machine readable output")

    # -- info --------------------------------------------------------------------
    info = sub.add_parser("info", help="show upstream release information")
    info.add_argument("version", nargs="?", default="latest")
    info.add_argument("--quality", default="stable", choices=("stable", "insider"))
    info.add_argument("--list", action="store_true", help="list recent versions")

    electron_cmd = sub.add_parser("electron", help="manage the loong64 Electron runtime cache")
    electron_cmd.add_argument("action", choices=("list", "fetch", "verify", "import"))
    electron_cmd.add_argument("version", nargs="?", default=None)
    electron_cmd.add_argument("--archive", type=Path, default=None, help="local zip for 'import'")
    electron_cmd.add_argument("--offline", action="store_true")
    electron_cmd.add_argument("--json", action="store_true")

    # -- package -----------------------------------------------------------------
    package_cmd = sub.add_parser("package", help="pack an already staged tree")
    package_cmd.add_argument("stage", type=Path, help="staged application directory")
    package_cmd.add_argument("version", help="version used in artifact names")
    package_cmd.add_argument("--formats", default="tar.gz,deb")
    package_cmd.add_argument("--out", type=Path, default=None)

    # -- publish -----------------------------------------------------------------
    publish_cmd = sub.add_parser("publish", help="upload artifacts to a GitHub release")
    publish_cmd.add_argument("--manifest", type=Path, default=None, help="dist/manifest.json")
    publish_cmd.add_argument("--repo", default=None, help="owner/name (default: $GITHUB_REPOSITORY)")
    publish_cmd.add_argument("--tag", default=None)
    publish_cmd.add_argument("--token", default=None)
    publish_cmd.add_argument("--draft", action="store_true")
    publish_cmd.add_argument("--prerelease", action="store_true")
    publish_cmd.add_argument("files", nargs="*", type=Path)

    # -- prebuilts ---------------------------------------------------------------
    prebuilts_cmd = sub.add_parser(
        "prebuilts", help="inspect and fetch the portable LoongArch payload bundle"
    )
    prebuilts_cmd.add_argument("action", choices=("inspect", "pack", "unpack", "fetch"))
    prebuilts_cmd.add_argument("archive", nargs="?", type=Path, default=None)
    prebuilts_cmd.add_argument("--version", dest="bundle_version", default=None)
    prebuilts_cmd.add_argument("--electron", default=None)
    prebuilts_cmd.add_argument("--repo", default=None, help="owner/name holding the bundle release")
    prebuilts_cmd.add_argument("--tag", default=None)
    prebuilts_cmd.add_argument("--dest", type=Path, default=None)
    prebuilts_cmd.add_argument("--json", action="store_true")

    # -- check -------------------------------------------------------------------
    check = sub.add_parser("should-build", help="exit 0 when a new build is required")
    check.add_argument("version", nargs="?", default="latest")
    check.add_argument("--published", default="", help="comma separated list of published tags")
    check.add_argument("--force", action="store_true")

    return parser


def configure_logging(args: argparse.Namespace) -> None:
    if getattr(args, "verbose", False):
        os.environ["VSCODE_LOONG64_LOG"] = "debug"
    if getattr(args, "quiet", False):
        os.environ["VSCODE_LOONG64_QUIET"] = "1"
    # logging level is read at import time, so re-apply it explicitly
    utils._LEVEL = {"debug": 10, "info": 20, "warn": 30, "error": 40}[
        "debug" if getattr(args, "verbose", False) else "info"
    ]
    utils._QUIET = bool(getattr(args, "quiet", False))


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    paths = Paths.create(args.root)
    if args.prebuilt_bundle:
        # A bundle supplies every architecture specific payload: the pipeline
        # returns the bundled ones before reaching any compiler, and the .NET
        # SDK download would only be dead weight on an x64 runner.
        args.no_dotnet_download = True
    if args.clean:
        for directory in (paths.work, paths.dist):
            if directory.exists():
                log("removing %s", directory)
                shutil.rmtree(directory, ignore_errors=True)
        paths = Paths.create(args.root)
    if args.no_prebuilt:
        os.environ["VSCODE_LOONG64_NO_PREBUILT"] = "1"
    config = BuildConfig(
        version=args.version,
        quality=args.quality,
        paths=paths,
        source_platform=args.source_platform,
        source_package=args.source_package,
        rebuild_natives=not args.no_natives,
        allow_electron_fallback=not args.no_electron_fallback,
        app_name=args.app_name,
        app_name_long=args.app_name_long,
        binary_name=args.binary_name,
        artifact_suffix=args.artifact_suffix,
        formats=[f.strip() for f in args.formats.split(",") if f.strip()],
        offline=args.offline,
        force=args.force,
        jobs=args.jobs,
        electron_archive=args.electron_archive,
        upstream_archive=args.upstream,
        upstream_dir=args.upstream_dir,
        electron_version=args.electron_version,
        ripgrep_binary=args.ripgrep,
        os_proxy_resolver_source=args.os_proxy_src,
        os_proxy_features=args.os_proxy_features,
        rebuild_helpers=not args.no_helpers,
        prebuilt_dirs=list(args.prebuilt or []),
        dotnet_download=not args.no_dotnet_download,
        prebuilt_bundle=args.prebuilt_bundle,
        emit_prebuilt_bundle=args.emit_prebuilt_bundle,
    )
    result = build(config, verify_app=not args.no_smoke_test)
    if result.manifest_path:
        print(json.dumps({"manifest": str(result.manifest_path), "artifacts": [a.as_dict() for a in result.artifacts]}, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    section("Environment")
    for key, value in report["environment"].items():
        print(f"  {key:<18} {value}")
    section("Tools")
    for tool in report["tools"]:
        mark = "ok " if tool["found"] else "MISSING"
        suffix = "" if tool["required"] else " (optional)"
        print(f"  [{mark}] {tool['name']}{suffix}: {tool['path'] or tool['hint']}")
    section("Electron cache")
    for entry in report["electron_cache"]:
        print(f"  {entry['path']} ({human_size(entry['size'])}, version {entry.get('version', '?')})")
    if not report["electron_cache"]:
        print("  (empty)")
    if report["problems"]:
        section("Problems")
        for problem in report["problems"]:
            print(f"  - {problem}")
    return 0 if report["ok"] else 1


def doctor_report() -> dict:
    import platform

    tools = [
        ("git", True, "version control"),
        ("tar", True, "archive handling"),
        ("unzip", True, "zip extraction"),
        ("ar", True, "deb packaging (binutils)"),
        ("xar", False, ""),
        ("node", True, "native module builds"),
        ("npm", True, "native module builds"),
        ("make", True, "native module builds"),
        ("g++", False, "C/C++ native module builds on a LoongArch host"),
        ("python3", True, "node-gyp"),
        ("cargo", True, "static Rust proxy resolver and ripgrep"),
        ("rustc", True, "static Rust proxy resolver and ripgrep"),
        ("loongarch64-linux-musl-gcc", False, "static musl linking (optional: the Rust musl target is self-contained)"),
        ("pkg-config", False, "native-keymap"),
        ("file", False, "diagnostics"),
        ("zstd", False, "deb extraction"),
        ("fpm", False, "electron-builder deb target"),
        ("rpmbuild", False, "rpm packaging"),
        ("gh", False, "publishing"),
    ]
    entries = []
    problems = []
    for name, required, hint in tools:
        path = shutil.which(name)
        entries.append({"name": name, "found": bool(path), "path": path, "required": required, "hint": hint})
        if required and not path:
            if name in ("node", "npm", "make", "python3", "cargo", "rustc"):
                problems.append(f"{name} is missing: native modules cannot be rebuilt (use --no-natives)")
            else:
                problems.append(f"{name} is missing: {hint}")
    environment = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "loong64 host": "yes" if is_loong64_host() else "no",
        "python": platform.python_version(),
        "cwd": os.getcwd(),
    }
    return {
        "ok": not problems,
        "environment": environment,
        "tools": entries,
        "problems": problems,
        "electron_cache": electron.describe_cache(),
    }


def cmd_info(args: argparse.Namespace) -> int:
    if args.list:
        versions = upstream.list_releases(args.quality, limit=20)
        print("\n".join(versions))
        return 0
    release = upstream.resolve_version(args.version, quality=args.quality)
    print(json.dumps(release.as_dict(), indent=2))
    return 0


def cmd_electron(args: argparse.Namespace) -> int:
    if args.action == "list":
        entries = electron.describe_cache()
        if args.json:
            print(json.dumps(entries, indent=2, default=str))
            return 0
        if not entries:
            print("electron cache is empty: " + str(electron.cache_dir()))
            return 0
        for entry in entries:
            print(f"{entry.get('version', '?'):>10}  {human_size(entry['size']):>10}  {entry['path']}")
        return 0
    if args.action == "fetch":
        version = args.version or "latest"
        if version == "latest":
            required = upstream.latest_release().product_version
            # the mirror tracks Electron versions, not VSCode ones
            version = args.version
        if not args.version:
            raise BuildError("electron fetch requires an explicit Electron version")
        runtime = electron.fetch(args.version, offline=args.offline)
        print(json.dumps({"version": runtime.version, "root": str(runtime.root)}, indent=2))
        return 0
    if args.action == "verify":
        version = args.version
        if not version:
            cached = electron.cached_versions()
            if not cached:
                raise BuildError("no cached Electron runtime; pass a version")
            version = cached[0]
        root = electron.find_cached(version)
        if not root:
            raise BuildError(f"Electron {version} is not cached")
        runtime = electron.ElectronRuntime(version=version, arch="loong64", root=root, binary=root / "electron")
        info = electron.verify(runtime)
        print(json.dumps(info, indent=2))
        return 0
    if args.action == "import":
        if not args.archive or not args.version:
            raise BuildError("electron import requires --archive and a version")
        runtime = electron.import_local(args.archive, args.version, force=True)
        print(json.dumps({"version": runtime.version, "root": str(runtime.root)}, indent=2))
        return 0
    raise BuildError(f"unknown electron action: {args.action}")


def cmd_package(args: argparse.Namespace) -> int:
    stage_root = args.stage.expanduser().resolve()
    if not stage_root.is_dir():
        raise BuildError(f"{stage_root} is not a directory")
    paths = Paths.create(PROJECT_ROOT)
    config = BuildConfig(version=args.version, paths=paths, formats=[f.strip() for f in args.formats.split(",") if f.strip()])
    produced = artifacts.build_all(stage_root, config, dist=args.out)
    manifest_path = (args.out or paths.dist) / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}
        by_name = {str(entry.get("name")): entry for entry in manifest.get("artifacts") or []}
        for artifact in produced:
            by_name[artifact.path.name] = artifact.as_dict()
        manifest["artifacts"] = list(by_name.values())
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([a.as_dict() for a in produced], indent=2))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    manifest = args.manifest or (Paths.create(PROJECT_ROOT).dist / "manifest.json")
    if not manifest.exists():
        raise BuildError(f"manifest not found: {manifest}")
    names = publish.publish(
        manifest,
        repo=args.repo,
        tag=args.tag,
        token=args.token,
        draft=args.draft,
        prerelease=args.prerelease,
        files=[f.expanduser().resolve() for f in args.files] or None,
    )
    print(json.dumps({"uploaded": names}, indent=2))
    return 0


def cmd_prebuilts(args: argparse.Namespace) -> int:
    from . import prebuilts

    if args.action == "inspect":
        if not args.archive:
            raise BuildError("prebuilts inspect requires an archive")
        print(json.dumps(prebuilts.describe(args.archive), indent=2))
        return 0
    if args.action == "unpack":
        if not args.archive:
            raise BuildError("prebuilts unpack requires an archive")
        dest = args.dest or (Paths.create(PROJECT_ROOT).work / "bundle-current")
        bundle = prebuilts.unpack(args.archive, dest)
        print(json.dumps({"root": str(bundle.root), "helpers": [str(p) for p in bundle.helpers.values()],
                          "natives": sorted(bundle.natives)}, indent=2))
        return 0
    if args.action == "pack":
        raise BuildError(
            "the bundle is written by 'build --emit-prebuilt-bundle' because it needs "
            "the compiled payloads; run that on a LoongArch host"
        )
    if args.action == "fetch":
        if not args.version:
            raise BuildError("prebuilts fetch requires --version")
        runtime = args.electron or "auto"
        path = prebuilts.fetch(args.version, runtime, repo=args.repo, tag=args.tag, dest=args.dest)
        print(json.dumps({"archive": str(path)}, indent=2))
        return 0
    raise BuildError(f"unknown prebuilts action: {args.action}")


def cmd_should_build(args: argparse.Namespace) -> int:
    release = upstream.resolve_version(args.version)
    published = [t for t in (args.published or "").split(",") if t.strip()]
    decision = upstream.should_build(release, published, force=args.force)
    print(json.dumps({"version": release.version, "tag": release.tag, "build": decision}, indent=2))
    return 0 if decision else 1


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------

COMMANDS = {
    "build": cmd_build,
    "doctor": cmd_doctor,
    "info": cmd_info,
    "electron": cmd_electron,
    "package": cmd_package,
    "publish": cmd_publish,
    "should-build": cmd_should_build,
    "prebuilts": cmd_prebuilts,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    try:
        return handler(args)
    except BuildError as exc:
        utils.log(str(exc), level="error")
        return 1
    except KeyboardInterrupt:
        utils.log("interrupted", level="error")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
