#!/usr/bin/env bash
# Build and run the RV32I test suite on actual qemu-system-riscv32.
#
# This compiles a bare-metal C program with riscv64-elf-gcc targeting RV32I,
# loads it into qemu-system-riscv32 (-machine virt), and runs the tests.
# The same instruction categories are tested here as in test_sim.py.
#
# Prerequisites (installed by this script if missing):
#   brew install qemu riscv64-elf-gcc coreutils
#
# Usage: bash build_qemu_virt_test.sh [BUILD_DIR]

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${1:-${BUILD_DIR:-/tmp/rv32-qemu-virt}}"

GCC=riscv64-elf-gcc
QEMU=qemu-system-riscv32
LIBGCC="$(${GCC} -march=rv32i -mabi=ilp32 -print-libgcc-file-name 2>/dev/null)"

for tool in "$GCC" "$QEMU"; do
    if ! command -v "$tool" &>/dev/null; then
        echo "Missing: $tool  —  run: brew install qemu riscv64-elf-gcc" >&2
        exit 1
    fi
done

mkdir -p "$BUILD_DIR"

echo "[1/2] Compiling bare-metal RV32I test for qemu-system-riscv32 ..."
${GCC} -march=rv32i -mabi=ilp32 -O1 -nostdlib -ffreestanding \
    -T  "$SCRIPTS_DIR/qemu_virt.ld" \
    "$SCRIPTS_DIR/qemu_virt_start.S" \
    "$SCRIPTS_DIR/qemu_virt_test.c" \
    ${LIBGCC:+"$LIBGCC"} \
    -o "$BUILD_DIR/rv32i_virt_test.elf"

echo "[2/2] Running on qemu-system-riscv32 -machine virt ..."
${QEMU} \
    -machine virt \
    -bios none \
    -display none \
    -serial file:"$BUILD_DIR/test_output.txt" \
    -no-reboot \
    -kernel "$BUILD_DIR/rv32i_virt_test.elf" < /dev/null 2>/dev/null

cat "$BUILD_DIR/test_output.txt"
echo ""
echo "Run pytest:  RV32_QEMU_OUT=$BUILD_DIR/test_output.txt uv run pytest $SCRIPTS_DIR/test_qemu_virt.py -v"
