"""Create installable artifacts (tar.gz, deb, rpm) from a staged build."""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import DEB_ARCH, RPM_ARCH, BuildConfig, Paths
from .utils import BuildError, human_size, log, run, sha256_file, warn


@dataclass
class Artifact:
    path: Path
    fmt: str
    sha256: str = ""
    size: int = 0

    def finalise(self) -> "Artifact":
        self.size = self.path.stat().st_size if self.path.exists() else 0
        if self.path.exists():
            self.sha256 = sha256_file(self.path)
        return self

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.path.name,
            "path": str(self.path),
            "format": self.fmt,
            "size": self.size,
            "sha256": self.sha256,
        }


# --------------------------------------------------------------------------------------
# tar.gz
# --------------------------------------------------------------------------------------

def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    # keep permissions but normalise ownership for reproducibility
    info.mtime = int(info.mtime)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    return info


def create_tarball(stage_root: Path, dest: Path, top_dir: str) -> Artifact:
    """Pack ``stage_root`` into ``dest`` using ``top_dir`` as the archive root."""

    dest.parent.mkdir(parents=True, exist_ok=True)
    log("creating %s", dest.name)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tarfile.open(
        tmp,
        "w:gz",
        compresslevel=6,
        format=tarfile.GNU_FORMAT,
    ) as tf:
        for entry in sorted(stage_root.iterdir(), key=lambda p: p.name):
            tf.add(entry, arcname=f"{top_dir}/{entry.name}", recursive=True, filter=_tar_filter)
    tmp.replace(dest)
    return Artifact(path=dest, fmt="tar.gz").finalise()


# --------------------------------------------------------------------------------------
# deb
# --------------------------------------------------------------------------------------

DEB_CONTROL_TEMPLATE = """Package: {package}
Version: {version}
Section: devel
Priority: optional
Architecture: {arch}
Maintainer: {maintainer}
Installed-Size: {installed_size}
Depends: {depends}
Recommends: {recommends}
Homepage: {homepage}
Description: {description}
 {long_description}
"""

DEB_POSTINST = """#!/bin/bash
set -e

# LoongArch builds ship their own Electron runtime, so only the file
# associations and the CLI symlink have to be refreshed.
if [ -f /usr/share/{binary}/resources/app/resources/linux/code.png ]; then
	rm -f /usr/share/pixmaps/{binary}.png
	ln -sf /usr/share/{binary}/resources/app/resources/linux/code.png /usr/share/pixmaps/{binary}.png
	update-alternatives --install /usr/bin/{binary} {binary} /usr/share/{binary}/bin/{binary} 100 >/dev/null 2>&1 || true
fi

if command -v update-mime-database >/dev/null 2>&1; then
	update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
	update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
exit 0
"""

DEB_POSTRM = """#!/bin/bash
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
	rm -f /usr/share/pixmaps/{binary}.png
	update-alternatives --remove {binary} /usr/share/{binary}/bin/{binary} >/dev/null 2>&1 || true
fi
exit 0
"""

DESKTOP_TEMPLATE = """[Desktop Entry]
Name={name}
Comment=Code Editing. Redefined.
GenericName=Text Editor
Exec=/usr/share/{binary}/{binary} %F
Icon={binary}
Type=Application
StartupNotify=false
StartupWMClass=Code
Categories=TextEditor;Development;IDE;
MimeType=application/x-{binary}-workspace;
Actions=new-empty-window;
Keywords=vscode;

[Desktop Action new-empty-window]
Name=New Empty Window
Exec=/usr/share/{binary}/{binary} --new-window %F
Icon={binary}
"""

DEFAULT_DEPENDS = ",".join(
    [
        "ca-certificates",
        "libasound2",
        "libatk-bridge2.0-0",
        "libatk1.0-0",
        "libatspi2.0-0",
        "libc6",
        "libcairo2",
        "libcups2",
        "libdbus-1-3",
        "libdrm2",
        "libexpat1",
        "libgbm1",
        "libglib2.0-0",
        "libgtk-3-0",
        "libstdc++6",
        "libnspr4",
        "libnss3",
        "libpango-1.0-0",
        "libudev1",
        "libx11-6",
        "libxcb1",
        "libxcomposite1",
        "libxdamage1",
        "libxext6",
        "libxfixes3",
        "libxkbcommon0",
        "libxkbfile1",
        "libxrandr2",
        "xdg-utils",
    ]
)

DEFAULT_RECOMMENDS = "libvulkan1,bubblewrap,socat"


