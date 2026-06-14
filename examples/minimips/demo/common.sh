#!/usr/bin/env bash
# common.sh — shared variables and helpers for the minimips end-to-end demo.
#
# minimips proves the pipeline is not RISC-V-specific: a different register file
# (r0..r31, MIPS ABI names) and a different constant-materialization strategy
# (LUI+ORI / hi_lo_or) drive the generated compiler and simulator. It reuses the
# riscv32 triple + R_RISCV relocation path so the backend links and runs with the
# same toolchain.

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../../.." && pwd)"
ISA_YAML="$REPO_ROOT/examples/minimips/isa.yaml"

BUILD_DIR="${BUILD_DIR:-/tmp/minimips-demo}"
QEMU_BUILD_DIR="$BUILD_DIR/qemu"
LLVM_BUILD_DIR="$BUILD_DIR/llvm"

QEMU_SRC="$QEMU_BUILD_DIR/qemu-src"
QEMU_BIN="$QEMU_BUILD_DIR/build/qemu-system-minimips"

LLVM_SRC="$LLVM_BUILD_DIR/llvm-src"
CLANG="$LLVM_BUILD_DIR/build/bin/clang"
LLC="$LLVM_BUILD_DIR/build/bin/llc"

QEMU_TAG="v9.2.0"
LLVM_TAG="llvmorg-18.1.8"

check_tool() {
    local cmd="$1" hint="$2"
    command -v "$cmd" &>/dev/null || { echo "ERROR: '$cmd' not found. $hint" >&2; exit 1; }
}
