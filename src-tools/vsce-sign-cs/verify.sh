#!/bin/sh
# Verification suite for the loongarch64 reimplementation of @vscode/vsce-sign.
#
#   ./verify.sh <path-to-vsce-sign> [<vsix> <sigzip>]
#
# Every case prints its return code and PASS/FAIL. The suite exits non-zero if
# any expectation is not met.

set -u

BIN=${1:-./out/vsce-sign}
VSIX=${2:-/tmp/vsix/ext.vsix}
SIGZIP=${3:-/tmp/vsix/vim.sigzip}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0

# expect <label> <expected-exit> <command...>
expect() {
    label=$1
    expected=$2
    shift 2
    printf '%-58s ' "$label"
    "$@" >"$WORK/stdout" 2>"$WORK/stderr"
    actual=$?
    if [ "$actual" -eq "$expected" ]; then
        printf 'exit=%s ok\n' "$actual"
        pass=$((pass + 1))
    else
        printf 'exit=%s FAIL (expected %s)\n' "$actual" "$expected"
        sed 's/^/    /' "$WORK/stderr" | head -5
        fail=$((fail + 1))
    fi
}

echo "binary: $BIN"
file "$BIN" | sed 's/^/    /'
echo

expect "help exits 0"                 0  "$BIN" --help
expect "no arguments -> 3"            3  "$BIN"
expect "unknown command -> 4"         4  "$BIN" frobnicate
expect "unknown option -> 4"          4  "$BIN" verify --nope

expect "verify missing package -> 3"  3  "$BIN" verify --signaturearchive "$SIGZIP"
expect "verify missing sigarchive -> 3" 3 "$BIN" verify --package "$VSIX"

expect "valid package verifies -> 0"  0  "$BIN" verify --package "$VSIX" --signaturearchive "$SIGZIP"
expect "valid package verifies (verbose) -> 0" 0 "$BIN" verify --package "$VSIX" --signaturearchive "$SIGZIP" --verbose

expect "missing package file -> 5"    5  "$BIN" verify --package "$WORK/nope.vsix" --signaturearchive "$SIGZIP"
echo "this is not a zip" > "$WORK/notazip.vsix"
expect "invalid package zip -> 40"    40 "$BIN" verify --package "$WORK/notazip.vsix" --signaturearchive "$SIGZIP"

expect "missing sigarchive file -> 12" 12 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/nope.sigzip"
echo "this is not a zip" > "$WORK/notazip.sigzip"
expect "invalid sigarchive zip -> 14" 14 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/notazip.sigzip"

# --- signature archive contents ---------------------------------------------
python3 - "$WORK" <<'PY'
import sys, zipfile, json, base64
work = sys.argv[1]
manifest = open('/tmp/vsix/sig/.signature.manifest', 'rb').read()
p7s = open('/tmp/vsix/sig/.signature.p7s', 'rb').read()

def make(name, entries):
    with zipfile.ZipFile(f'{work}/{name}', 'w', zipfile.ZIP_DEFLATED) as z:
        for entry_name, data in entries:
            z.writestr(entry_name, data)

make('no-manifest.sigzip', [('.signature.p7s', p7s)])
make('no-p7s.sigzip', [('.signature.manifest', manifest)])
make('garbage-manifest.sigzip', [('.signature.manifest', b'\x00\x01garbage'), ('.signature.p7s', p7s)])
make('empty-manifest.sigzip', [('.signature.manifest', b'{}'), ('.signature.p7s', p7s)])
make('many.sigzip', [(f'x{i}', b'x') for i in range(12)] + [('.signature.manifest', manifest), ('.signature.p7s', p7s)])

doc = json.loads(manifest)
doc['entries'][base64.b64encode(b'extension/missing.txt').decode()] = {
    'size': 5, 'digests': {'sha256': base64.b64encode(b'\x00' * 32).decode()}}
make('missing-entry.sigzip', [('.signature.manifest', json.dumps(doc, separators=(',', ':')).encode()), ('.signature.p7s', p7s)])

doc = json.loads(manifest)
doc['package']['size'] += 1
make('bad-size.sigzip', [('.signature.manifest', json.dumps(doc, separators=(',', ':')).encode()), ('.signature.p7s', p7s)])

doc = json.loads(manifest)
doc['package']['digests']['sha256'] = base64.b64encode(b'\x00' * 32).decode()
make('bad-digest.sigzip', [('.signature.manifest', json.dumps(doc, separators=(',', ':')).encode()), ('.signature.p7s', p7s)])
PY

expect "no .signature.manifest -> 7"  7  "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/no-manifest.sigzip"
expect "no .signature.p7s -> 9"       9  "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/no-p7s.sigzip"
expect "unreadable manifest -> 32"    32 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/garbage-manifest.sigzip"
expect "non-manifest JSON -> 32"      32 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/empty-manifest.sigzip"
expect "too many archive entries -> 41" 41 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/many.sigzip"

