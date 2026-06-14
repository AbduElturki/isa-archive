#!/usr/bin/env bash
# common.sh — shared config + helpers for the pico32 build/run convenience
# scripts. These automate exactly what the tutorial does by hand (docs in
# examples/tutorial/pico32-part*/README.md); the inline commands there remain
# the canonical reference.
#
# Defaults target the part-4 pico32 snapshot. To build a different ISA, export
# ISA_YAML and ISA_NAME before running, e.g.:
#   ISA_YAML=.../mul/isa.yaml ISA_NAME=pico32 bash 02_build_llvm.sh

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../../.." && pwd)"   # examples/tutorial/scripts → repo root

# The ISA manifest to build, and the name QEMU/LLVM use for the generated target.
# ISA_NAME must match the manifest's metadata.name (drives qemu-system-<name>,
# <name>-softmmu, <name>-virt, and the LLVM target).
ISA_YAML="${ISA_YAML:-$REPO_ROOT/examples/tutorial/pico32-part4/isa.yaml}"
ISA_NAME="${ISA_NAME:-pico32}"
PROGRAMS_DIR="${PROGRAMS_DIR:-$(dirname "$ISA_YAML")/programs}"

BUILD_DIR="${BUILD_DIR:-/tmp/${ISA_NAME}-demo}"
QEMU_BUILD_DIR="$BUILD_DIR/qemu"
LLVM_BUILD_DIR="$BUILD_DIR/llvm"

QEMU_SRC="$QEMU_BUILD_DIR/qemu-src"
QEMU_TARGET="${ISA_NAME}-softmmu"
QEMU_MACHINE="${ISA_NAME}-virt"
QEMU_BIN="$QEMU_BUILD_DIR/build/qemu-system-${ISA_NAME}"

LLVM_SRC="$LLVM_BUILD_DIR/llvm-src"
CLANG="$LLVM_BUILD_DIR/build/bin/clang"
LLC="$LLVM_BUILD_DIR/build/bin/llc"

QEMU_TAG="v9.2.0"
LLVM_TAG="llvmorg-18.1.8"

check_tool() {
    local cmd="$1" hint="$2"
    command -v "$cmd" &>/dev/null || { echo "ERROR: '$cmd' not found. $hint" >&2; exit 1; }
}
