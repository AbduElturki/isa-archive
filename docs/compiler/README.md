# LLVM: the generated compiler

`-t llvm` turns your YAML into a complete LLVM backend - built into LLVM, it
gives you a real `clang` and `llc` that compile C for your ISA. This page
explains what's generated and what "complete" means;
[roles-and-coverage.md](roles-and-coverage.md) covers how the generator knows
which instruction does what, and [build-and-use.md](build-and-use.md) walks
the build and the linking story.

## What `-t llvm` produces

```
build/
  llvm/lib/Target/{ISA}/      → drop into $LLVM/llvm/lib/Target/{ISA}/
    {ISA}.td, RegisterInfo.td, InstrInfo.td, CallingConv.td, …   (TableGen)
    ISelLowering.cpp, FrameLowering.cpp, MCTargetDesc/, …        (C++)
    COMPILER_COVERAGE.md      ← read this first, every time
  patch_llvm.sh               → one-shot integration script
  INTEGRATE.md                → step-by-step instructions
```

Sub-targets emit just a slice, handy for piping pieces into different trees:

- `-t llvm-tablegen` - only the `*.td` TableGen files.
- `-t llvm-backend` - only the C++ backend sources + CMake.
- `-t llvm-mc` - only `MCTargetDesc/` + `TargetInfo/` (the MC layer).

This is not a stub: register classes, instruction selection patterns, calling
convention, frame lowering, the assembler/disassembler (MC layer), and ELF
object emission are all generated from your manifests. The bundled
`examples/tutorial/pico32-part4` backend compiles `fib.c` with `-O1` and the
result runs on the generated QEMU - that's the proven path the
[tutorial](../../examples/tutorial/pico32-part3/README.md) reproduces for your
own ISA.

## What "complete" means: target profiles

A simulator executes anything. A C compiler has a longer shopping list - and
not every ISA wants to compile C. Your ISA declares its ambition:

```yaml
spec:
  compiler:
    profile: c-baremetal      # the default
```

| Profile | The contract | "Complete" requires |
|---|---|---|
| `c-baremetal` | compile freestanding C | full ALU, word load/store, eq/ne + ordering branches, call/return, stack adjustment, constant materialization - **plus `zero`/`ra`/`sp` aliases** |
| `kernel-only` | a compute target (GPU/NPU style) | nothing - the report is informational |
| `custom` | exactly what you say | the roles in `requires: [...]` |

Every `-t llvm` run writes `COMPILER_COVERAGE.md` scoring your ISA against
its profile, and `--strict` turns an incomplete backend into a generation
failure (good for CI). A stack-less accelerator like `examples/npu-probe`
is **COMPILER-COMPLETE** under `kernel-only` - completeness is measured
against *your* ambition, not against RISC-V.

## Register files and the compiler

Only register files the compiler can allocate become register classes:
float-typed files and integer files of the ISA's data width. Anything else -
1-bit predicates, 128-bit vectors, odd-width accumulators - remains
architectural state: it simulates fully in QEMU, but instructions touching it
are omitted from the compiler with a warning:

```
NPU_PROBE: instruction 'VADD' uses register file(s) vreg, which have no LLVM
register class; omitted from the LLVM backend
```

This is deliberate: such instructions are how you model accelerator
operations, and the simulator is their home today.

## Nothing is hardcoded

The generator never assumes names or values. Which instruction is the
"add", which registers hold arguments, how constants are materialized -
all of it comes from your YAML, via [roles](roles-and-coverage.md) and the
[ABI block](../yaml/isa.md#abi--the-calling-convention). An OR-based constant
idiom (`LUI`+`ORI`) yields the `hi_lo_or` strategy instead of pico32's
`LUI`+`ADDI` `hi_lo_add` - same generator, different YAML - and
[`examples/npu-probe`](../../examples/npu-probe/README.md) pushes further off
the pico32 path entirely (big-endian, vector registers, no stack).

## Current boundaries

- **Stack machines and accumulator machines** (one working register, operand
  stack) don't fit LLVM's register-allocation model as a parameter change -
  they'd need a different backend strategy. Today they get the full QEMU
  functional model and the [standalone assembler](../targets/assembler.md),
  not a C compiler. Use `profile: kernel-only` so the coverage report
  reflects that honestly.
- **Floating point** covers arithmetic, load/store, and the calling
  convention; int↔float conversions, float comparisons, and FP constant
  materialization aren't generated yet.
- **Addressing modes** beyond `base + immediate` and `base + register` fall
  back to custom lowering (listed in the coverage report).
- Instructions on non-class register files are simulator-only (above).
- The LLVM version story and the linking ceiling live in
  [build-and-use.md](build-and-use.md#current-boundaries) - read that before
  inventing your own relocations.
