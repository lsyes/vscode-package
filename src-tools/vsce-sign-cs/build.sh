#!/bin/sh
# Builds the loongarch64 NativeAOT vsce-sign binary.
#
# Requirements: a .NET SDK that ships the linux-loongarch64 NativeAOT packs
# (runtime.linux-loongarch64.Microsoft.DotNet.ILCompiler and
# Microsoft.NETCore.App.Runtime.NativeAOT.linux-loongarch64). No clang or
# binutils cross toolchain is needed beyond the native loongarch64 ones.
set -eu

DOTNET_ROOT=${DOTNET_ROOT:-/tmp/dotnet-la}
DOTNET=${DOTNET:-$DOTNET_ROOT/dotnet}
OUT=${OUT:-$(cd "$(dirname "$0")" && pwd)/out}

export DOTNET_ROOT
"$DOTNET" publish "$(dirname "$0")/src/VsceSign/VsceSign.csproj" \
    -c Release \
    -r linux-loongarch64 \
    -o "$OUT" \
    -p:PublishAot=true \
    -p:StripSymbols=true \
    -p:InvariantGlobalization=true

# The .dbg file is a separate symbol file, not a runtime dependency; the
# executable itself is fully self contained.
echo "built: $OUT/vsce-sign"
file "$OUT/vsce-sign"
