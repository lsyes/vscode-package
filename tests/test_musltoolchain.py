from pathlib import Path

from vscode_loong64 import musltoolchain


def test_static_problems_rejects_missing_file(tmp_path: Path) -> None:
    problems = musltoolchain.static_problems(tmp_path / "missing")
    assert problems and problems[0].startswith("unreadable:")


def test_cache_rg_is_static_when_present() -> None:
    cached = Path(".cache/tools/rg")
    if cached.exists():
        assert musltoolchain.static_problems(cached) == []


def test_gnu_rust_env_targets_the_electron_libc() -> None:
    env = musltoolchain.gnu_rust_env()
    assert "CC_loongarch64_unknown_linux_gnu" in env
    assert "CARGO_TARGET_LOONGARCH64_UNKNOWN_LINUX_GNU_LINKER" in env


def test_glibc_addon_problems_rejects_missing_file(tmp_path: Path) -> None:
    assert musltoolchain.glibc_addon_problems(tmp_path / "missing.node") == ["missing"]
