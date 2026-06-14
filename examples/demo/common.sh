#!/usr/bin/env bash
# common.sh — shared variables and helpers sourced by all demo scripts

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
ISA_YAML="$REPO_ROOT/examples/rv32/base/isa.yaml"

BUILD_DIR="${BUILD_DIR:-/tmp/rv32-demo}"
QEMU_BUILD_DIR="$BUILD_DIR/qemu"
LLVM_BUILD_DIR="$BUILD_DIR/llvm"

QEMU_SRC="$QEMU_BUILD_DIR/qemu-src"
QEMU_BIN="$QEMU_BUILD_DIR/build/qemu-system-rv32i"

LLVM_SRC="$LLVM_BUILD_DIR/llvm-src"
CLANG="$LLVM_BUILD_DIR/build/bin/clang"
LLC="$LLVM_BUILD_DIR/build/bin/llc"

QEMU_TAG="v9.2.0"
LLVM_TAG="llvmorg-18.1.8"

check_tool() {
    local cmd="$1" hint="$2"
    command -v "$cmd" &>/dev/null || { echo "ERROR: '$cmd' not found. $hint" >&2; exit 1; }
}
