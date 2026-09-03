#!/usr/bin/env bash
set -euo pipefail

VERSION=3.4.1
SHA256=6fb8c8c849e09593b509a9df1aaddb94b8187f65bb217ff707c8252fddd79e2f
URL="https://www.cril.univ-artois.fr/~roussel/runsolver/runsolver-${VERSION}.tar.bz2"
# TU Wien's VPN DNS may not resolve this public host. This is only a fallback;
# the archive checksum below remains authoritative if the address ever changes.
FALLBACK_ADDRESS=193.49.115.72
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
OUTPUT=${1:-"$REPO_DIR/runsolver-build/runsolver"}

if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    echo "This helper currently builds an x86-64 Linux runsolver binary." >&2
    exit 1
fi

for command in curl sha256sum tar make g++ install; do
    command -v "$command" >/dev/null || {
        echo "Missing build command: $command" >&2
        exit 1
    }
done

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/slum-runsolver-build.XXXXXXXX")
trap 'rm -rf -- "$TEMP_DIR"' EXIT

echo "Downloading runsolver $VERSION..." >&2
if ! curl --fail --location --retry 1 \
    --output "$TEMP_DIR/runsolver.tar.bz2" "$URL"; then
    echo "Normal DNS failed; retrying the verified source via its current address..." >&2
    curl --fail --location \
        --resolve "www.cril.univ-artois.fr:443:$FALLBACK_ADDRESS" \
        --output "$TEMP_DIR/runsolver.tar.bz2" "$URL"
fi
printf '%s  %s\n' "$SHA256" "$TEMP_DIR/runsolver.tar.bz2" | sha256sum --check --status
tar -xjf "$TEMP_DIR/runsolver.tar.bz2" -C "$TEMP_DIR"

# The 3.4.1 NUMA build does not compile with some current libnuma headers.
# SluM relies on Slurm for CPU placement, so build the portable non-NUMA form.
make -C "$TEMP_DIR/runsolver/src" clean
make -C "$TEMP_DIR/runsolver/src" -j"$(nproc)" \
    CFLAGS="-std=c++11 -Dtmpdebug -Wall -DVERSION=\\\"$VERSION\\\" -DSVNVERSION=\\\"4412\\\" -DWSIZE=64" \
    LDFLAGS="-static -Wl,--build-id" \
    LIBS= \
    runsolver

mkdir -p -- "$(dirname -- "$OUTPUT")"
install -m 0755 -- "$TEMP_DIR/runsolver/src/runsolver" "$OUTPUT"
runner_help=$("$OUTPUT" 2>&1 || true)
if ! grep -q -- '--rss-swap-limit' <<< "$runner_help"; then
    echo "Built runsolver failed its feature check." >&2
    exit 1
fi
echo "Built $OUTPUT" >&2