# --- tampering ---------------------------------------------------------------
python3 - "$WORK" <<'PY'
import sys, zipfile
work = sys.argv[1]
with zipfile.ZipFile('/tmp/vsix/ext.vsix') as zin, zipfile.ZipFile(f'{work}/tampered.vsix', 'w') as zout:
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename == 'extension/README.md':
            data = bytes([data[0] ^ 0x01]) + data[1:]
        zout.writestr(info, data)

# Same-size package: append junk after the ZIP structures, which no entry digest covers.
data = open('/tmp/vsix/ext.vsix', 'rb').read()
open(f'{work}/appended.vsix', 'wb').write(data + b'JUNK')
PY
expect "tampered entry -> 35"         35 "$BIN" verify --package "$WORK/tampered.vsix" --signaturearchive "$SIGZIP"
expect "package size mismatch -> 30"  30 "$BIN" verify --package "$WORK/appended.vsix" --signaturearchive "$SIGZIP"
expect "manifest entry missing -> 31 (signature no longer matches)" 31 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/missing-entry.sigzip"
expect "package size claim wrong -> 31" 31 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/bad-size.sigzip"

# --- trust -------------------------------------------------------------------
python3 - "$WORK" <<'PY'
# Re-sign the genuine manifest with a throwaway CA: the signature is
# cryptographically valid but the root is not a trust anchor.
import sys, os, subprocess, tempfile, zipfile
work = sys.argv[1]
d = tempfile.mkdtemp()
def run(*args):
    subprocess.run(args, check=True, capture_output=True)
run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
    '-keyout', f'{d}/root.key', '-out', f'{d}/root.pem', '-days', '30',
    '-subj', '/CN=Untrusted Root', '-addext', 'basicConstraints=critical,CA:TRUE',
    '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-keyout', f'{d}/leaf.key',
    '-out', f'{d}/leaf.csr', '-subj', '/C=US/O=Untrusted Corp/OU=Visual Studio Marketplace/CN=Untrusted Corp')
open(f'{d}/leaf.cnf', 'w').write(
    'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n')
run('openssl', 'x509', '-req', '-in', f'{d}/leaf.csr', '-CA', f'{d}/root.pem', '-CAkey', f'{d}/root.key',
    '-CAcreateserial', '-out', f'{d}/leaf.pem', '-days', '10', '-extfile', f'{d}/leaf.cnf')
run('openssl', 'cms', '-sign', '-binary', '-in', '/tmp/vsix/sig/.signature.manifest',
    '-signer', f'{d}/leaf.pem', '-inkey', f'{d}/leaf.key', '-certfile', f'{d}/root.pem',
    '-outform', 'DER', '-out', f'{d}/untrusted.p7s', '-md', 'sha256')
