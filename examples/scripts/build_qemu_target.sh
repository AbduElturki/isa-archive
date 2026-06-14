#!/usr/bin/env bash
#
# build_qemu_target.sh — Prove the ISA-Archive YAML → QEMU CPU target pipeline.
#
# What this does:
#   1. `isa-archive generate -t qemu` — generates ALL files (ISA + glue + machine)
#   2. Clones QEMU v9.2.0 (once, shallow) if not already present
#   3. `patch_qemu.sh` — copies generated tree into QEMU source + patches 5 lines
#   4. Configures QEMU for {isa}-softmmu only
#   5. Builds with ninja
#
# Usage:
#   bash build_qemu_target.sh [BUILD_DIR]
#   BUILD_DIR=/tmp/rv32-qemu-target bash build_qemu_target.sh
#
# After building, run the test suite:
#   RV32_QEMU_BIN=$BUILD_DIR/build/qemu-system-rv32i \
#     uv run pytest examples/scripts/test_qemu_target.py -v

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
BUILD_DIR="${1:-${BUILD_DIR:-/tmp/rv32-qemu-target}}"
ISA_YAML="$SCRIPTS_DIR/../rv32/base/isa.yaml"
QEMU_SRC="$BUILD_DIR/qemu-src"
QEMU_BUILD="$BUILD_DIR/build"
QEMU_TAG="v9.2.0"
GEN_DIR="$BUILD_DIR/isa-generated"

# ── Prerequisites ────────────────────────────────────────────────────────────

check_tool() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: '$1' not found.  $2" >&2; exit 1
    fi
}

check_tool meson   "Install with: brew install meson"
check_tool ninja   "Install with: brew install ninja"
check_tool python3 "Install with: brew install python3"
check_tool git     "git is required"

python3 -c "import tomli" 2>/dev/null || pip3 install --quiet tomli

echo "=== ISA-Archive → QEMU CPU Target Build ==="
echo "BUILD_DIR : $BUILD_DIR"
echo "QEMU      : $QEMU_TAG"
echo ""

# ── Step 1: Generate complete QEMU target from YAML ─────────────────────────

echo "[1/5] Generating complete QEMU target from YAML..."
mkdir -p "$GEN_DIR"
uv --directory "$REPO_ROOT" run isa-archive generate \
    --isa "$ISA_YAML" -t qemu -o "$GEN_DIR"

echo "      Generated: $(find "$GEN_DIR" -type f | wc -l | tr -d ' ') files"
echo "      target/rv32i/ : $(ls "$GEN_DIR/target/rv32i/" | wc -l | tr -d ' ') files"
echo "      hw/rv32i/     : $(ls "$GEN_DIR/hw/rv32i/" | wc -l | tr -d ' ') files"

# ── Step 2: Clone QEMU ───────────────────────────────────────────────────────

if [ ! -d "$QEMU_SRC/.git" ]; then
    echo "[2/5] Cloning QEMU $QEMU_TAG (shallow)..."
    git clone --depth=1 --branch "$QEMU_TAG" \
        https://github.com/qemu/qemu.git "$QEMU_SRC"
else
    echo "[2/5] Using existing QEMU source at $QEMU_SRC"
fi

# ── Step 3: Integrate generated tree into QEMU ──────────────────────────────

echo "[3/5] Integrating generated files into QEMU source tree..."
bash "$GEN_DIR/patch_qemu.sh" "$QEMU_SRC"

# ── Step 4: Configure ────────────────────────────────────────────────────────

echo "[4/5] Configuring QEMU (rv32i-softmmu only)..."
mkdir -p "$QEMU_BUILD"
(cd "$QEMU_BUILD" && "$QEMU_SRC/configure" \
    --prefix="$BUILD_DIR/install" \
    --target-list=rv32i-softmmu \
    --disable-docs \
    --disable-werror \
    --extra-cflags="-Wno-unused-function -Wno-unused-variable" \
    2>&1 | tail -5)

# ── Step 5: Build ────────────────────────────────────────────────────────────

NPROC=$(python3 -c "import os; print(os.cpu_count())")
echo "[5/5] Building with ninja -j$NPROC ..."
ninja -C "$QEMU_BUILD" -j"$NPROC"

BINARY="$QEMU_BUILD/qemu-system-rv32i"
echo ""
echo "=== Build complete ==="
echo "Binary : $BINARY"
echo ""
echo "Run tests:"
echo "  RV32_QEMU_BIN=$BINARY \\"
echo "    uv run pytest $SCRIPTS_DIR/test_qemu_target.py -v"
