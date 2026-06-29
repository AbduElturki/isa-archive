<div align="center">

# ISA-Archive Documentation

**Define your instruction set once, in YAML. Generate everything else.**

[Repo](../README.md) · [Quickstart](getting-started/quickstart.md) · [Tutorial](../examples/tutorial/README.md) · [CLI](cli.md)

</div>

From one set of manifests, ISA-Archive generates:

- a **QEMU system emulator** that boots and runs programs for your ISA
- an **LLVM backend** - a real `clang` that compiles C for your ISA
- a **standalone assembler** + linker script (zero dependencies)
- **C and Rust intrinsics** headers for your custom instructions
- **SystemVerilog** hardware models
- a **reference manual** (Markdown / HTML / PDF)

The pipeline is proven end to end: the generated pico32 backend compiles `fib.c`, and the binary
runs on the generated `qemu-system-pico32`.

## 🧭 Choose your path

| You want to… | Go to |
|---|---|
| Try it in 5 minutes, no builds | [Quickstart](getting-started/quickstart.md) |
| **Build your own ISA from scratch** | [**The pico32 tutorial**](../examples/tutorial/README.md) |
| Look up a YAML field or kind | [Manifest reference](yaml/README.md) |
| Understand the big picture first | [Concepts](getting-started/concepts.md) |
| Build & run the QEMU simulator | [QEMU guide](targets/qemu/README.md) |
| Build & use the C compiler | [Compiler guide](targets/compiler/README.md) |
| See what each example demonstrates | [Examples tour](examples.md) |
| Understand the code / contribute | [Developer docs](development/README.md) |

## 📚 All pages

**Getting started**
- [Installation](getting-started/installation.md) - install the tool; what you'll need later for toolchain builds
- [Quickstart](getting-started/quickstart.md) - validate, generate a manual, assemble a program - in minutes
- [Concepts](getting-started/concepts.md) - the manifest kinds and the generation pipeline

**Tutorial - build pico32 from an empty directory** (lives in `examples/tutorial/`)
- [Overview](../examples/tutorial/README.md) - what you'll build, and one design decision explained
- [Part 1 - Hello, pico32](../examples/tutorial/pico32-part1/README.md) - 4 instructions, a UART, and a working simulator
- [Part 2 - A real instruction set](../examples/tutorial/pico32-part2/README.md) - branches, loads, jumps; loops in assembly
- [Part 3 - Compiling C](../examples/tutorial/pico32-part3/README.md) - ABI, compiler roles, and a clang of your own
- [Part 4 - Growing the ISA](../examples/tutorial/pico32-part4/README.md) - extensions, intrinsics, manuals, hardware

**YAML manifest reference**
- [The manifest format](yaml/README.md) - envelope, validation, multi-file projects
- [ISA](yaml/isa.md) · [Register files](yaml/registers.md) · [Schema](yaml/schemas.md) · [Instruction](yaml/instructions.md) · [Operand / Enum / Constant / ScalarType](yaml/types.md) · [uArch](yaml/uarch.md)
- [The behavior DSL](yaml/behavior.md) - how instruction semantics are written

**Guides**
- [QEMU: the generated simulator](targets/qemu/README.md) and [building & running it](targets/qemu/build-and-run.md)
- [LLVM: the generated compiler](targets/compiler/README.md), [compiler roles & the coverage report](targets/compiler/roles-and-coverage.md), and [building & using clang](targets/compiler/build-and-use.md)

**Other generation targets** ([overview](targets/README.md))
- [Standalone assembler](targets/assembler/README.md) · [C/Rust intrinsics](targets/intrinsics/README.md) · [SystemVerilog](targets/verilog/README.md) · [Reference manuals](targets/reference-manuals/README.md) · [C++ ISA headers](targets/cpp-isa/README.md)

**Reference**
- [CLI reference](cli.md) - every command, flag, and target
- [Examples tour](examples.md) - what each `examples/` directory demonstrates
- [Limitations](limitations.md) - every current boundary, by tool area and by target, in one place

**Developer docs** (how it's built, how to contribute)
- [Architecture](development/architecture.md) - the pipeline and the modules
- [Extending the tool](development/extending.md) - add a target, manifest kind, DSL construct, or backend
