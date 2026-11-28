# VS Code for LoongArch64

This project repackages Microsoft's official VS Code Linux build for
LoongArch (`loong64`). It downloads the upstream x64 application, replaces the
Electron runtime with a LoongArch build, rebuilds every native module **and
every standalone helper** (the tunnel CLI, the Copilot CLI bootstrapper, the
seccomp helper and the extension signature verifier), patches asset lookup
paths, creates a Debian package, and can publish the result to a GitHub Release.

`make deb` performs the complete local flow and produces a `.deb`. The GitHub
Actions workflow repeats the same flow on a schedule and only publishes when the
upstream release changes.

## What is included

| Component | Build policy |
| --- | --- |
| Electron | Community LoongArch runtime (`darkyzhou/electron-loong64`), glibc |
| Node-API C/C++ addons | `node-gyp` rebuild for `loongarch64-unknown-linux-gnu` |
| `os-proxy-resolver` | Microsoft source, Node-API crate, `loongarch64-unknown-linux-gnu` |
| `ripgrep` | Rust, `loongarch64-unknown-linux-musl`, fully static |
| `bin/code-tunnel` | Rust, rebuilt from `cli/` of the matching upstream tag, static-pie |
| `bin/copilot-shim/copilot` | Rust, rebuilt from `copilot_shim/`, static musl |
| `apply-seccomp` | C, rebuilt from the sources bundled in `@vscode/sandbox-runtime`, static |
| `vsce-sign` | C# NativeAOT, reimplemented in `src-tools/vsce-sign-cs` |
| `vsda` | JavaScript shim (the upstream binding is closed source and x64-only) |
| VS Code JavaScript/extensions | Copied from the official upstream payload |
| Debian package | Built manually with `dpkg-deb` (or GNU `ar`/`tar`), `Architecture: loong64` |

Every payload Microsoft publishes only for x64/arm64 is either rebuilt from
source or removed on purpose; the build fails verification if an unknown
foreign binary remains anywhere in the package.

### Standalone helpers

Four executables in an official build are architecture specific but are not
`.node` addons, so `node-gyp` cannot produce them:

| Helper | Why it cannot stay x64 |
| --- | --- |
| `bin/code-tunnel` | `out/cli.js` spawns it for `code tunnel`, `code serve-web` and `code agent`; the x64 ELF cannot run |
| `bin/copilot-shim/copilot` | the Copilot terminal profile launches it; it then looks for a CLI that has no LoongArch release |
| `apply-seccomp` | `@vscode/sandbox-runtime` execs it as its seccomp wrapper; without it the sandbox silently stops blocking unix sockets |
| `vsce-sign` | `code --install-extension` verifies signatures with it; a broken binary makes **every** signature-checked Marketplace install fail |

They are resolved in this order, first match wins:

1. `--prebuilt <dir>` / `VSCODE_LOONG64_PREBUILTS` (explicit directory),
2. the local prebuilt cache `<cache>/prebuilts/<helper>/<version>/loong64/`,
3. the repository's `prebuilts/` directory (what CI publishes),
4. compiling from the sources of the **exact** payload version.

`code-tunnel` and the shim are cloned out of the matching upstream tag with
`https://codeload.github.com/microsoft/vscode/tar.gz/<tag>` (no git checkout,
only `cli/`, `copilot_shim/`, `package.json` and `product.json` are extracted)
and cached per version, so a new upstream release automatically rebuilds them
against its own sources instead of the previous ones. `--no-prebuilt` ignores
the cache, `--no-helpers` keeps the upstream binaries (the result is broken on
LoongArch and is meant for debugging only).

One upstream source patch is required and applied automatically: `cli/build.rs`
stamps the binary from `product.json`, and `cli/src/util/prereqs.rs` has no
`DEFAULT_LIB_PATH` for `loongarch64`, so the crate does not compile without
adding it. `src/vscode_loong64/patches.py` declares the patch as a literal
substitution and fails the build when upstream changed the anchor text.

### Building on an x86-64 runner (prebuilt payload bundle)

GitHub does not provide LoongArch runners, and only the compile steps actually
need one: rebuilding the addons, `ripgrep`, the Rust helpers, `apply-seccomp`
and `vsce-sign`. Everything else - staging, `product.json`/workbench patching,
pruning, verification, `deb` writing - is architecture independent.

A *payload bundle* captures exactly the architecture specific inputs of one
build, so the packaging host needs no cross toolchain:

```bash
# 1. on a LoongArch machine (or a container with the LoongArch toolchain)
make bundle VERSION=1.148.0
#    dist/loong64-prebuilts-1.148.0-electron43.7.7-loong64.tar.gz

# 2. on any x86-64 machine
make deb-from-bundle VERSION=1.148.0 \
  BUNDLE=dist/loong64-prebuilts-1.148.0-electron43.7.7-loong64.tar.gz
```

A bundle is a flat tar.gz of

