#!/usr/bin/env bash
#
# 03_run_demo.sh — Step 3: compile programs and run on the generated QEMU sim
#
# Prerequisites:
#   bash 01_build_qemu.sh   (builds qemu-system-rv32i)
#   bash 02_build_llvm.sh   (builds clang for rv32i)
#
# Usage:
#   bash 03_run_demo.sh [BUILD_DIR]

set -euo pipefail
source "$(dirname "$0")/common.sh"
[ -n "${1:-}" ] && BUILD_DIR="$1" \
    && QEMU_BIN="$BUILD_DIR/qemu/build/qemu-system-rv32i" \
    && CLANG="$BUILD_DIR/llvm/build/bin/clang"

PROGRAMS_DIR="$(dirname "$0")/programs"
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

CFLAGS=(
    --target=riscv32-unknown-elf
    -march=rv32i
    -mabi=ilp32
    -nostdlib
    -ffreestanding
    -O1
    -T "$PROGRAMS_DIR/link.ld"
)

echo "=== Step 3: Compile & Run ==="
echo "clang  : $CLANG"
echo "QEMU   : $QEMU_BIN"
echo ""

# ── fib(10) == 55 ────────────────────────────────────────────────────────────

echo "[1/2] Compiling fib.c ..."
"$CLANG" "${CFLAGS[@]}" \
    "$PROGRAMS_DIR/start.c" "$PROGRAMS_DIR/fib.c" \
    -o "$OUT_DIR/fib.elf"

echo "      Running on generated QEMU ..."
set +e
"$QEMU_BIN" -M rv32i-virt -display none -monitor null -bios none \
    -kernel "$OUT_DIR/fib.elf" 2>/dev/null
FIB_EXIT=$?
set -e

if [ $FIB_EXIT -eq 0 ]; then
    echo "      fib(10) == 55:  PASS"
else
    echo "      fib(10) == 55:  FAIL (exit $FIB_EXIT)"
fi

# ── Hello, rv32i! ─────────────────────────────────────────────────────────────

echo ""
echo "[2/2] Compiling hello.c ..."
"$CLANG" "${CFLAGS[@]}" \
    "$PROGRAMS_DIR/start.c" "$PROGRAMS_DIR/hello.c" \
    -o "$OUT_DIR/hello.elf"

echo "      Running on generated QEMU ..."
HELLO_OUT=$("$QEMU_BIN" -M rv32i-virt -display none -monitor null -bios none \
    -kernel "$OUT_DIR/hello.elf" -serial stdio 2>/dev/null || true)
echo "      Output: $HELLO_OUT"

if [ "$HELLO_OUT" = "Hello, rv32i!" ]; then
    echo "      UART output:    PASS"
else
    echo "      UART output:    FAIL (got: '$HELLO_OUT')"
fi

echo ""
echo "=== Demo complete ==="
