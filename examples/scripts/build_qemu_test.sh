#!/usr/bin/env bash
# Build a test binary that links directly against the ISA-Archive QEMU-generator
# output (rv32i_helpers.c) to prove the generated helpers are correct.
#
# Usage: bash build_qemu_test.sh [BUILD_DIR]
# Default BUILD_DIR: /tmp/rv32-qemu-test
#
# What this does:
#   1. Runs `isa-archive generate -t qemu-isa` to produce the ISA semantics files
#   2. Compiles rv32i_helpers.c (the generated file) with QEMU API shims
#   3. Links it with qemu_test_harness.c (the decode loop + main)
#
# The resulting binary accepts the same flat-binary format used by test_sim.py.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPTS_DIR/../.." && pwd)"
BUILD_DIR="${1:-${BUILD_DIR:-/tmp/rv32-qemu-test}}"
ISA_YAML="$SCRIPTS_DIR/../rv32/base/isa.yaml"
SHIMS="$SCRIPTS_DIR/qemu_shims"

echo "[1/2] Generating ISA semantics from $ISA_YAML ..."
uv --directory "$REPO_ROOT" run isa-archive generate \
    --isa "$ISA_YAML" \
    -t qemu-isa \
    -o "$BUILD_DIR"

echo "[2/2] Compiling $BUILD_DIR/rv32i_helpers.c + qemu_test_harness.c ..."
cc -O2 -std=c11 -Wno-parentheses-equality \
    -I"$BUILD_DIR" \
    -I"$SHIMS" \
    "$BUILD_DIR/rv32i_helpers.c" \
    "$SCRIPTS_DIR/qemu_test_harness.c" \
    -o "$BUILD_DIR/rv32i_qemu_test"

echo ""
echo "Test binary: $BUILD_DIR/rv32i_qemu_test"
echo "Run tests:   RV32_SIM=$BUILD_DIR/rv32i_qemu_test uv run pytest $SCRIPTS_DIR/test_sim.py -v"
