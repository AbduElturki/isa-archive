# LLVM: the generated compiler

`-t llvm` turns your YAML into a complete LLVM backend - built into LLVM, it
gives you a real `clang` and `llc` that compile C for your ISA. This page
explains what's generated and what "complete" means;
[roles-and-coverage.md](roles-and-coverage.md) covers how the generator knows
which instruction does what, and [build-and-use.md](build-and-use.md) walks
the build and the linking story.

This is not a stub: register classes, instruction-selection patterns, calling convention, frame
lowering, the MC layer (encoder, assembler), and ELF object emission are all generated from your
manifests. The bundled `examples/tutorial/pico32-part4` backend compiles `fib.c` with `-O1` and the
result runs on the generated QEMU - the proven path the
[tutorial](../../../examples/tutorial/pico32-part3/README.md) reproduces for your own ISA.

## Files generated

Everything lands under `llvm/lib/Target/{ISA}/` (`{ISA}` = the PascalCase target prefix). The files
group by sub-target (`-t llvm` emits all; the slices below emit one group each).

**Always** (at the target root + output root):

| File | Purpose |
|---|---|
| `COMPILER_COVERAGE.md` | the role/coverage report - which C constructs lower, what's custom-lowered. Read it first, every time. |
| `.clang-format` | LLVM style for the generated C++ |
| `patch_llvm.sh` | one-shot script that copies the target into an LLVM checkout and registers it |
| `INTEGRATE.md` | step-by-step manual integration instructions |

**TableGen** (`-t llvm-tablegen`):

| File | Purpose |
|---|---|
| `{ISA}.td` | top-level include (pulls in the others; CPU/features) |
| `{ISA}RegisterInfo.td` | register classes (GPR, FPR, vector, …) |
| `{ISA}InstrFormats.td` · `{ISA}InstrInfo.td` | instruction format base classes; per-instruction defs + selection patterns |
| `{ISA}CallingConv.td` | argument/return register assignment |
| `{ISA}Schedule.td` | instruction latencies / throughput (from the uArch) |

**C++ backend** (`-t llvm-backend`):

| File | Purpose |
|---|---|
| `{ISA}.h` | target entry header (factories) |
| `{ISA}TargetMachine.{h,cpp}` | codegen pass pipeline |
| `{ISA}Subtarget.{h,cpp}` | per-CPU feature flags |
| `{ISA}RegisterInfo.{h,cpp}` | frame/stack register management, spilling |
| `{ISA}InstrInfo.{h,cpp}` | copy/branch/move handling, operand info |
| `{ISA}ISelLowering.{h,cpp}` | DAG legalization + custom lowering |
| `{ISA}ISelDAGToDAG.cpp` | instruction selection (DAG → instructions) |
| `{ISA}AsmPrinter.cpp` | assembly emission |
| `{ISA}FrameLowering.{h,cpp}` | prologue/epilogue, stack frame |
| `CMakeLists.txt` | build wiring |

**MC layer** (`-t llvm-mc`; under `MCTargetDesc/` and `TargetInfo/`):

| File | Purpose |
|---|---|
| `{ISA}MCTargetDesc.{h,cpp}` | MC backend registration |
| `{ISA}MCAsmInfo.{h,cpp}` | assembly syntax (comment string, endianness, directives) |
| `{ISA}FixupKinds.h` | relocation fixup kinds |
| `{ISA}MCCodeEmitter.cpp` | instruction → bytes encoder (incl. the >64-bit APInt path) |
| `{ISA}InstPrinter.{h,cpp}` | assembly mnemonic/operand printing |
| `{ISA}AsmBackend.cpp` | fixup application, NOP emission |
| `{ISA}ELFObjectWriter.cpp` | ELF relocation emission |
| `MCTargetDesc/CMakeLists.txt` | MC build wiring |
| `TargetInfo/{ISA}TargetInfo.{h,cpp}` · `TargetInfo/CMakeLists.txt` | target registration + build wiring |

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
[ABI block](../../yaml/isa.md#abi--the-calling-convention). An OR-based constant
idiom (`LUI`+`ORI`) yields the `hi_lo_or` strategy instead of pico32's
`LUI`+`ADDI` `hi_lo_add` - same generator, different YAML - and
[`examples/npu-probe`](../../../examples/npu-probe/README.md) pushes further off
the pico32 path entirely (big-endian, vector registers, no stack).

## Current boundaries

This project's boundaries are consolidated in one place - see [Limitations](../../limitations.md#llvm-compiler).