with zipfile.ZipFile(f'{work}/untrusted.sigzip', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('/tmp/vsix/sig/.signature.manifest', '.signature.manifest')
    z.write(f'{d}/untrusted.p7s', '.signature.p7s')
PY
expect "untrusted root -> 36"         36 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/untrusted.sigzip"

# A p7s whose signature bytes were altered (the manifest is intact) is invalid.
python3 - "$WORK" <<'PY'
import sys, zipfile
work = sys.argv[1]
p7s = bytearray(open('/tmp/vsix/sig/.signature.p7s', 'rb').read())
p7s[-1] ^= 0xff
with zipfile.ZipFile(f'{work}/bad-signature.sigzip', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('/tmp/vsix/sig/.signature.manifest', '.signature.manifest')
    z.writestr('.signature.p7s', bytes(p7s))
PY
expect "corrupted signature bytes -> 31" 31 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/bad-signature.sigzip"

# --- integrity policies, exercised with a valid signature --------------------
#
# To reach the package/entry integrity codes the signature itself must be valid,
# so these cases sign a modified manifest with a throwaway CA and extend the trust
# anchor set through VSCE_SIGN_EXTRA_TRUST_ANCHORS. That variable is empty by
# default; everything above ran with Microsoft's anchors only.
python3 - "$WORK" <<'PY'
import sys, os, subprocess, tempfile, zipfile, json, base64, hashlib
work = sys.argv[1]
d = tempfile.mkdtemp()
def run(*a): subprocess.run(a, check=True, capture_output=True)
run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', f'{d}/root.key',
    '-out', f'{d}/root.pem', '-days', '30', '-subj', '/CN=Test Root',
    '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-keyout', f'{d}/leaf.key',
    '-out', f'{d}/leaf.csr', '-subj', '/C=US/O=Test/OU=Visual Studio Marketplace/CN=Test')
open(f'{d}/leaf.cnf', 'w').write(
    'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning\n')
run('openssl', 'x509', '-req', '-in', f'{d}/leaf.csr', '-CA', f'{d}/root.pem', '-CAkey', f'{d}/root.key',
    '-CAcreateserial', '-out', f'{d}/leaf.pem', '-days', '10', '-extfile', f'{d}/leaf.cnf')

original = json.load(open('/tmp/vsix/sig/.signature.manifest'))

def sign(doc, name):
    body = json.dumps(doc, separators=(',', ':')).encode()
    open(f'{d}/{name}.manifest', 'wb').write(body)
    run('openssl', 'cms', '-sign', '-binary', '-in', f'{d}/{name}.manifest',
        '-signer', f'{d}/leaf.pem', '-inkey', f'{d}/leaf.key', '-certfile', f'{d}/root.pem',
        '-outform', 'DER', '-out', f'{d}/{name}.p7s', '-md', 'sha256')
    with zipfile.ZipFile(f'{work}/{name}.sigzip', 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(f'{d}/{name}.manifest', '.signature.manifest')
        z.write(f'{d}/{name}.p7s', '.signature.p7s')

def clone():
    return json.loads(json.dumps(original))

# A completely valid manifest + signature from the throwaway root: 0 when trusted.
sign(clone(), 'selftrusted')

# Entry digest wrong -> EntryIsTampered (35).
doc = clone()
first = next(iter(doc['entries']))
doc['entries'][first]['digests']['sha256'] = base64.b64encode(b'\x00' * 32).decode()
sign(doc, 'tampered-entry')

# Entry listed but absent from the package -> EntryIsMissing (34).
doc = clone()
doc['entries'][base64.b64encode(b'extension/absent.txt').decode()] = {
    'size': 1, 'digests': {'sha256': base64.b64encode(b'\x00' * 32).decode()}}
sign(doc, 'missing-entry-signed')

# Package digest wrong -> PackageIntegrityCheckFailed (30).
doc = clone()
doc['package']['digests']['sha256'] = base64.b64encode(b'\x00' * 32).decode()
sign(doc, 'package-digest')

# Package with an unlisted entry whose manifest correctly covers the rest,
# including the package digest -> tolerated unless --requirenoextraneousentries.
with zipfile.ZipFile('/tmp/vsix/ext.vsix') as zin, \
     zipfile.ZipFile(f'{work}/extra.vsix', 'w', zipfile.ZIP_DEFLATED) as zout:
    for info in zin.infolist():
        zout.writestr(info, zin.read(info.filename))
    zout.writestr('extension/unlisted.txt', b'hello')
data = open(f'{work}/extra.vsix', 'rb').read()
doc = clone()
doc['package']['size'] = len(data)
doc['package']['digests']['sha256'] = base64.b64encode(hashlib.sha256(data).digest()).decode()
sign(doc, 'extra-entry')

open(f'{work}/anchors.txt', 'w').write(f'{d}/root.pem')
PY

ANCHORS=$(cat "$WORK/anchors.txt")

expect_anchored() {
    label=$1
    expected=$2
    shift 2
    printf '%-58s ' "$label"
    VSCE_SIGN_EXTRA_TRUST_ANCHORS="$ANCHORS" "$@" >"$WORK/stdout" 2>"$WORK/stderr"
    actual=$?
    if [ "$actual" -eq "$expected" ]; then
        printf 'exit=%s ok\n' "$actual"
        pass=$((pass + 1))
    else
        printf 'exit=%s FAIL (expected %s)\n' "$actual" "$expected"
        sed 's/^/    /' "$WORK/stderr" | head -5
        fail=$((fail + 1))
    fi
}

expect_anchored "valid signature from an added anchor -> 0"  0 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/selftrusted.sigzip"
expect_anchored "entry digest mismatch -> 35"                35 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/tampered-entry.sigzip"
expect_anchored "manifest entry missing from package -> 34"  34 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/missing-entry-signed.sigzip"
expect_anchored "package digest mismatch -> 30"              30 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/package-digest.sigzip"
expect_anchored "unlisted package entry tolerated -> 0"       0 "$BIN" verify --package "$WORK/extra.vsix" --signaturearchive "$WORK/extra-entry.sigzip"
expect_anchored "unlisted package entry rejected -> 33"      33 "$BIN" verify --package "$WORK/extra.vsix" --signaturearchive "$WORK/extra-entry.sigzip" --requirenoextraneousentries
expect_anchored "marketplace countersignature required -> 36" 36 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/selftrusted.sigzip" --requirevsmprepositoryprimarysignatureorcountersignature

# --- generatemanifest / zip --------------------------------------------------
printf '%-58s ' "generatemanifest reproduces Microsoft's bytes"
"$BIN" generatemanifest --package "$VSIX" --output "$WORK/generated.manifest" >/dev/null 2>&1
if cmp -s "$WORK/generated.manifest" /tmp/vsix/sig/.signature.manifest; then
    echo "ok"
    pass=$((pass + 1))
else
    echo "FAIL"
    fail=$((fail + 1))
fi

expect "generatemanifest to existing file -> 13" 13 "$BIN" generatemanifest --package "$VSIX" --output "$WORK/generated.manifest"
expect "generatemanifest --zip overwrites -> 0" 0 "$BIN" generatemanifest --package "$VSIX" --output "$WORK/generated.manifest" --zip
expect "zip builds a signature archive -> 0"  0 "$BIN" zip --manifest /tmp/vsix/sig/.signature.manifest --signature /tmp/vsix/sig/.signature.p7s --output "$WORK/built.sigzip"
expect "the built archive verifies -> 0"      0 "$BIN" verify --package "$VSIX" --signaturearchive "$WORK/built.sigzip"

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] || exit 1
