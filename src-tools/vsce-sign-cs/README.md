# vsce-sign for Linux/loongarch64

A from-scratch C# reimplementation of Microsoft's `@vscode/vsce-sign` helper,
published with .NET NativeAOT as a self-contained `linux-loongarch64` ELF
executable. It is a drop-in replacement for the x64 binary shipped in
`node_modules.asar.unpacked/@vscode/vsce-sign/bin/vsce-sign`, which fails on
LoongArch with `Failed to create CoreCLR, HRESULT: 0x8007000E`.

The JavaScript wrapper (`@vscode/vsce-sign/src/main.js`) spawns the binary with
`execFile` and maps the process exit code to an
`ExtensionSignatureVerificationCode`, so the exit codes below are the entire
contract. VS Code uses `verify` to decide whether an installed extension's
package is intact and was signed by Microsoft.

## Build

```sh
DOTNET_ROOT=/tmp/dotnet-la ./build.sh
# equivalently:
/tmp/dotnet-la/dotnet publish src/VsceSign/VsceSign.csproj \
    -c Release -r linux-loongarch64 -o out \
    -p:PublishAot=true -p:StripSymbols=true -p:InvariantGlobalization=true
```

The SDK must ship the `linux-loongarch64` NativeAOT packs
(`runtime.linux-loongarch64.Microsoft.DotNet.ILCompiler` and
`Microsoft.NETCore.App.Runtime.NativeAOT.linux-loongarch64`). No cross compiler
is required; the native `clang`/`ld` on the host are used.

Result: `out/vsce-sign`, a stripped LoongArch ELF that links only `libc`, `libm`
and the loader. It runs with no `DOTNET_ROOT`, no runtime directory, and no
`box64`:

```
$ file out/vsce-sign
ELF 64-bit LSB pie executable, LoongArch, version 1 (SYSV), dynamically linked,
interpreter /lib64/ld-linux-loongarch-lp64d.so.1, ... stripped
$ readelf -d out/vsce-sign | grep NEEDED     # libm.so.6, libc.so.6, ld-linux-loongarch-lp64d.so.1
$ env -i PATH=/usr/bin:/bin out/vsce-sign verify --package ext.vsix --signaturearchive vim.sigzip; echo $?
0
```

`out/vsce-sign.dbg` is a separated symbol file, not a runtime dependency; it can
be discarded or archived for crash triage.

## Test suite

```sh
./verify.sh out/vsce-sign [<vsix> <sigzip>]
```

The suite builds its own broken/tampered fixtures with `python3` and `openssl`
and asserts every return code; it currently covers 35 cases (all passing). It
defaults to the fixtures at `/tmp/vsix/ext.vsix` and `/tmp/vsix/vim.sigzip`.

## What was reverse engineered

The reference binary
(`work/build/stage/resources/app/node_modules.asar.unpacked/@vscode/vsce-sign/bin/vsce-sign`,
17 MB, `Microsoft.VisualStudio.Extensions.Signing` 2.0.8) is a stripped .NET
NativeAOT executable. It cannot be executed here, so the contract was recovered
statically:

* `strings` gives the command names (`generatemanifest`, `verify`, `zip`), the
  full option table (`--package`, `--signaturearchive`, `--manifest`,
  `--signature`, `--output`, `--verbose`, `--isppe`,
  `--requirerevocationstatus`, ... and their single-letter aliases `-p -sa -m -s
  -o -v -c -ha -ts -ap -z`), the diagnostic lines it prints (`Package file size:
  `, `Manifest file hash:  `, `Exit code:  `, `Primary signature type:  `, ...),
  and the default file names `.signature.manifest`, `.signature.p7s`,
  `.signature.zip`.
* The UTF-16 string heap in the same region holds the policy prose the original
  prints in verbose mode: `SignatureIntegrityPolicy`, `SignatureTrustPolicy`,
  `SignatureValidityPolicy`, `ManifestEntriesIntegrityPolicy`,
  `ManifestPackageIntegrityPolicy`, `VsMarketplacePolicy`, `LibraryBuildPolicy`,
  `TrustedSignerPolicy`, plus an explicit
  `Signature archive entry count (<n>) exceeds the limit of <n>` message that
  pins the archive entry cap at 10 (return code 41).
* `@vscode/vsce-sign/src/main.js` (read out of `node_modules.asar` with
  `src/vscode_loongcode/asar.py`) documents all 27 return codes.
