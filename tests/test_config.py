from pathlib import Path

from vscode_loong64.config import BuildConfig, Paths


def test_artifact_names(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path)
    config = BuildConfig(version="1.148.0", paths=paths, binary_name="code")
    assert config.artifact_name("tar.gz") == "code-1.148.0-loong64.tar.gz"
    assert config.artifact_name("deb") == "code_1.148.0_loong64.deb"


def test_build_config_requires_supported_formats(tmp_path: Path) -> None:
    from vscode_loong64.utils import BuildError

    config = BuildConfig(paths=Paths.create(tmp_path), formats=["snap"])
    try:
        config.validate()
    except BuildError:
        pass
    else:
        raise AssertionError("unsupported format was accepted")
