#!/usr/bin/env bash
#
# 02_build_llvm.sh — Step 2: YAML → clang compiler for <isa>
#
# Generates a complete LLVM backend from the ISA YAML, clones LLVM 18,
# integrates the generated files, and builds clang + llc. Defaults to the
# part-4 pico32 snapshot (see common.sh to point it elsewhere).
#
# Usage:
#   bash 02_build_llvm.sh [BUILD_DIR]
#   BUILD_DIR=/tmp/my-pico32 bash 02_build_llvm.sh
#
# After building:
#   $BUILD_DIR/llvm/build/bin/clang --version

set -euo pipefail
source "$(dirname "$0")/common.sh"
[ -n "${1:-}" ] && BUILD_DIR="$1" && LLVM_BUILD_DIR="$BUILD_DIR/llvm" && LLVM_SRC="$LLVM_BUILD_DIR/llvm-src" && CLANG="$LLVM_BUILD_DIR/build/bin/clang"

check_tool cmake  "brew install cmake"
check_tool ninja  "brew install ninja"
check_tool git    "git is required"
check_tool python3 "brew install python3"

echo "=== Step 2: YAML → LLVM Compiler ==="
echo "ISA       : $ISA_NAME ($ISA_YAML)"
echo "BUILD_DIR : $LLVM_BUILD_DIR"
echo "LLVM      : $LLVM_TAG"
echo ""

# ── 1/5: Generate LLVM backend from YAML ─────────────────────────────────────

GEN_DIR="$LLVM_BUILD_DIR/generated"
echo "[1/5] Generating LLVM backend from $ISA_YAML ..."
mkdir -p "$GEN_DIR"
uv --directory "$REPO_ROOT" run isa-archive generate \
    --isa "$ISA_YAML" -t llvm -o "$GEN_DIR"
echo "      Generated: $(find "$GEN_DIR" -type f | wc -l | tr -d ' ') files"

# ── 2/5: Clone LLVM ──────────────────────────────────────────────────────────

if [ ! -d "$LLVM_SRC/.git" ]; then
    echo "[2/5] Cloning LLVM $LLVM_TAG (shallow — this may take a few minutes) ..."
    git clone --depth=1 --branch "$LLVM_TAG" \
        https://github.com/llvm/llvm-project.git "$LLVM_SRC"
else
    echo "[2/5] Using existing LLVM source at $LLVM_SRC"
fi

# ── 3/5: Integrate generated backend ─────────────────────────────────────────

echo "[3/5] Integrating generated LLVM backend into source tree ..."
bash "$GEN_DIR/patch_llvm.sh" "$LLVM_SRC"

# ── 4/5: CMake configure ─────────────────────────────────────────────────────

LLVM_BUILD="$LLVM_BUILD_DIR/build"
# Derive the LLVM target name from the generated directory (avoids parsing YAML).
ISA_UPPER=$(ls "$GEN_DIR/llvm/lib/Target/" | head -1)

echo "[4/5] Configuring LLVM (target: $ISA_UPPER, with clang) ..."
# Remove stale build dir to avoid CMake "binary dir already used" conflicts
[ -f "$LLVM_BUILD/CMakeCache.txt" ] && rm -rf "$LLVM_BUILD"
cmake -S "$LLVM_SRC/llvm" -B "$LLVM_BUILD" \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_TARGETS_TO_BUILD="$ISA_UPPER" \
    -DLLVM_ENABLE_PROJECTS="clang" \
    -DLLVM_ENABLE_ASSERTIONS=OFF \
    -DLLVM_INCLUDE_TESTS=OFF \
    -DLLVM_INCLUDE_EXAMPLES=OFF \
    -DLLVM_INCLUDE_DOCS=OFF \
    2>&1 | tail -10

# ── 5/5: Build clang + llc ───────────────────────────────────────────────────

NPROC=$(python3 -c "import os; print(os.cpu_count())")
echo "[5/5] Building clang + llc with ninja -j$NPROC (this takes ~40 min) ..."
ninja -C "$LLVM_BUILD" -j"$NPROC" clang llc

echo ""
echo "=== LLVM build complete ==="
echo "clang : $CLANG"
"$CLANG" --version | head -1
echo ""
echo "Next: bash 03_run_demo.sh"
