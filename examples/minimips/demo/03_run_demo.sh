#!/usr/bin/env bash
#
# 03_run_demo.sh — Step 3: compile programs with the generated minimips clang
# and run them on the generated qemu-system-minimips.
#
# Prerequisites:
#   bash 01_build_qemu.sh   (builds qemu-system-minimips)
#   bash 02_build_llvm.sh   (builds clang for minimips)

set -euo pipefail
source "$(dirname "$0")/common.sh"
[ -n "${1:-}" ] && BUILD_DIR="$1" \
    && QEMU_BIN="$BUILD_DIR/qemu/build/qemu-system-minimips" \
    && CLANG="$BUILD_DIR/llvm/build/bin/clang"

PROGRAMS_DIR="$(dirname "$0")/../programs"
OUT_DIR="$BUILD_DIR/programs"
mkdir -p "$OUT_DIR"

[ -x "$QEMU_BIN" ] || { echo "ERROR: QEMU not found at $QEMU_BIN — run 01_build_qemu.sh" >&2; exit 1; }
[ -x "$CLANG" ]    || { echo "ERROR: clang not found at $CLANG — run 02_build_llvm.sh" >&2; exit 1; }

# minimips registers under the riscv32 triple (reusing the proven relocation path).
CFLAGS=(
    --target=riscv32-unknown-elf
    -march=rv32i
    -mabi=ilp32
    -nostdlib
    -ffreestanding
    -O1
    -T "$PROGRAMS_DIR/link.ld"
)

echo "=== Step 3: Compile & Run (minimips) ==="
echo "clang  : $CLANG"
echo "QEMU   : $QEMU_BIN"
echo ""

echo "[1/2] Compiling fib.c ..."
"$CLANG" "${CFLAGS[@]}" \
    "$PROGRAMS_DIR/start.c" "$PROGRAMS_DIR/fib.c" \
    -o "$OUT_DIR/fib.elf"

echo "      Running on generated QEMU ..."
set +e
"$QEMU_BIN" -M minimips-virt -display none -monitor null -bios none \
    -kernel "$OUT_DIR/fib.elf" 2>/dev/null
FIB_EXIT=$?
set -e
[ $FIB_EXIT -eq 0 ] && echo "      fib(10) == 55:  PASS" || echo "      fib(10) == 55:  FAIL (exit $FIB_EXIT)"

echo ""
echo "[2/2] Compiling hello.c ..."
"$CLANG" "${CFLAGS[@]}" \
    "$PROGRAMS_DIR/start.c" "$PROGRAMS_DIR/hello.c" \
    -o "$OUT_DIR/hello.elf"

echo "      Running on generated QEMU ..."
HELLO_OUT=$("$QEMU_BIN" -M minimips-virt -display none -monitor null -bios none \
    -kernel "$OUT_DIR/hello.elf" -serial stdio 2>/dev/null || true)
echo "      Output: $HELLO_OUT"
[ "$HELLO_OUT" = "Hello, minimips!" ] && echo "      UART output:    PASS" || echo "      UART output:    FAIL (got: '$HELLO_OUT')"

echo ""
echo "=== minimips demo complete ==="
