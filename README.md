# VS Code for LoongArch64

This project repackages Microsoft's official VS Code Linux build for
LoongArch (`loong64`). It downloads the upstream x64 application, replaces the
Electron runtime with a LoongArch build, rebuilds every native module, patches
asset lookup paths, creates a Debian package, and can publish the result to a
GitHub Release.

`make deb` performs the complete local flow and produces a `.deb`. The GitHub
Actions workflow repeats the same flow on a schedule, downloads the current
upstream installer, and publishes only when the upstream release changes.

## What is included

| Component | Build policy |
| --- | --- |
| Electron | Community LoongArch runtime (`darkyzhou/electron-loong64`), glibc |
| Node-API C/C++ addons | `node-gyp` rebuild for `loongarch64-unknown-linux-gnu` |
| `os-proxy-resolver` | Microsoft source, Node-API crate, `loongarch64-unknown-linux-gnu` |
| `ripgrep` | Rust, `loongarch64-unknown-linux-musl`, fully static |
| `vsda` | JavaScript shim (the upstream binding is closed source and x64-only) |
| VS Code JavaScript/extensions | Copied from the official upstream payload |
| Debian package | Built manually with `dpkg-deb` (or GNU `ar`/`tar`), `Architecture: loong64` |

### Native module ABI

Every `.node` addon is loaded into the Electron process, so it must use the
same C library as the runtime. The LoongArch Electron build is a glibc binary;
therefore all addons are compiled for `loongarch64-unknown-linux-gnu`. This is
the same split Microsoft publishes for x64 (`-gnu`/`-musl` packages), and it
matches how VS Code ships on glibc distributions.

Statically linking a second libc (musl or glibc) into a shared object that is
later `dlopen()`-ed by another libc is not a supported configuration and
crashes on the first allocation. The builder therefore rejects such an addon
instead of packaging it. Only standalone helpers that run as their own process
(`ripgrep`) are linked statically.

### Proxy resolver

