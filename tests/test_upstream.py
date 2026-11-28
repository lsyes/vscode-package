"""Tests for the upstream update API layer (pure functions + payload mapping)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

import pytest

from vscode_loong64 import upstream


LATEST_PAYLOAD: Dict[str, object] = {
    "url": "https://example.invalid/code-stable-x64-1795525921.tar.gz",
    "name": "1.148.0",
    "version": "661626d0e72f97806550790a98fb87116c9db825",
    "productVersion": "1.148.0",
    "hash": "d5701c27efdcd0bca940fa1286b04b0105a69cd1",
    "timestamp": 1795524073017,
    "sha256hash": "7acdaf01a1d7a27c55828156ec3fdbe75b8e0809070fecd10de022c4982a0f16",
    "supportsFastUpdate": True,
}


def test_latest_release_uses_version_as_commit(monkeypatch) -> None:
    monkeypatch.setattr(upstream.utils, "http_get_json", lambda url, **kw: dict(LATEST_PAYLOAD))
    release = upstream.latest_release()
    assert release.version == "1.148.0"
    assert release.commit == "661626d0e72f97806550790a98fb87116c9db825"
    # ``hash`` is the installer SHA-1, never the commit
    assert release.sha1 == "d5701c27efdcd0bca940fa1286b04b0105a69cd1"
    assert release.sha256 == LATEST_PAYLOAD["sha256hash"]


def test_specific_release_uses_versions_endpoint(monkeypatch) -> None:
    seen = {}

    def fake(url, **kwargs):
        seen["url"] = url
        return dict(LATEST_PAYLOAD)

    monkeypatch.setattr(upstream.utils, "http_get_json", fake)
    release = upstream.specific_release("1.148.0")
    assert seen["url"] == "https://update.code.visualstudio.com/api/versions/1.148.0/linux-x64/stable"
    assert release.commit == LATEST_PAYLOAD["version"]
    assert release.version == "1.148.0"


def test_specific_release_platform_and_quality(monkeypatch) -> None:
    seen = {}

    def fake(url, **kwargs):
        seen["url"] = url
        return dict(LATEST_PAYLOAD)

    monkeypatch.setattr(upstream.utils, "http_get_json", fake)
    upstream.specific_release("1.148.0", platform="linux-arm64", quality="insider")
    assert seen["url"].endswith("/versions/1.148.0/linux-arm64/insider")


def test_specific_release_rejects_unknown_version(monkeypatch) -> None:
    monkeypatch.setattr(upstream.utils, "http_get_json", lambda url, **kw: {"error": "nope"})
    with pytest.raises(upstream.BuildError):
        upstream.specific_release("9.9.9")


def test_resolve_version_routes_latest_and_explicit(monkeypatch) -> None:
    monkeypatch.setattr(upstream, "latest_release", lambda **kw: "LATEST")
    monkeypatch.setattr(upstream, "specific_release", lambda v, **kw: f"SPECIFIC:{v}")
    assert upstream.resolve_version("latest") == "LATEST"
    assert upstream.resolve_version(None) == "LATEST"
    assert upstream.resolve_version("1.147.0") == "SPECIFIC:1.147.0"


def test_release_info_roundtrip_keeps_sha1() -> None:
    release = upstream.ReleaseInfo(version="1.148.0", commit="abc", sha1="def", sha256="123")
    restored = upstream.ReleaseInfo.from_dict(release.as_dict())
    assert restored.commit == "abc"
    assert restored.sha1 == "def"
    assert restored.sha256 == "123"
    assert restored.tag == "v1.148.0-loong64"


def test_short_commit() -> None:
    assert upstream.ReleaseInfo(version="1", commit="661626d0e72f").short_commit == "661626d0"


def test_change_report_detects_behind(monkeypatch) -> None:
    report = upstream.change_report("1.147.0", "1.148.0")
    assert report["update"] is True
    report = upstream.change_report("1.148.0", "1.148.0")
    assert report["update"] is False
    report = upstream.change_report("latest", "1.148.0")
    assert report["update"] is True


def test_latest_or_none_swallows_errors(monkeypatch) -> None:
    def boom(**kwargs):
        raise upstream.BuildError("offline")

    monkeypatch.setattr(upstream, "latest_release", boom)
    assert upstream.latest_or_none() is None


def test_should_build_skips_published_tag() -> None:
    release = upstream.ReleaseInfo(version="1.148.0", commit="abc")
    assert upstream.should_build(release, ["v1.147.0-loong64"]) is True
    assert upstream.should_build(release, ["v1.148.0-loong64"]) is False
    assert upstream.should_build(release, [], force=True) is True