* The concrete package/manifest/signature format was taken from a real signed
  extension: the manifest is `{"package":{"size","digests":{"sha256"}},"entries":
  {"<base64 UTF-8 path>":{"size","digests":{"sha256"}}}}` with base64 (not hex)
  digests, serialized with no whitespace; the p7s is a detached CMS SignedData
  (SHA-256, RSA PKCS#1 v1.5) over the manifest bytes, with an RFC 3161
  timestamp token in the signer's unsigned attributes and, for some
  Microsoft-published extensions, a repository countersignature instead of a
  Marketplace publisher certificate.

One non-obvious encoding detail had to be recovered empirically. The signed
attributes (`[0] IMPLICIT SET OF Attribute`) are *not* DER-sorted in the
encoding Microsoft emits, but the RSA signature is computed over the **canonical
(sorted) re-encoding** with the tag changed from `0xA0` to `0x31`. Hashing the
bytes as they appear in the file fails; .NET's own `SignedCms` performs the same
canonicalization. Confirm it directly:

```
openssl dgst -sha256 -verify leaf.pub -signature sig  <retagged-and-sorted attrs>  # Verified OK
```

`SignedAttributesSetDer` in `Crypto/Cms.cs` therefore always holds the
canonical form.

## CLI

```
vsce-sign <command> [options]

  verify           --package <vsix> --signaturearchive <sigzip>
  generatemanifest --package <vsix> [--output <manifest>] [--zip]
  zip              --manifest <m> --signature <p7s> [--output <sigzip>] [--zip]
  -h, --help
```

Options accept `--name value` and `--name=value`, are case-insensitive, and
`generatemanifest`/`zip` default their output to `.signature.manifest` /
`.signature.zip` next to the input, like the original. `--verbose` writes
diagnostics to stderr **and** mirrors them to stdout, because the JavaScript
wrapper only captures stdout when it asks for verbose output. `--help` exits 0
and prints the usage plus the return-code table. Unknown options and commands
exit 4.

## Return codes

| Code | Name | Emitted when |
| ---: | --- | --- |
| 0 | Success | everything verified |
| 3 | RequiredArgumentMissing | no command, or a required option is absent |
| 4 | InvalidArgument | unknown command or option |
| 5 | PackageIsUnreadable | the vsix path does not exist or cannot be opened |
| 6 | UnhandledException | unexpected exception |
| 7 | SignatureManifestIsMissing | the sigzip has no `.signature.manifest` |
| 8 | SignatureManifestIsUnreadable | the manifest entry cannot be decompressed / is empty |
| 9 | SignatureIsMissing | the sigzip has no `.signature.p7s` |
| 10 | SignatureIsUnreadable | the p7s entry cannot be read / is empty |
| 11 | CertificateIsUnreadable | reserved; not reachable from `verify` |
| 12 | SignatureArchiveIsUnreadable | the sigzip path does not exist or cannot be opened |
| 13 | FileAlreadyExists | `generatemanifest`/`zip` output exists and `--zip` was not given |
| 14 | SignatureArchiveIsInvalidZip | the sigzip is not a ZIP archive |
| 15 | SignatureArchiveHasSameSignatureFile | `zip` given the same file for `--manifest` and `--signature` |
| 30 | PackageIntegrityCheckFailed | the whole-file size/sha256 disagrees with the manifest's `package` object |
| 31 | SignatureIsInvalid | the CMS signature, the `messageDigest` attribute, or the timestamp token does not verify |
| 32 | SignatureManifestIsInvalid | the manifest is not readable JSON, or lacks `package`/`entries`/digests |
| 33 | SignatureIntegrityCheckFailed | an unlisted package entry when `--requirenoextraneousentries` |
| 34 | EntryIsMissing | a manifest entry is not in the vsix |
| 35 | EntryIsTampered | a manifest entry's size or sha256 disagrees with the vsix |
| 36 | Untrusted | chain building fails, the root is not a trust anchor, or the Marketplace policy fails |
| 37 | CertificateRevoked | revocation checking is on and the chain is revoked |
| 38 | SignatureIsNotValid | EKU problems (no code-signing EKU, lifetime-signing EKU) |
| 39 | UnknownError | an output file could not be written |
| 40 | PackageIsInvalidZip | the vsix is not a ZIP archive |
| 41 | SignatureArchiveHasTooManyEntries | more than 10 entries in the sigzip |

## Verification semantics

`verify` performs, in order: package readable → sigzip readable → package is a
valid ZIP → sigzip is a valid ZIP → entry count ≤ 10 → manifest present and
readable → manifest parses → signature present and readable → signature and
trust → per-entry integrity → package-level integrity.

Per entry: the base64 key is decoded to the entry path, the entry must exist
(34), its length must equal `size` and its SHA-256 must equal the base64 digest
(35). The package-level check compares the *whole file's* size and SHA-256,
including the ZIP end-of-central-directory record, against `package` (30). The
manifest is parsed before the signature is checked (matching the original's
ordering) but nothing from it is trusted until the signature verifies.

**Extra entries in the vsix are tolerated** — the whole-file digest covers them,
and Microsoft's verifier does not reject them (`RequireNoExtraneousEntries`
defaults to false). `--requirenoextraneousentries` makes an unlisted entry
report 33. Every real Microsoft-published package we tested lists all of its
entries anyway; see "Validated against" below.

