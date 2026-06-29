# pico32f - single-precision floating point

An extension layer (`extends: ../isa.yaml`) that gives pico32 a **second
register class** and a **hard-float ABI** - the case that exercises the
generator's multi-register-class and floating-point paths.

## What it adds

- **`fpr`** - 32 single-precision (`f32`) registers with their own ABI names
  (`fa0..fa3`, `fs0..fs1`, `ft0..ft2`).
- **FADD / FSUB / FMUL** - float arithmetic. The compiler infers the float
  operation from the behavior (`rd = rs1 + rs2`) because the operands live in
  the `f32` file - no separate "this is float" flag needed.
- **FLW / FSW** - move 32-bit values between memory and `fpr` (the address base
  is an ordinary `gpr`).
- A **hard-float calling convention**: `abi.fp_arg_registers` / `fp_ret_registers`
  pass and return floats in `fa*`.

## Why the gpr file is repeated

The loader inherits a base ISA's register files **only when the extension
declares none of its own** (`compiler/loader.py`). Because `fp/` introduces
`fpr`, it must also restate the inherited `gpr` file (and the `abi` block, to
add the `fp_*` registers). Everything else - schemas, instructions, constants,
the riscv32 triple, the machine - is inherited.

## Try it

```sh
# A compiler with a real float register class + FADD patterns:
uv run isa-archive generate -i isa.yaml -t llvm -o build/fp-llvm
grep -n 'FPR : RegisterClass\|ISD::FADD\|CCIfType<\[f32' \
    build/fp-llvm/llvm/lib/Target/PICO32F/*.td build/fp-llvm/llvm/lib/Target/PICO32F/*.cpp

# QEMU helpers that do real float math (f2u32(u2f32(a) + u2f32(b))):
uv run isa-archive generate -i isa.yaml -t qemu-isa -o build/fp-qemu
```

`programs/float.c` compiles to FLW + FMUL + FADD with the clang built from this
ISA (see [`../../scripts/02_build_llvm.sh`](../../scripts/02_build_llvm.sh)),
using the hard-float calling convention.

## Current boundaries

- No float↔int conversion (`FCVT`) or float compare/branch - out of scope for a
  minimal showcase, so `float.c` checks its result by bit pattern rather than
  printing it.