def _dir_size(path: Path) -> int:
    total = 0
    seen = set()
    for root, _dirs, files in os.walk(path):
        for name in files:
            candidate = Path(root) / name
            try:
                info = candidate.lstat()
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += info.st_size
            except OSError:
                continue
    return total


def _tar_data_dir(root: Path, dest: Path) -> None:
    # dpkg's tar reader on some distributions does not understand PAX extended
    # headers.  GNU format also carries long paths without emitting type 'x'.
    with tarfile.open(
        dest,
        "w:xz",
        preset=6,
        format=tarfile.GNU_FORMAT,
    ) as tf:
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            tf.add(entry, arcname=f"./{entry.name}", recursive=True, filter=_tar_filter)


def _copy_tree_preserving_links(src: Path, dst: Path) -> None:
    """Copy a tree while preserving existing hardlink relationships."""

    first_link: Dict[Tuple[int, int], Path] = {}
    for current, dirs, files in os.walk(src, topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(src)
        target_dir = dst / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        dirs[:] = sorted(dirs)
        for name in dirs:
            source = current_path / name
            target = target_dir / name
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
        for name in sorted(files):
            source = current_path / name
            target = target_dir / name
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
                continue
            stat = source.stat()
            key = (stat.st_dev, stat.st_ino)
            previous = first_link.get(key)
            if previous is not None and stat.st_nlink > 1:
                os.link(previous, target)
            else:
                shutil.copy2(source, target)
                first_link[key] = target
    for current, dirs, _files in os.walk(src, topdown=False, followlinks=False):
        source = Path(current)
        target = dst / source.relative_to(src)
        try:
            shutil.copystat(source, target, follow_symlinks=False)
        except OSError:
            pass


def create_deb(
    stage_root: Path,
    dest: Path,
    config: BuildConfig,
    depends: str = DEFAULT_DEPENDS,
    recommends: str = DEFAULT_RECOMMENDS,
    maintainer: str = "vscode-loong64 <noreply@localhost>",
    description: str = "Visual Studio Code for LoongArch64",
) -> Artifact:
    """Build a Debian package with ``dpkg-deb`` or system ``ar``/``tar`` tools."""

    if not shutil.which("dpkg-deb") and not shutil.which("ar"):
        raise BuildError("`dpkg-deb` or `ar` is required to build .deb packages")
    log("creating %s", dest.name)
    binary = config.binary_name
    work = dest.parent / (dest.name + ".work")
    if work.exists():
        shutil.rmtree(work)
    package_root = work / "package"
    (package_root / "DEBIAN").mkdir(parents=True)
    payload = package_root

    # /usr/share/<binary>
    share_dir = payload / "usr" / "share" / binary
    _copy_tree_preserving_links(stage_root, share_dir)

    # /usr/bin symlink
    bin_dir = payload / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / binary).symlink_to(f"../share/{binary}/bin/{binary}")

    # desktop entry + icon
    applications = payload / "usr" / "share" / "applications"
    applications.mkdir(parents=True)
    (applications / f"{binary}.desktop").write_text(
        DESKTOP_TEMPLATE.format(name=config.app_name_long, binary=binary), encoding="utf-8"
    )
    pixmaps = payload / "usr" / "share" / "pixmaps"
    pixmaps.mkdir(parents=True)
    icon = stage_root / "resources" / "app" / "resources" / "linux" / "code.png"
    if icon.exists():
        shutil.copy2(icon, pixmaps / f"{binary}.png")

    installed_size = max(1, _dir_size(payload) // 1024)
    control = DEB_CONTROL_TEMPLATE.format(
        package=binary,
        version=_deb_version(config.version),
        arch=DEB_ARCH,
        maintainer=maintainer,
        installed_size=installed_size,
        depends=depends,
        recommends=recommends,
        homepage="https://code.visualstudio.com/",
        description=description,
        long_description="\n ".join(
            [
                "Visual Studio Code is a code editor redefined and optimized for building and",
                "debugging modern web and cloud applications. This package contains the",
                "LoongArch64 (loong64) build generated from the official Microsoft binaries.",
            ]
        ),
    )
    (package_root / "DEBIAN" / "control").write_text(control, encoding="utf-8")
    for script, body in (("postinst", DEB_POSTINST), ("postrm", DEB_POSTRM)):
        path = package_root / "DEBIAN" / script
        path.write_text(body.format(binary=binary), encoding="utf-8")
        path.chmod(0o755)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    if shutil.which("dpkg-deb"):
        # Let dpkg-deb construct and compress the archives.  Its output is
        # accepted by the same dpkg version that will install the package.
        run(
            [
                "dpkg-deb",
                "--root-owner-group",
                "-Zxz",
                "-z6",
                "--build",
                str(package_root),
                str(tmp),
            ]
        )
    else:
        data_tar = work / "data.tar.xz"
        _tar_data_dir(payload, data_tar)
        control_tar = work / "control.tar.gz"
        with tarfile.open(control_tar, "w:gz", format=tarfile.GNU_FORMAT) as tf:
            for entry in sorted((package_root / "DEBIAN").iterdir()):
                tf.add(entry, arcname=f"./{entry.name}", filter=_tar_filter)
        control_binary = work / "debian-binary"
        control_binary.write_text("2.0\n", encoding="utf-8")
        run(
            [
                "ar",
                "rc",
                str(tmp.absolute()),
                str(control_binary.absolute()),
                str(control_tar.absolute()),
                str(data_tar.absolute()),
            ]
        )
    tmp.replace(dest)
    shutil.rmtree(work, ignore_errors=True)
    return Artifact(path=dest, fmt="deb").finalise()


def _deb_version(version: str) -> str:
    # debian versions must not contain '-' except for the revision separator
    cleaned = version.replace("-", "~")
    return cleaned if "~" in cleaned else f"{cleaned}"


# --------------------------------------------------------------------------------------
# rpm
# --------------------------------------------------------------------------------------

def create_rpm(stage_root: Path, dest: Path, config: BuildConfig) -> Artifact:
    """Build an RPM package using ``rpmbuild`` when it is available."""

    if not shutil.which("rpmbuild"):
        raise BuildError("`rpmbuild` is required to build .rpm packages (apt install rpm)")
    log("creating %s", dest.name)
    binary = config.binary_name
    top = dest.parent / "rpmbuild"
    for sub in ("BUILD", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (top / sub).mkdir(parents=True, exist_ok=True)
    source_tar = top / "SOURCES" / f"{binary}-{config.version}.tar.gz"
    with tarfile.open(source_tar, "w:gz") as tf:
        for entry in sorted(stage_root.iterdir(), key=lambda p: p.name):
            tf.add(entry, arcname=f"{binary}-{config.version}/{entry.name}")
    spec = top / "SPECS" / f"{binary}.spec"
    spec.write_text(
        f"""Name: {binary}
Version: {config.version}
Release: 1
Summary: {config.app_name_long} for LoongArch64
License: MIT
URL: https://code.visualstudio.com/
Source0: {source_tar.name}
BuildArch: {RPM_ARCH}
Requires: libxkbcommon, libxkbfile, libgtk-3, libnss3, libasound, libgbm, xdg-utils

%description
{config.app_name_long} built from the official Microsoft binaries for LoongArch64.

%prep
%setup -q

%build
# binaries are prebuilt

%install
mkdir -p %{{buildroot}}/usr/share/{binary}
cp -a * %{{buildroot}}/usr/share/{binary}/

%files
/usr/share/{binary}

%changelog
* {time.strftime('%a %b %d %Y')} vscode-loong64 <noreply@localhost> - {config.version}-1
- LoongArch64 build
""",
        encoding="utf-8",
    )
    run(["rpmbuild", "--define", f"_topdir {top}", "-bb", str(spec)], capture=True)
    built = list((top / "RPMS").rglob("*.rpm"))
    if not built:
        raise BuildError("rpmbuild produced no package")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built[0], dest)
    shutil.rmtree(top, ignore_errors=True)
    return Artifact(path=dest, fmt="rpm").finalise()


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------

def build_all(stage_root: Path, config: BuildConfig, dist: Optional[Path] = None) -> List[Artifact]:
    """Produce every requested artifact for ``stage_root``."""

    dist = Path(dist or config.paths.dist)
    dist.mkdir(parents=True, exist_ok=True)
    artifacts: List[Artifact] = []
    for fmt in config.formats:
        dest = dist / config.artifact_name(fmt)
        if fmt == "tar.gz":
            artifacts.append(create_tarball(stage_root, dest, f"VSCode-linux-{config.artifact_suffix}"))
        elif fmt == "zip":
            archive = shutil.make_archive(str(dest.with_suffix("")), "zip", root_dir=stage_root.parent, base_dir=stage_root.name)
            produced = Path(archive)
            produced.replace(dest)
            artifacts.append(Artifact(path=dest, fmt="zip").finalise())
        elif fmt == "deb":
            artifacts.append(create_deb(stage_root, dest, config))
        elif fmt == "rpm":
            try:
                artifacts.append(create_rpm(stage_root, dest, config))
            except BuildError as exc:
                warn("skipping rpm: %s", exc)
        else:
            raise BuildError(f"unsupported format: {fmt}")
    return artifacts