### Why a tampered byte reports 35 and not 33/30

The required tamper test flips one byte inside one entry while keeping the ZIP
valid. Exit code **35 (EntryIsTampered)** is produced, for a precise reason:
per-entry checks run *before* the package-level digest, so a manifest-listed
entry that no longer matches its recorded SHA-256 is reported as `EntryIsTampered`
rather than as a whole-file mismatch. Exit 30 only appears when the *file* size
or hash no longer matches, e.g. when bytes are appended outside the ZIP
structures (verified in the suite: `tampered entry -> 35`, `package size
mismatch -> 30`). Exit 33 requires `--requirenoextraneousentries` and an
unlisted entry, so it is not reachable from a byte flip inside a listed entry.

## Trust model

The reference binary embeds two sets of certificates, and both were extracted
from it rather than taken from the system store:

1. **`Resources/trusted-roots.pem` — 376 certificates.** This is a verbatim copy
   of the PEM certificate bundle that the Microsoft binary carries (the
   `CodeSigningFallbackCertificateBundle` and `TimestampingFallbackCertificateBundle`
   resources referenced by its chain-building strings, which also mention
   `/etc/pki/ca-trust/extracted/pem/objsign-ca-bundle.pem`). It includes the
   commercial roots Microsoft ships with (`DigiCert Trusted Root G4`, the
   `Microsoft Root Certificate Authority 2010/2011` and `Microsoft Time Stamp
   Root Certificate Authority 2014` used by the timestamp chain, Symantec,
   Amazon, ...). These are the anchors used for **every** chain build.
2. **`Resources/publisher-certs.pem` — 9 certificates.** The
   `Microsoft.VisualStudio.Extensions.Signing.Resources.Certificates.*.cer`
   resources compiled into the same binary (file names are their SHA-256
   thumbprints). They are the Marketplace publisher certificates
   (`CN=Microsoft Corporation, OU=Visual Studio Marketplace[, (PPE)]`) and the
   `DigiCert Trusted Root G4` root. They are recorded for reference and for
   pinning analysis; the implementation uses them only to identify Marketplace
   publisher identities, not as additional anchors.

Chain building uses `X509Chain` in `CustomRootTrust` mode with those anchors, the
signature's own certificates as the untrusted intermediate store, and
`RevocationMode = NoCheck`. All the original binary's policies are variations on
this single chain build:

