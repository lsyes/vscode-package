"""Build upstream Visual Studio Code for the LoongArch (loong64) architecture.

The package repackages the official Microsoft Linux binaries with a LoongArch
Electron runtime and native modules rebuilt from source.

Public entry points:

``vscode_loong64.pipeline.build``
    Run the whole pipeline for a :class:`~vscode_loong64.config.BuildConfig`.
``vscode_loong64.upstream``
    Discover upstream VSCode releases.
``vscode_loong64.electron``
    Resolve and cache LoongArch Electron runtimes.
``vscode_loong64.publish``
    Upload artifacts to a GitHub release.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
