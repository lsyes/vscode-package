"""Build ``apply-seccomp`` for LoongArch without libseccomp.

``@vscode/sandbox-runtime`` ships a statically linked ``apply-seccomp`` helper
for x64 and arm64 only.  Its source lives inside the package
(``vendor/seccomp-src``), but the file it includes - ``unix-block-bpf.h`` - is
generated at build time from a libseccomp filter, so the package alone cannot
be compiled.  ``generate-seccomp-filter.js`` additionally only maps
``x64``/``arm64`` to a vendor directory, which means a LoongArch build silently
degrades to "do not restrict unix sockets".

This module closes both gaps:

* it emits the BPF program itself (pure Python, no libseccomp, no host
  ``-dev`` package), with the audit token and syscall numbers of the target
  architecture;
* it compiles the upstream ``apply-seccomp.c`` statically and installs it as
  ``vendor/seccomp/loong64/apply-seccomp``;
* it patches ``generate-seccomp-filter.js`` so ``process.arch === "loong64"``
  resolves that directory instead of returning ``null``.

The emitted filter is byte-for-byte equivalent to what libseccomp generates for
the same rules; ``tests/test_seccomp.py`` keeps that property under test.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import musltoolchain, utils
from .utils import BuildError, log, warn

# -- architecture data ---------------------------------------------------------

#: ``AUDIT_ARCH_*`` value as seen in ``seccomp_data.arch``
EM_LOONGARCH = 258
AUDIT_ARCH_LOONGARCH64 = 0xC0000000 | EM_LOONGARCH  # __AUDIT_ARCH_64BIT|__AUDIT_ARCH_LE|EM

#: syscall numbers per architecture (socket + the io_uring family)
SYSCALLS: Dict[str, Dict[str, int]] = {
    "loongarch64": {"socket": 198, "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427},
    "x86_64": {"socket": 41, "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427},
    "aarch64": {"socket": 198, "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427},
}

AUDIT_ARCH: Dict[str, int] = {
    "loongarch64": AUDIT_ARCH_LOONGARCH64,
    "x86_64": 0xC000003E,
    "aarch64": 0xC00000B7,
}

#: ``seccomp_data`` field offsets used by the filter
OFFSET_NR = 0
OFFSET_ARCH = 4
OFFSET_ARG0_LOW = 16

AF_UNIX = 1

#: seccomp return values
RET_ALLOW = 0x7FFF0000
RET_ERRNO_EPERM = 0x00050001
RET_KILL = 0x00000000

#: BPF opcodes (linux/bpf_common.h)
BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_ALU_AND_K = 0x54
BPF_RET_K = 0x06

class SockFilter(NamedTuple):
    """One ``struct sock_filter`` instruction."""

    code: int
    jt: int
    jf: int
    k: int


def filter_program(arch: str = "loongarch64") -> List[SockFilter]:
    """Return the BPF program that blocks ``socket(AF_UNIX, ...)`` and io_uring.

    Layout (the jump offsets are computed, not hardcoded)::

        0  ld  arch            ; seccomp_data.arch
        1  jeq <audit+64>      ; wrong architecture -> kill
        2  ld  nr              ; seccomp_data.nr
        3  jeq io_uring_setup  ; these create sockets in kernel context and
        4  jeq io_uring_enter  ; would bypass the socket() rule below
        5  jeq io_uring_register
        6  jeq socket          ; everything else falls through to allow
        7  ld  args[0] low     ; socket(domain, ...)
        8  and 0xffffffff
        9  jeq AF_UNIX
       10  ret ALLOW
       11  ret ERRNO(EPERM)
       12  ret KILL
    """

    if arch not in SYSCALLS:
        raise BuildError(f"no seccomp syscall table for architecture {arch}")
    numbers = SYSCALLS[arch]
    audit = AUDIT_ARCH[arch]

    io_uring = ["io_uring_setup", "io_uring_enter", "io_uring_register"]
    program: List[SockFilter] = [
        SockFilter(BPF_LD_W_ABS, 0, 0, OFFSET_ARCH),
        SockFilter(BPF_JEQ_K, 0, 0, audit),  # patched below: arch mismatch -> kill
        SockFilter(BPF_LD_W_ABS, 0, 0, OFFSET_NR),
    ]
    io_indexes: List[int] = []
    for name in io_uring:
        io_indexes.append(len(program))
        program.append(SockFilter(BPF_JEQ_K, 0, 0, numbers[name]))
    socket_index = len(program)
    program.append(SockFilter(BPF_JEQ_K, 0, 0, numbers["socket"]))
    program.append(SockFilter(BPF_LD_W_ABS, 0, 0, OFFSET_ARG0_LOW))
    program.append(SockFilter(BPF_ALU_AND_K, 0, 0, 0xFFFFFFFF))
    compare_index = len(program)
    program.append(SockFilter(BPF_JEQ_K, 0, 0, AF_UNIX))
    allow_index = len(program)
    program.append(SockFilter(BPF_RET_K, 0, 0, RET_ALLOW))
    deny_index = len(program)
    program.append(SockFilter(BPF_RET_K, 0, 0, RET_ERRNO_EPERM))
    kill_index = len(program)
    program.append(SockFilter(BPF_RET_K, 0, 0, RET_KILL))

    # a filter built for a foreign ABI must never run: kill instead of allow
    program[1] = SockFilter(BPF_JEQ_K, 0, kill_index - 2, audit)
    # io_uring_* -> deny
    for index in io_indexes:
        program[index] = SockFilter(BPF_JEQ_K, deny_index - index - 1, 0, program[index].k)
    # not socket() -> allow (skip the argument check)
    program[socket_index] = SockFilter(
        BPF_JEQ_K, 0, allow_index - socket_index - 1, numbers["socket"]
    )
    # AF_UNIX -> deny, any other domain -> allow (next instruction)
    program[compare_index] = SockFilter(
        BPF_JEQ_K, deny_index - compare_index - 1, 0, AF_UNIX
    )
    return program


def render_header(arch: str = "loongarch64") -> str:
    """Render ``unix-block-bpf.h`` with the baked-in filter for ``arch``."""

    program = filter_program(arch)
    lines = [
        "/* Generated by vscode_loong64.seccomp - do not edit.",
        f" * Architecture: {arch}, {len(program)} instructions,",
        f" * audit arch 0x{AUDIT_ARCH[arch]:08x}.",
        " */",
        "#include <stddef.h>",
        "#include <linux/filter.h>",
        "",
        "static const struct sock_filter unix_block_bpf[] = {",
    ]
    for insn in program:
        lines.append(
            f"\t{{ 0x{insn.code:04x}, {insn.jt}, {insn.jf}, 0x{insn.k:08x} }},"
        )
    lines.extend(["};", ""])
    return "\n".join(lines)


# -- compilation ---------------------------------------------------------------


@dataclass
class SeccompBuild:
    ok: bool
    binary: Optional[Path] = None
    detail: str = ""
    log_path: Optional[Path] = None
    source: str = ""


def find_sources(app_dir: Path) -> Optional[Path]:
    """Return the ``vendor/seccomp-src`` directory of the staged package."""

    for candidate in (
        app_dir / "node_modules" / "@vscode" / "sandbox-runtime" / "vendor" / "seccomp-src",
        app_dir
        / "node_modules.asar.unpacked"
        / "@vscode"
        / "sandbox-runtime"
        / "vendor"
        / "seccomp-src",
    ):
        if (candidate / "apply-seccomp.c").exists():
            return candidate
    return None


def compile_apply_seccomp(
    source_dir: Path,
    workdir: Path,
    logs: Path,
    arch: str = "loongarch64",
    cc: Optional[str] = None,
) -> SeccompBuild:
    """Compile ``apply-seccomp.c`` statically with the generated BPF header."""

    workdir.mkdir(parents=True, exist_ok=True)
    build_dir = workdir / "apply-seccomp"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    for name in ("apply-seccomp.c", "seccomp-unix-block.c"):
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, build_dir / name)
    if not (build_dir / "apply-seccomp.c").exists():
        return SeccompBuild(ok=False, detail=f"apply-seccomp.c not found in {source_dir}")
    (build_dir / "unix-block-bpf.h").write_text(render_header(arch), encoding="utf-8")

    compiler = cc or os.environ.get("CC") or shutil.which("gcc") or "gcc"
    binary = build_dir / "apply-seccomp"
    cmd = [compiler, "-static", "-O2", "-Wall", "-o", str(binary), str(build_dir / "apply-seccomp.c")]
    log("compiling apply-seccomp (%s, static)", arch)
    proc = utils.run(cmd, cwd=build_dir, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    log_path = logs / "helper-apply-seccomp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not binary.exists():
        tail = output.strip().splitlines()[-3:]
        return SeccompBuild(ok=False, detail="; ".join(tail)[:400] or "gcc failed", log_path=log_path, source=str(build_dir))
    problems = musltoolchain.static_problems(binary)
    if problems:
        return SeccompBuild(
            ok=False,
            detail="apply-seccomp is not a static LoongArch ELF: " + "; ".join(problems),
            log_path=log_path,
            source=str(build_dir),
        )
    binary.chmod(0o755)
    log("built apply-seccomp (%s)", utils.human_size(binary.stat().st_size))
    return SeccompBuild(ok=True, binary=binary, log_path=log_path, source=str(build_dir))


def arch_key(process_arch: str = "loong64") -> str:
    """Directory name used by ``vendor/seccomp/<arch>`` upstream."""

    return {"loong64": "loong64", "loongarch64": "loong64"}.get(process_arch, process_arch)


def patch_generate_filter_js(path: Path, directory: str = "loong64") -> bool:
    """Teach ``generate-seccomp-filter.js`` about LoongArch.

    Upstream maps ``process.arch`` to ``x64``/``arm64`` and logs an error for
    everything else, so the bundled binary is never looked up.  Add the
    ``loong64`` case next to ``aarch64``.  Returns ``True`` when the file was
    changed.
    """

    path = Path(path)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if f"'{directory}'" in text or f'"{directory}"' in text:
        return False
    original = text

    def add_case(pattern: str, replacement: str) -> None:
        nonlocal text
        if re.search(pattern, text):
            text = re.sub(pattern, replacement, text, count=1)

    # JS is minified in the shipped build; accept both quoted spellings.
    add_case(
        r"case\s*'arm64'\s*:\s*case\s*'aarch64'\s*:\s*return\s*'arm64'\s*;",
        f"case 'arm64': case 'aarch64': return 'arm64'; case '{directory}': case 'loongarch64': return '{directory}';",
    )
    add_case(
        r'case\s*"arm64"\s*:\s*case\s*"aarch64"\s*:\s*return\s*"arm64"\s*;',
        f'case "arm64": case "aarch64": return "arm64"; case "{directory}": case "loongarch64": return "{directory}";',
    )
    if text == original:
        # Fall back to the message that is only reached for unsupported arches;
        # rewording keeps the log truthful if the switch statement moved.
        add_case(
            r"Only x64 and arm64 are supported\.",
            "Only x64, arm64 and loong64 are supported.",
        )
    if text == original:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def install(
    app_dir: Path,
    binary: Path,
    arch: str = "loong64",
) -> List[Path]:
    """Place the compiled helper and teach the JS loader about ``arch``.

    The helper is installed twice: under ``vendor/seccomp/<arch>/apply-seccomp``
    (what the JS lookup uses) and next to the upstream ``x64`` directory, so a
    future package update that adds the lookup does not need a rebuild.
    """

    written: List[Path] = []
    vendor = None
    for candidate in (
        app_dir / "node_modules" / "@vscode" / "sandbox-runtime" / "vendor" / "seccomp",
        app_dir
        / "node_modules.asar.unpacked"
        / "@vscode"
        / "sandbox-runtime"
        / "vendor"
        / "seccomp",
    ):
        if candidate.is_dir():
            vendor = candidate
            break
    if vendor is None:
        return written
    target = vendor / arch / "apply-seccomp"
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(binary).resolve() != target.resolve():
        shutil.copy2(binary, target)
    target.chmod(0o755)
    written.append(target)

    for js in (
        app_dir / "node_modules" / "@vscode" / "sandbox-runtime" / "dist" / "sandbox" / "generate-seccomp-filter.js",
        app_dir
        / "node_modules.asar.unpacked"
        / "@vscode"
        / "sandbox-runtime"
        / "dist"
        / "sandbox"
        / "generate-seccomp-filter.js",
    ):
        if patch_generate_filter_js(js, arch):
            log("patched seccomp arch lookup in %s", js)
            written.append(js)
    return written


def describe_binary(binary: Path) -> str:
    """One-line description used in the manifest."""

    problems = musltoolchain.static_problems(binary)
    return "static" if not problems else "; ".join(problems)