The proxy addon is compiled from
[`microsoft/os-proxy-resolver`](https://github.com/microsoft/os-proxy-resolver)
(`npm/native` crate) for `loongarch64-unknown-linux-gnu` and installed as
`@vscode/os-proxy-resolver-linux-loong64-gnu`; the facade's platform table is
extended with `linux-loong64-glibc`. There is no JavaScript shim and no
hand-written stub binding: the only accepted proxy source is a real Microsoft
checkout, which the builder verifies before compiling.

PAC evaluation uses the repository's native QuickJS-NG backend (`pac-engine`)
by default. Upstream's `pac-engine-wasm2c` backend needs WABT 1.0.41 exactly;
if you have that binary installed, build with
`VSCODE_LOONG64_PROXY_BACKEND=wasm2c` to keep the wasm sandbox.

The upstream Copilot SDK only publishes `runtime.node` prebuilds for x64/arm64,
so its platform package is removed from the staged app; the extension then uses
its TypeScript fallback instead of loading a foreign ABI.

## Requirements

Build on a LoongArch64 machine (or a matching LoongArch runner) with:

- Python 3.9+, Node.js and npm
- `git`, `make`, `g++`, `python3`, `ar`, `tar`, `xz`, `zstd`
- Rust with the targets `loongarch64-unknown-linux-gnu` and
  `loongarch64-unknown-linux-musl`
- `loongarch64-linux-musl-gcc` (static ripgrep)
- `pkg-config`, plus development headers required by VS Code's addons
  (`libxkbcommon-dev`, `libxkbfile-dev`, `libkrb5-dev`, `krb5-multidev`)

On Debian/Ubuntu:

```bash
sudo apt install build-essential binutils git make \
  gcc-loongarch64-linux-gnu g++-loongarch64-linux-gnu \
  musl-dev musl-tools pkg-config zstd \
  libxkbcommon-dev libxkbfile-dev libkrb5-dev krb5-multidev
rustup target add loongarch64-unknown-linux-gnu loongarch64-unknown-linux-musl
python3 -m pip install -e '.[dev]'
```

On a native LoongArch host the default `gcc`/`g++` are the correct compilers
for the glibc addons. Cross builds need `gcc-loongarch64-linux-gnu`; the
builder picks it up automatically when the native compiler is absent.

## Local build

Build the latest stable release and emit `dist/code-<version>-loong64.tar.gz`
plus `dist/code_<version>_loong64.deb`:

```bash
make build VERSION=latest
```

Build only the Debian package:

```bash
make deb VERSION=1.148.0
```

Use already-downloaded inputs, which is useful for repeatable/offline runs:

```bash
make deb VERSION=1.148.0 \
  UPSTREAM=.cache/downloads/code-1.148.0-linux-x64.tar.gz \
  ELECTRON_ARCHIVE=.cache/electron-loong64/downloads/electron-v43.7.7-linux-loong64.zip \
  OS_PROXY_SRC=/path/to/os-proxy-resolver
```

`OS_PROXY_SRC` may be omitted; the builder then clones the pinned Microsoft
repository (`v0.4.0`) into the work directory. Without network access, pass a
checkout explicitly.

The corresponding CLI is available directly:

```bash
PYTHONPATH=src python3 -m vscode_loong64.cli build 1.148.0 \
  --formats deb \
  --os-proxy-src /path/to/os-proxy-resolver
```

Other useful targets and commands:

```bash
make check                 # Python syntax and tests
make package STAGE=work/build/stage VERSION=1.148.0
make publish               # requires dist/manifest.json and GH_TOKEN
make clean
python3 -m vscode_loong64.cli doctor
```

## Pipeline

1. Resolve the requested upstream stable/insider release from Microsoft's update
   API (or use the supplied local archive).
2. Download the official x64 installer and verify its SHA-256.
3. Extract the installer and obtain the matching LoongArch Electron runtime.
4. Rebuild the C/C++ addons with `node-gyp` against the Electron headers, and
   compile `os-proxy-resolver` from the Microsoft repository for
   `loongarch64-unknown-linux-gnu`.
5. Build `ripgrep` 14.1.1 as a static musl executable.
6. Stage the upstream JavaScript/extensions, overlay Electron, replace all
   bundled `rg` binaries, install the native bindings, and patch product
   metadata.
7. Run structural/static-link checks and a headless CLI smoke test.
8. Build the `deb`/`tar.gz` artifacts manually, write `dist/manifest.json`, and
   optionally publish with `gh release`.

The manifest records the upstream version and commit, the Electron runtime,
the proxy resolver target/backend, every native module that was built or
disabled, artifact hashes, and verification results.

## GitHub Actions

[`.github/workflows/build.yml`](.github/workflows/build.yml) runs:

- on a daily schedule;
- manually with `workflow_dispatch` (optional upstream version and publish
  toggle);
- on tags matching `v*-loong64`.

The build job runs on a self-hosted runner labelled
`self-hosted, linux, loong64` because the native addons must be compiled for
the target architecture. It installs the LoongArch toolchains, downloads the
upstream installer, runs the same builder, verifies the resulting `rg` is
static and the proxy addon links glibc, checks the `.deb` architecture, uploads
the artifacts, and publishes a release with the repository-scoped
`GITHUB_TOKEN`. If the release tag already exists, the scheduled job skips the
build; manual dispatch can force a rebuild.

Configure the repository's **Actions > General > Workflow permissions** to
allow read and write access if your organization does not grant this by
default.

## Reliability notes

- `.node` addons are intentionally **not** forced through musl. There is no
  supported way to load a statically linked foreign libc into the glibc
  Electron process; the builder validates the `DT_NEEDED` entries and refuses
  an addon that does not link libc dynamically.
- Frontend note: VS Code's startup log reports
  `[node-compile-cache] ... status: missing` because the x64-compiled cache is
  keyed to a different Node/arch pair. It is removed during staging and the
  message is informational - the application continues normally.
- The Rust path is the strict static path: the builder verifies `NEEDED` and
  `INTERP` with `readelf`, not just the ELF machine type.
- Ripgrep's upstream musl build normally enables jemalloc. The builder removes
  that optional dependency and uses Rust's static system allocator, avoiding a
  second C build toolchain requirement.
- The project uses a manually constructed Debian archive and does not depend on
  `electron-builder` for packaging. If `dpkg-deb` is available it is used as the
  final archive writer; the fallback writes GNU-format tar members.
- PAX tar headers are avoided because some LoongArch `dpkg` builds reject them
  as an unsupported type (`x`). GNU long-name entries remain compatible.
- Repeated `rg` payloads are hardlinked in the staged tree and in the archive,
  so the installed package has one binary plus the expected lookup aliases.

## Artifact layout

The `.deb` installs the application under `/usr/share/code`, with a launcher at
`/usr/bin/code`, a desktop entry, and icons. The staged tarball contains the
same application tree with `VSCode-linux-loong64/` as its top-level directory.
Each run writes `dist/manifest.json`.
