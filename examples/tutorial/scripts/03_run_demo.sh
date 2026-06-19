#!/usr/bin/env bash
#
# 03_run_demo.sh — Step 3: compile a program and run it on the generated sim
#
# Prerequisites:
#   bash 01_build_qemu.sh   (builds qemu-system-<isa>)
#   bash 02_build_llvm.sh   (builds clang for <isa>)
#
# Usage:
#   bash 03_run_demo.sh [BUILD_DIR]

set -euo pipefail
# Honor a pre-set $CLANG (e.g. CI using the distro clang, which supports the
# riscv32/rv32i target these CFLAGS request) so the demo can run without the
# ~40-min generated-LLVM build; otherwise fall back to the generated clang.
CLANG_OVERRIDE="${CLANG:-}"
source "$(dirname "$0")/common.sh"
[ -n "${1:-}" ] && BUILD_DIR="$1" \
    && QEMU_BIN="$BUILD_DIR/qemu/build/qemu-system-${ISA_NAME}" \
    && CLANG="$BUILD_DIR/llvm/build/bin/clang"
[ -n "$CLANG_OVERRIDE" ] && CLANG="$CLANG_OVERRIDE"

OUT_DIR="$BUILD_DIR/programs"
mkdir -p "$OUT_DIR"

if [ ! -x "$QEMU_BIN" ]; then
    echo "ERROR: QEMU binary not found at $QEMU_BIN" >&2
    echo "       Run: bash 01_build_qemu.sh" >&2; exit 1
fi
if [ ! -x "$CLANG" ]; then
    echo "ERROR: clang not found at $CLANG" >&2
    echo "       Run: bash 02_build_llvm.sh" >&2; exit 1
fi

# pico32 reuses the riscv32 triple + ilp32 ABI (see the part-3 tutorial).
CFLAGS=(
    --target=riscv32-unknown-elf
    -march=rv32i
    -mabi=ilp32
    -nostdlib
    -ffreestanding
    -O1
    -fuse-ld=lld
    -T "$PROGRAMS_DIR/link.ld"
)

echo "=== Step 3: Compile & Run ==="
echo "clang  : $CLANG"
echo "QEMU   : $QEMU_BIN ($QEMU_MACHINE)"
echo ""

# ── fib(10) == 55 ────────────────────────────────────────────────────────────

echo "[1/1] Compiling fib.c ..."
"$CLANG" "${CFLAGS[@]}" \
    "$PROGRAMS_DIR/start.c" "$PROGRAMS_DIR/fib.c" \
    -o "$OUT_DIR/fib.elf"

echo "      Running on generated QEMU ..."
FIB_OUT=$("$QEMU_BIN" -M "$QEMU_MACHINE" -display none -monitor none -bios none \
    -serial stdio -kernel "$OUT_DIR/fib.elf" 2>/dev/null || true)
echo "      Output: $FIB_OUT"

if [ "$FIB_OUT" = "fib(10) = 55" ]; then
    echo "      fib(10) = 55:  PASS"
else
    echo "      fib(10) = 55:  FAIL (got: '$FIB_OUT')"
    echo ""
    echo "=== Demo FAILED ==="
    exit 1
fi

echo ""
echo "=== Demo complete ==="
