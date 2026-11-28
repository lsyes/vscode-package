"""Tests for the generated seccomp BPF filter and its compiler glue."""

from __future__ import annotations

import struct
from pathlib import Path

from vscode_loong64 import seccomp


def test_program_jumps_stay_inside_the_program() -> None:
    for arch in seccomp.SYSCALLS:
        program = seccomp.filter_program(arch)
        for index, insn in enumerate(program):
            assert index + insn.jt < len(program), (arch, index, "jt")
            assert index + insn.jf < len(program), (arch, index, "jf")


def test_program_blocks_af_unix_and_io_uring() -> None:
    program = seccomp.filter_program("loongarch64")
    returns = [insn.k for insn in program if insn.code == seccomp.BPF_RET_K]
    assert seccomp.RET_ALLOW in returns
    assert seccomp.RET_ERRNO_EPERM in returns
    assert seccomp.RET_KILL in returns
    # the AF_UNIX comparison compares args[0] against 1
    assert any(insn.code == seccomp.BPF_JEQ_K and insn.k == seccomp.AF_UNIX for insn in program)
    # the io_uring trio is denied by number
    numbers = {insn.k for insn in program if insn.code == seccomp.BPF_JEQ_K}
    for name in ("io_uring_setup", "io_uring_enter", "io_uring_register", "socket"):
        assert seccomp.SYSCALLS["loongarch64"][name] in numbers


def test_audit_arch_is_the_loongarch_token() -> None:
    # EM_LOONGARCH | __AUDIT_ARCH_64BIT | __AUDIT_ARCH_LE
    assert seccomp.AUDIT_ARCH["loongarch64"] == 0xC0000000 | 258


def test_render_header_is_valid_c() -> None:
    header = seccomp.render_header("loongarch64")
    assert "unix_block_bpf[]" in header
    assert header.count("{ 0x") == len(seccomp.filter_program("loongarch64"))
    # the C initialiser must fit into unsigned char jump fields
    for line in header.splitlines():
        if not line.strip().startswith("{ 0x"):
            continue
        parts = line.strip().strip("{},").split(",")
        assert 0 <= int(parts[1]) <= 255
        assert 0 <= int(parts[2]) <= 255


def test_foreign_architecture_is_rejected() -> None:
    try:
        seccomp.filter_program("mips")
    except Exception as exc:  # noqa: BLE001 - BuildError is enough
        assert "no seccomp syscall table" in str(exc)
    else:
        raise AssertionError("unknown architecture was accepted")


def test_patch_generate_filter_js(tmp_path: Path) -> None:
    js = tmp_path / "generate-seccomp-filter.js"
    js.write_text(
        "function getVendorArchitecture(){switch(process.arch){"
        "case 'x64':return 'x64';"
        "case 'arm64':case 'aarch64':return 'arm64';"
        "default:return null}}",
        encoding="utf-8",
    )
    assert seccomp.patch_generate_filter_js(js) is True
    text = js.read_text(encoding="utf-8")
    assert "'loong64'" in text
    # idempotent
    assert seccomp.patch_generate_filter_js(js) is False


def test_patch_generate_filter_js_tolerates_minified_quotes(tmp_path: Path) -> None:
    js = tmp_path / "generate-seccomp-filter.js"
    js.write_text(
        'switch(n){case"arm64":case"aarch64":return"arm64";default:return null}',
        encoding="utf-8",
    )
    assert seccomp.patch_generate_filter_js(js) is True
    assert '"loong64"' in js.read_text(encoding="utf-8")


def test_missing_source_dir_is_reported(tmp_path: Path) -> None:
    assert seccomp.find_sources(tmp_path) is None
