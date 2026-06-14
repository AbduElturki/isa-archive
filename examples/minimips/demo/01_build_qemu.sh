#!/usr/bin/env bash
#
# 01_build_qemu.sh — Step 1: minimips YAML → qemu-system-minimips
#
# Usage:
#   bash 01_build_qemu.sh [BUILD_DIR]
#   BUILD_DIR=/tmp/my-mm bash 01_build_qemu.sh

set -euo pipefail
source "$(dirname "$0")/common.sh"
[ -n "${1:-}" ] && BUILD_DIR="$1" && QEMU_BUILD_DIR="$BUILD_DIR/qemu" && QEMU_SRC="$QEMU_BUILD_DIR/qemu-src" && QEMU_BIN="$QEMU_BUILD_DIR/build/qemu-system-minimips"

check_tool meson   "brew install meson"
check_tool ninja   "brew install ninja"
check_tool python3 "brew install python3"
check_tool git     "git is required"

echo "=== Step 1: minimips YAML → QEMU Simulator ==="
echo "BUILD_DIR : $QEMU_BUILD_DIR"
echo ""

GEN_DIR="$QEMU_BUILD_DIR/generated"
echo "[1/5] Generating QEMU target from $ISA_YAML ..."
mkdir -p "$GEN_DIR"
uv --directory "$REPO_ROOT" run isa-archive generate \
    --isa "$ISA_YAML" -t qemu -o "$GEN_DIR"
echo "      Generated: $(find "$GEN_DIR" -type f | wc -l | tr -d ' ') files"

if [ ! -d "$QEMU_SRC/.git" ]; then
    echo "[2/5] Cloning QEMU $QEMU_TAG (shallow) ..."
    git clone --depth=1 --branch "$QEMU_TAG" \
        https://github.com/qemu/qemu.git "$QEMU_SRC"
else
    echo "[2/5] Using existing QEMU source at $QEMU_SRC"
fi

echo "[3/5] Integrating generated files into QEMU source tree ..."
bash "$GEN_DIR/patch_qemu.sh" "$QEMU_SRC"

QEMU_BUILD="$QEMU_BUILD_DIR/build"
echo "[4/5] Configuring QEMU (minimips-softmmu only) ..."
mkdir -p "$QEMU_BUILD"
(cd "$QEMU_BUILD" && "$QEMU_SRC/configure" \
    --prefix="$QEMU_BUILD_DIR/install" \
    --target-list=minimips-softmmu \
    --disable-docs \
    --disable-werror \
    --extra-cflags="-Wno-unused-function -Wno-unused-variable" \
    2>&1 | tail -5)

NPROC=$(python3 -c "import os; print(os.cpu_count())")
echo "[5/5] Building with ninja -j$NPROC ..."
ninja -C "$QEMU_BUILD" -j"$NPROC"

echo ""
echo "=== QEMU build complete ==="
echo "Binary : $QEMU_BIN"
"$QEMU_BIN" --version | head -1
echo ""
echo "Next: bash 02_build_llvm.sh"