| Member | Contents |
| --- | --- |
| `bundle.json` | schema version, upstream version/commit, Electron version, arch, one SHA-256 per payload |
| `natives/<module>/…` | the compiled addons, addressed by the path they are installed to |
| `helpers/<name>/<binary>` | `code-tunnel`, `copilot-shim`, `apply-seccomp`, `vsce-sign` |
| `rg` | the static musl ripgrep for every bundled copy |

`--prebuilt-bundle` verifies every entry (size, SHA-256, ELF machine) and
refuses a bundle whose upstream or Electron version does not match the payload,
so a stale bundle can never be smuggled into a release. The result is recorded
in the manifest (`prebuiltBundleInput`/`prebuiltBundle`), and the build still
runs the full verification pass, so the missing compilers are visible rather
than silently skipped. Bundles can be published as release assets and fetched
with `vscode-loong64 prebuilts fetch --version 1.148.0 --repo owner/name`.

### Extension signatures (`vsce-sign`)

`@vscode/vsce-sign` ships a .NET NativeAOT verifier for nine platform/arch
pairs, none of them LoongArch; on this machine it aborts with
`Failed to create CoreCLR`. Rebuilding Microsoft's binary is impossible (the
source is closed), so the verifier is reimplemented in C# and published with
NativeAOT for `linux-loongarch64` — see
[`src-tools/vsce-sign-cs`](src-tools/vsce-sign-cs/README.md) for the reverse
engineered CLI contract, the return codes and the trust model.

The builder locates a .NET SDK with the `linux-loongarch64` NativeAOT pack
(`VSCODE_LOONG64_DOTNET_ROOT`, `DOTNET_ROOT`, `<cache>/dotnet`, `PATH`) and
falls back to downloading the Loongson SDK. Pass `--no-dotnet-download` to keep
it offline; `vsce-sign` is then reported as unavailable and the package keeps
the x64 binary, which the verification step flags.

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
(`ripgrep`, `code-tunnel`, the Copilot shim, `apply-seccomp`) are linked
statically.

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
- optionally `loongarch64-linux-musl-gcc`: the Rust `loongarch64-unknown-linux-musl`
  target ships its own musl + crt objects, so a plain `gcc` links the static
  helpers; the cross driver is only preferred when present
- `pkg-config`, plus development headers required by VS Code's addons
  (`libxkbcommon-dev`, `libxkbfile-dev`, `libkrb5-dev`, `krb5-multidev`)

On Debian/Ubuntu:

```bash
sudo apt install build-essential binutils git make curl \
  gcc-loongarch64-linux-gnu g++-loongarch64-linux-gnu \
  pkg-config zstd \
  libxkbcommon-dev libxkbfile-dev libkrb5-dev krb5-multidev
rustup target add loongarch64-unknown-linux-gnu loongarch64-unknown-linux-musl
python3 -m pip install -e '.[dev]'
```

On a native LoongArch host the default `gcc`/`g++` are the correct compilers
for the glibc addons. Cross builds need `gcc-loongarch64-linux-gnu`; the
builder picks it up automatically when the native compiler is absent.

The standalone helpers add these requirements:

- a C compiler that can link statically (`gcc -static`) for `apply-seccomp`;
  no `libseccomp-dev` needed - the BPF filter is emitted by
  `src/vscode_loong64/seccomp.py`;