* **SignatureTrustPolicy** — the publisher certificate must chain to an anchor.
  One deliberate refinement: like Microsoft's signer, the chain is evaluated at
  the **timestamp's generation time** when the signature carries a valid RFC 3161
  token (checking SHA-256 message imprint == SHA-256 of the primary signature
  value, the token's own signature, and its timestamping EKU). This matters
  because the Marketplace publisher certificates are short-lived (e.g. serial
  `0AC14A1D...` expired 2026-08-17) and the fixture was signed 2024-12-05; a
  naive check at "now" reports `Untrusted`/expired for a perfectly good package.
* **Timestamp trust** — the timestamp signer's chain is built the same way, at
  the token's `genTime`, against the same anchor set.
* **VsMarketplacePolicy** — the primary signature must be a Marketplace
  publisher certificate (`OU=Visual Studio Marketplace` / `(PPE)`), *or* carry a
  repository countersignature (`1.2.840.113549.1.9.6`) whose own certificate is a
  Marketplace certificate, whose signature over the primary signature value
  verifies, and whose chain builds at its own timestamp. This second form is
  what Microsoft now ships for `ms-vscode.cpptools` and `ms-python.python`.
* **SignatureValidityPolicy** — code-signing EKU required, lifetime-signing EKU
  rejected (38), and the publisher certificate must be currently valid only with
  `--requirepublishercertificatebetimevalid`.
* **TrustedSignerPolicy** — the "not a Microsoft publisher" check is the same
  chain build evaluated against the anchor set, so it needs no separate root
  list; a non-anchored root already fails earlier with 36.
* **ManifestEntriesIntegrityPolicy / ManifestPackageIntegrityPolicy** — the
  digest checks described above.

Decision: **embedded Microsoft roots** (extracted from the reference binary),
not `/etc/ssl/certs/ca-certificates.crt`. The system bundle happens to contain
`DigiCert Trusted Root G4`, so using it would also work for the current fixture,
but it (a) diverges from what Microsoft's tool trusts, (b) cannot validate the
timestamp chain's Microsoft roots on a machine with a trimmed trust store, and
(c) would change behaviour whenever the host's `ca-certificates` package is
updated. The extracted bundle is pinned inside the binary instead.

`VSCE_SIGN_EXTRA_TRUST_ANCHORS` (colon-separated PEM files) can add anchors; it
is empty by default and exists only so the test suite can exercise the
integrity-code paths with a signature that is genuinely valid. It is documented
here so nobody mistakes it for part of Microsoft's model.

## Revocation (known limitation)

Revocation is **off by default**, exactly like the original: Microsoft's
`verify` only consults CRLs/OCSP with `--requirerevocationstatus`. With that flag
the implementation downloads each certificate's CRL distribution points, parses
the `CertificateList` and matches the certificate serial (→ 37
`CertificateRevoked`), and additionally queries OCSP responders for a
`Revoked` status. Network failures are logged but never turn into a revocation
verdict, and OCSP responses are not signature-verified. The parsing path is unit
tested against a real DigiCert CRL (no false positive on the genuine chain) and
against a synthetic CRL that revokes its own leaf (correctly detected); see
"Known limitations".

## Known limitations

* **`sign-pkg` is not implemented** (not required by VS Code's verification
  path).
* **Revocation**: CRL/OCSP support exists behind `--requirerevocationstatus` but
  is not exercised end-to-end against a live revoked Microsoft certificate (no
  such fixture exists publicly). OCSP responses are parsed for status only, not
  cryptographically validated, and no CRL/OCSP caching or timeout tuning beyond
  a 15 s per-request budget.
* **Countersignature trust** is evaluated at the countersignature's own
  timestamp when present, otherwise at the current time; the original's exact
  precedence between publisher and repository timestamps was not observed in
  enough samples to be certain.
* **`generatemanifest`** hashes entries exactly like the original — the output
  for the reference fixture is **byte-identical** to Microsoft's manifest
  (`cmp` clean) — but it does not implement `--certificate`, `--hashalgorithm`
  or `--timestampservice` (accepted and ignored, since generating a *signed*
  manifest requires Microsoft's signing service).
* **`zip`** writes a two-entry archive and reports 15 only for identical input
  paths; the original may detect a pre-existing identical signature inside the
  archive in more cases.
* Only the `verify` path was cross-checked against real Microsoft output at
  scale (3 packages, 21-2419 entries each). `--isppe`,
  `--requireonlypublishersignatureandtimestamp` and `--additionalpolicy` are
  parsed but only partially modelled.
* Manifest JSON parsing is strict about the documented shape; a manifest using
  `sha384`/`sha512` digests is rejected with 32 rather than verified.

## Layout

```
src-tools/vsce-sign-cs/
  build.sh                     NativeAOT publish helper
  verify.sh                    assertion suite (35 cases)
  src/VsceSign/
    Program.cs                 entry point, dispatch, exit-code mapping
    CommandLine.cs             grammar, aliases, usage text
    ReturnCode.cs              the 27 documented codes
    Verifier.cs                verify / generatemanifest / zip
    Manifest.cs                manifest model + source-generated JSON
    ZipSupport.cs              ZIP opening and whole-file hashing
    Crypto/Der.cs              ASN.1 helpers and OIDs
    Crypto/Cms.cs              CMS SignedData / SignerInfo parser
    Crypto/CmsSignature.cs     signature + messageDigest verification
    Crypto/ChainValidator.cs   X.509 chain building against the anchors
    Crypto/Timestamp.cs        RFC 3161 token parsing and imprint checks
    Crypto/RevocationChecker.cs CRL/OCSP status
    Crypto/TrustStore.cs       embedded anchors (the trust model)
    Resources/trusted-roots.pem      376 anchors from the reference binary
    Resources/publisher-certs.pem    9 publisher certificates from it
```

## Validated against

| Package | Signature | Result |
| --- | --- | --- |
| `vscodevim.vim` 1.29.0 (`ext.vsix`, 743 531 B, 21 entries) | Marketplace publisher certificate, timestamped 2024-12-05 | 0 |
| `ms-python.python` 2026.11.20260601 (9 359 508 B, 2 419 entries) | non-Marketplace publisher + repository countersignature | 0 |
| `ms-vscode.cpptools` 1.35.6 (142 263 364 B) | same countersignature form | 0 |

All three also pass with `--verbose`. The reference x64 binary could not be run
for a differential comparison (no `box64`/qemu path succeeds: it aborts with
`Failed to create CoreCLR` before reaching the CLI), so equivalence rests on the
static contract (command names, option table, diagnostic strings, return-code
table from the JS wrapper) and on the byte-identical manifest.
