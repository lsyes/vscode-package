"""LoongArch64 toolchain helpers and ELF validation.

Two link models are used deliberately:

* Standalone helpers that run as their own process (``ripgrep``) are linked
  fully static against musl, so the package works on any distribution.
* Node-API addons (``.node``) are loaded into the Electron process and must
  share its C library.  The community LoongArch Electron runtime is a glibc
  binary, so every addon - the C/C++ ones as well as ``os-proxy-resolver`` -
  is compiled for ``loongarch64-unknown-linux-gnu``.  Statically linking a
  second libc into a shared object that is later ``dlopen()``-ed by another
  libc is unsupported and crashes on the first allocation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .utils import BuildError, log

MUSL_TARGET = "loongarch64-linux-musl"
MUSL_RUST_TARGET = "loongarch64-unknown-linux-musl"
GNU_RUST_TARGET = "loongarch64-unknown-linux-gnu"
MUSL_GCC = "loongarch64-linux-musl-gcc"
GNU_GCC = "loongarch64-linux-gnu-gcc"


def _readelf(path: Path, flag: str) -> str:
    if not shutil.which("readelf"):
        return ""
    env = dict(os.environ)
    # keep the parsed messages stable regardless of the build host locale
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        ["readelf", flag, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    return proc.stdout or ""


def needed_libraries(path: Path) -> List[str]:
    """Return the ``DT_NEEDED`` entries of an ELF file (empty when static)."""

    libraries: List[str] = []
    for line in _readelf(Path(path), "-d").splitlines():
        if "(NEEDED)" not in line:
            continue
        match = re.search(r"\[([^\]]+)\]", line)
        if match:
            libraries.append(match.group(1))
        else:
            libraries.append(line.split()[-1])
    return libraries


def has_elf_interpreter(path: Path) -> bool:
    return "INTERP" in _readelf(Path(path), "-l")


def static_problems(path: Path) -> List[str]:
    """Return reasons why ``path`` is not a static LoongArch ELF."""

    import struct

    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [f"unreadable: {exc}"]
    if len(data) < 20 or data[:4] != b"\x7fELF":
        return ["not an ELF file"]
    endian = "<" if data[5] == 1 else ">"
    machine = struct.unpack(endian + "H", data[18:20])[0]
    if machine != 258:
        return [f"wrong e_machine {machine}"]

    problems: List[str] = []
    needed = needed_libraries(path)
    if needed:
        problems.append("shared libraries: " + ", ".join(needed))
    if has_elf_interpreter(path):
        problems.append("has an ELF interpreter")
    return problems


def glibc_addon_problems(path: Path) -> List[str]:
    """Return reasons why ``path`` cannot be loaded into the glibc runtime.

    A Node-API addon has to be a LoongArch shared object that dynamically
    links the host's glibc.  Embedding libc (static musl or static glibc)
    produces a binary that may link but crashes once the loader has to run
    libc initialisation code inside a foreign process.
    """

    path = Path(path)
    if not path.exists():
        return ["missing"]
    problems: List[str] = []
    needed = needed_libraries(path)
    if not needed:
        problems.append("statically linked (no DT_NEEDED entries)")
    elif not any(lib.startswith("libc.so") for lib in needed):
        problems.append("does not link libc dynamically")
    if has_elf_interpreter(path):
        problems.append("has an ELF interpreter")
    return problems


def verify_static(path: Path, label: Optional[str] = None) -> None:
    """Raise unless ``path`` is a static LoongArch executable or shared object."""

    problems = static_problems(path)
    if problems:
        raise BuildError(f"{label or path} is not a static LoongArch ELF: " + "; ".join(problems))


def static_rust_env() -> Dict[str, str]:
    """Environment that produces a fully static musl executable."""

    cc = str(shutil.which(MUSL_GCC) or MUSL_GCC)
    flags = "-C target-feature=+crt-static -C relocation-model=pic -C code-model=medium"
    return {
        "CC_loongarch64_unknown_linux_musl": cc,
        "CARGO_TARGET_LOONGARCH64_UNKNOWN_LINUX_MUSL_LINKER": cc,
        "RUSTFLAGS": flags,
    }


def gnu_rust_env() -> Dict[str, str]:
    """Environment for Node-API addons built against the Electron glibc."

    On a LoongArch host the native ``gcc`` is the right compiler; on an
    emulated x86 runner the cross compiler is picked up when it is installed.
    """

    cc = shutil.which(GNU_GCC) or shutil.which("gcc") or GNU_GCC
    return {
        "CC_loongarch64_unknown_linux_gnu": cc,
        "CARGO_TARGET_LOONGARCH64_UNKNOWN_LINUX_GNU_LINKER": cc,
    }


def verify_rust_target() -> None:
    """Check that rustup has the statically linked LoongArch musl target."""

    if not shutil.which("rustc"):
        raise BuildError("rustc is required to build the static Rust helpers")
    proc = subprocess.run(
        ["rustc", "--print", "target-libdir", "--target", MUSL_RUST_TARGET],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0 or not Path(proc.stdout.strip()).is_dir():
        raise BuildError(
            f"Rust target {MUSL_RUST_TARGET} is not installed; run: "
            f"rustup target add {MUSL_RUST_TARGET}"
        )
    log("static Rust target ready: %s", MUSL_RUST_TARGET)


def cargo_home(cache_root: Path, offline: bool) -> Path:
    """Prepare a writable Cargo home, reusing local crates when offline."""

    target = Path(cache_root) / "cargo"
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.toml").write_text(
        "[source.crates-io]\nreplace-with = \"rsproxy-sparse\"\n"
        "[source.rsproxy-sparse]\nregistry = \"sparse+https://rsproxy.cn/index/\"\n",
        encoding="utf-8",
    )
    source_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    for area in ("registry/index", "registry/cache", "git"):
        base = source_home / area
        if not base.exists():
            continue
        for entry in base.iterdir():
            dest = target / area / entry.name
            if dest.exists() or dest.is_symlink():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.symlink_to(entry.resolve(), target_is_directory=entry.is_dir())
            except OSError:
                pass
    return target