- a .NET SDK with the `linux-loongarch64` NativeAOT pack for `vsce-sign`. It is
  detected via `VSCODE_LOONG64_DOTNET_ROOT`/`DOTNET_ROOT`, `<cache>/dotnet` or
  `PATH`, and downloaded from
  [`loongson/dotnet`](https://github.com/loongson/dotnet/releases) when missing;
- `libssl-dev`, `libzstd-dev` and `zlib1g-dev` headers, which the builder
  fetches with `apt-get download` into a private sysroot (no root required).

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

Helper-specific options:

```bash
# reuse helpers built earlier (default: <cache>/prebuilts and ./prebuilts)
vscode-loong64 build 1.148.0 --prebuilt /srv/prebuilts

# ignore every prebuilt and compile from source, even when a cache exists
vscode-loong64 build 1.148.0 --no-prebuilt

# never download the .NET SDK (vsce-sign then stays x64 and is reported)
vscode-loong64 build 1.148.0 --no-dotnet-download

# keep the upstream x64 helpers (broken on LoongArch, for bisecting only)
vscode-loong64 build 1.148.0 --no-helpers
```

The cache layout is
`<cache>/prebuilts/<helper>/<version>/loong64/<binary>` plus a
`<binary>.helper.json` marker that records the recipe revision, the SHA-256 of
`product.json` and the build target, so a prebuilt is only reused when it
matches the payload being repackaged.

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
   API (or use the supplied local archive) and compare it with the newest
   published release, recording the delta in the manifest.
2. Download the official x64 installer and verify its SHA-256.
3. Extract the installer and obtain the matching LoongArch Electron runtime.
4. Rebuild the C/C++ addons with `node-gyp` against the Electron headers, and
   compile `os-proxy-resolver` from the Microsoft repository for
   `loongarch64-unknown-linux-gnu`.
5. Build `ripgrep` 14.1.1 as a static musl executable.
6. Rebuild the standalone helpers for the exact payload version (prebuilt cache
   first, then the upstream sources of that tag, then the bundled C sources for
   `apply-seccomp` and `src-tools/vsce-sign-cs` for `vsce-sign`).
7. Stage the upstream JavaScript/extensions, overlay Electron, replace all
   bundled `rg` binaries, install the native bindings and helpers, remove the
   prebuilt payloads upstream never published for LoongArch (both on disk and
   packed inside `node_modules.asar`), and patch product metadata plus the
   Copilot CLI entry point.
8. Run structural/static-link checks (including "no foreign native payloads"
   over `node_modules.asar.unpacked`, `node_modules`, `extensions` and `bin/`)
   and a headless CLI smoke test.
9. Build the `deb`/`tar.gz` artifacts manually, write `dist/manifest.json`, and
   optionally publish with `gh release`.

The manifest records the upstream version and commit, the newest upstream
release at build time, the Electron runtime, the proxy resolver
target/backend, every native module and helper that was built, reused or
removed (with the reason), artifact hashes, and verification results.

### Deliberately unsupported prebuilts

`src/vscode_loong64/config.py` lists the payloads upstream never built for
LoongArch, with the reason for each removal:

| Payload | Reason |
| --- | --- |
| `@microsoft/copilot-sdk-linux-x64`, `@github/copilot/**/prebuilds` | only x64/arm64 `runtime.node`; the extension falls back to TypeScript |
| `@github/copilot/**/tgrep` | x64/arm64 helper; `rg` is substituted |
| `msal-node-runtime.node`, `libmsalruntime.so` | upstream prebuilds exist for win32/darwin/linux-x64 only; the broker falls back to JS |
| `@vscode/deviceid/**/windows.node` | Windows-only binding; Linux uses `dist/devdeviceid.js` |
| `ms-vscode.js-debug/**/*.win32-*.node` | Windows-only debugging helper |
| `@vscode/sandbox-runtime/vendor/seccomp/x64` | replaced by the rebuilt `vendor/seccomp/loong64/apply-seccomp` |
| `foundry-local-sdk`, `@teddyzhu/*`, `@picovoice/*`, `napi-oop-runtime` | no loongarch64 prebuild exists |

Anything not on that list is an error: `disable_foreign_helpers` and
`verify_layout` both walk every directory that can hold a native payload, so a
new foreign binary in a future upstream release fails the build instead of
slipping into a package.

## Continuous integration

The compile steps need a LoongArch machine; the packaging steps do not. Since
no hosted runner is LoongArch, the recommended split is a periodic job on a
LoongArch box (or a QEMU/container job) that only runs `make bundle`, plus an
ordinary `ubuntu-latest` job that consumes the bundle with
`make deb-from-bundle` and publishes. Independently of where they run, the
builder itself already:

- compares the payload with the newest upstream release and records the delta
  (`upstreamChange` in the manifest), so "nothing changed" is visible without a
  second API call;
- re-fetches `cli/` and `copilot_shim/` for the new tag and rebuilds
  `code-tunnel`/`copilot-shim` against it;
- reuses helpers from `prebuilts/` (or `PREBUILT=<dir>`) when the version,
  recipe revision and `product.json` hash still match, so a warm runner does not
  recompile the tunnel CLI and the shim on every run.

Publishing uses the repository-scoped `GITHUB_TOKEN`
(`make publish REPO=owner/name TAG=v<version>-loong64`), and the workflow needs
**Actions > General > Workflow permissions** set to read and write when the
organization does not grant that by default.

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
- `node_modules.asar` is rewritten only when a native member has to go. The
  writer reuses the reader's format rules, keeps `unpacked` entries offset-less
  with their real size (Electron reads that size, a zero size makes the loader
  report *Could not locate the bindings file*), and the build verifies the
  result by re-reading the archive.
- `@vscode/sandbox-runtime` looks its helper up as
  `vendor/seccomp/{x64,arm64}/apply-seccomp` and returns `null` for any other
  architecture, which silently downgrades the sandbox. The builder installs the
  rebuilt binary under `vendor/seccomp/loong64/` **and** teaches
  `generate-seccomp-filter.js` the `loong64` case; the generated filter is
  verified to allow `AF_INET` sockets and deny `AF_UNIX`.
- `chat.copilotCliCommand.enabled` defaults to `true` upstream and the Copilot
  CLI has no LoongArch release, so the builder flips the bundled default to
  `false`; the terminal profile disappears instead of failing at run time.
  Users can still enable it explicitly.

## Artifact layout

The `.deb` installs the application under `/usr/share/code`, with a launcher at
`/usr/bin/code`, a desktop entry, and icons. The staged tarball contains the
same application tree with `VSCode-linux-loong64/` as its top-level directory.
Each run writes `dist/manifest.json`.

With `--emit-prebuilt-bundle`, `dist/loong64-prebuilts-<version>-electron<version>-loong64.tar.gz`
is written as well; it is the only input `--prebuilt-bundle` needs on a host
without a LoongArch toolchain.
