"""Source patches that upstream VS Code needs for a LoongArch64 build.

The helper sources are fetched from the exact upstream tag, so any patch has to
be applied at build time instead of modifying a checkout.  Each patch is
declared as a literal ``(file, old, new)`` triple and is applied idempotently;
a patch whose ``old`` text is already replaced is skipped, and a patch whose
``old`` text cannot be found is reported as *stale* (upstream changed the code)
rather than crashing the build - the compiler error that follows is much easier
to diagnose than a silent mis-patch.

Currently required
------------------
``cli/src/util/prereqs.rs``
    ``check_glibcxx_version`` only defines ``DEFAULT_LIB_PATH`` for
    x86_64/aarch64/x86/arm.  On any other architecture the function does not
    compile at all (``E0425``), so ``code tunnel`` cannot be built for
    LoongArch without this one-line addition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

from .utils import BuildError, log, warn


@dataclass(frozen=True)
class Patch:
    """One literal text substitution inside a fetched source tree."""

    crate: str
    path: str
    old: str
    new: str
    reason: str = ""

    @property
    def name(self) -> str:
        return f"{self.crate}:{self.path}"


PREREQS_PATH = "src/util/prereqs.rs"
DEFAULT_LIB_PATH_OLD = """	#[cfg(any(target_arch = "x86", target_arch = "arm"))]
	const DEFAULT_LIB_PATH: &str = "/usr/lib/libstdc++.so.6";"""
DEFAULT_LIB_PATH_NEW = """	#[cfg(any(target_arch = "x86", target_arch = "arm"))]
	const DEFAULT_LIB_PATH: &str = "/usr/lib/libstdc++.so.6";
	#[cfg(any(target_arch = "loongarch64", target_arch = "riscv64"))]
	const DEFAULT_LIB_PATH: &str = "/usr/lib64/libstdc++.so.6";"""

PATCHES: List[Patch] = [
    Patch(
        crate="cli",
        path=PREREQS_PATH,
        old=DEFAULT_LIB_PATH_OLD,
        new=DEFAULT_LIB_PATH_NEW,
        reason=(
            "check_glibcxx_version() has no DEFAULT_LIB_PATH for loongarch64, "
            "so the crate does not compile (E0425)"
        ),
    ),
]


def patches_for(crate: str) -> List[Patch]:
    return [patch for patch in PATCHES if patch.crate == crate]


def apply(source_root: Path, crate: str, record: bool = True) -> List[Dict[str, object]]:
    """Apply every patch of ``crate`` below ``source_root``.

    Returns one report entry per patch: ``applied``, ``present`` (already
    patched) or ``stale`` (the anchor text disappeared upstream).
    """

    report: List[Dict[str, object]] = []
    for patch in patches_for(crate):
        target = Path(source_root) / patch.path
        entry: Dict[str, object] = {"name": patch.name, "reason": patch.reason}
        if not target.exists():
            entry["status"] = "missing"
            report.append(entry)
            warn("source patch target %s is missing", target)
            continue
        text = target.read_text(encoding="utf-8")
        if patch.new in text:
            entry["status"] = "present"
            report.append(entry)
            continue
        if patch.old not in text:
            entry["status"] = "stale"
            report.append(entry)
            warn(
                "source patch %s no longer applies; upstream changed %s",
                patch.name,
                patch.path,
            )
            continue
        target.write_text(text.replace(patch.old, patch.new, 1), encoding="utf-8")
        entry["status"] = "applied"
        report.append(entry)
        if record:
            log("applied source patch %s (%s)", patch.name, patch.reason)
    return report


def require_applied(report: Sequence[Dict[str, object]]) -> None:
    """Fail the build when a required patch is stale."""

    stale = [str(entry["name"]) for entry in report if entry.get("status") == "stale"]
    if stale:
        raise BuildError(
            "source patches no longer apply (upstream changed the code): " + ", ".join(stale)
        )
