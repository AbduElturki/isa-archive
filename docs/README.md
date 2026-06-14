# isa-archive documentation

Define your instruction set once, in YAML. Generate everything else:

- a **QEMU system emulator** that boots and runs programs for your ISA
- an **LLVM backend** — a real `clang` that compiles C for your ISA
- a **standalone assembler** + linker script (zero dependencies)
- **C and Rust intrinsics** headers for your custom instructions
- **SystemVerilog** hardware models
- a **reference manual** (Markdown / HTML / PDF)

All from one set of manifests. The pipeline is proven end to end: the
generated RISC-V backend compiles `fib.c`, and the binary runs on the
generated `qemu-system-rv32i`.

## Choose your path

| You want to… | Go to |
|---|---|
| Try it in 5 minutes, no builds | [Quickstart](getting-started/quickstart.md) |
| **Build your own ISA from scratch** | [**The pico32 tutorial**](tutorial/README.md) |
| Look up a YAML field or kind | [Manifest reference](yaml/README.md) |
| Understand the big picture first | [Concepts](getting-started/concepts.md) |
| Build & run the QEMU simulator | [QEMU guide](qemu/README.md) |
| Build & use the C compiler | [Compiler guide](compiler/README.md) |
| See what each example demonstrates | [Examples tour](examples.md) |

## All pages

**Getting started**
- [Installation](getting-started/installation.md) — install the tool; what you'll need later for toolchain builds
- [Quickstart](getting-started/quickstart.md) — validate, generate a manual, assemble a program — in minutes
- [Concepts](getting-started/concepts.md) — the manifest kinds and the generation pipeline

**Tutorial — build pico32 from an empty directory**
- [Overview](tutorial/README.md) — what you'll build, and one design decision explained
- [Part 1 — Hello, pico32](tutorial/01-hello-pico32.md) — 4 instructions, a UART, and a working simulator
- [Part 2 — A real instruction set](tutorial/02-a-real-instruction-set.md) — branches, loads, jumps; loops in assembly
- [Part 3 — Compiling C](tutorial/03-compiling-c.md) — ABI, compiler roles, and a clang of your own
- [Part 4 — Growing the ISA](tutorial/04-growing-the-isa.md) — extensions, intrinsics, manuals, hardware

**YAML manifest reference**
- [The manifest format](yaml/README.md) — envelope, validation, multi-file projects
- [ISA](yaml/isa.md) · [Schema](yaml/schemas.md) · [Instruction](yaml/instructions.md) · [Operand / Enum / Constant](yaml/types.md) · [uArch](yaml/uarch.md)
- [The behavior DSL](yaml/behavior.md) — how instruction semantics are written

**Guides**
- [QEMU: the generated simulator](qemu/README.md) and [building & running it](qemu/build-and-run.md)
- [LLVM: the generated compiler](compiler/README.md), [compiler roles & the coverage report](compiler/roles-and-coverage.md), and [building & using clang](compiler/build-and-use.md)

**Other generation targets**
- [Standalone assembler](targets/assembler.md) · [C/Rust intrinsics](targets/intrinsics.md) · [SystemVerilog](targets/verilog.md) · [Reference manuals](targets/reference-manuals.md)

**Reference**
- [CLI reference](cli.md) — every command, flag, and target
- [Examples tour](examples.md) — what each `examples/` directory demonstrates
