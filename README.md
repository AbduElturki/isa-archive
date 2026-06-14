# ISA-Archive

**ISA-Archive** is a Kubernetes-style manifest orchestrator for modern processor design. Define your ISA once in YAML, then generate a complete QEMU target, standalone assembler, SystemVerilog hardware models, C/Rust software intrinsics, and documentation from that single source of truth.

## Key Features

- **Declarative YAML Manifests:** Define your ISA using structured, human-readable YAML (K8s-style).
- **ISA & uArch Decoupling:** Define an ISA once and implement multiple micro-architecture "chassis" for it.
- **Deep Validation:** Strict compile-time checks catch bit-width mismatches, overlapping fields, undeclared variables, unmapped registers, and duplicate instruction encodings (decoder collisions).
- **Multi-Target Generation:**
  - **Hardware (SystemVerilog):** Packed structs, parameterized ALUs, CSR logic, and pipeline skeletons.
  - **Standalone Assembler (`asm`):** Self-contained Python assembler + linker script. No external dependencies — runs directly against the QEMU target.
  - **QEMU (TCG JIT):** Full QEMU backend — `decodetree` rules, `helper.c/h`, `trans_*.c.inc`, QOM boilerplate, machine definition, and build system.  Drop output directly into the QEMU source tree.
  - **Software Intrinsics (C/Rust):** Type-safe structs and inline assembly intrinsics for calling custom instructions from software.
  - **Documentation (Markdown/HTML/PDF):** Human-readable reference manuals with instruction layouts, CSRs, and behavior descriptions.
  - **Compiler (LLVM):** A complete LLVM backend — a real `clang`/`llc` for your ISA. Proven end to end: the generated pico32 backend compiles `fib.c`, and the binary runs on the generated `qemu-system-pico32`.

## Installation

ISA-Archive is designed to be used with [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/abduelturki/isa-archive.git
cd isa-archive
uv run isa-archive --help
```

## Quick Start

```bash
# Scaffold a new ISA project
isa-archive init my-cpu --xlen 32 --output-dir /tmp

# Parse and validate
isa-archive parse examples/tutorial/pico32-part4/isa.yaml

# Generate a standalone assembler
isa-archive generate --isa examples/tutorial/pico32-part4/isa.yaml -t asm -o /tmp/asm-out/

# Generate a complete QEMU target (drop-in, mirrors QEMU source tree)
isa-archive generate --isa examples/tutorial/pico32-part4/isa.yaml -t qemu -o /tmp/qemu-out/

# Generate everything at once
isa-archive generate --isa examples/tutorial/pico32-part4/isa.yaml -t all -o build/
```

**New here?** Build your own ISA from an empty directory — simulate it, then
compile C for it — in the [**pico32 tutorial**](examples/tutorial/README.md).

## Documentation

Full docs live in [`docs/`](docs/README.md):

| | |
|---|---|
| [Quickstart](docs/getting-started/quickstart.md) | First success in 5 minutes, no builds |
| [**Tutorial**](docs/tutorial/README.md) | Build the pico32 ISA from scratch: simulate it, then compile C for it |
| [Manifest reference](docs/yaml/README.md) | Every YAML kind, field by field, plus the [behavior DSL](docs/yaml/behavior.md) |
| [CLI reference](docs/cli.md) | Every command, flag, and generation target |
| [QEMU guide](docs/qemu/README.md) · [Compiler guide](docs/compiler/README.md) | The generated simulator and compiler, and how to build them |
| [Examples tour](docs/examples.md) | What each `examples/` directory demonstrates |

## Generation Targets

| Target | Flag | Output |
|---|---|---|
| Standalone assembler + linker script | `-t asm` | `{isa}_asm.py`, `linker.ld` |
| Complete QEMU target (tree-mirroring) | `-t qemu` | `target/{isa}/`, `hw/{isa}/`, `configs/`, `patch_qemu.sh` |
| QEMU ISA semantics only (flat) | `-t qemu-isa` | 7 flat files per ISA |
| SystemVerilog hardware | `-t verilog` | `{isa}_operands.sv`, `{isa}_cpu.sv`, … |
| C intrinsics + structs | `-t c` | `{isa}_intrinsics.h`, `{isa}_structs.h`, `{isa}_csrs.h` |
| Rust intrinsics + structs | `-t rust` | `{isa}_intrinsics.rs`, `{isa}_structs.rs`, `{isa}_csrs.rs` |
| Documentation | `-t docs` | `{isa}_reference.md` / `.html` / `.pdf` |
| Complete LLVM backend (tree-mirroring) | `-t llvm` | `llvm/lib/Target/{ISA}/`, `COMPILER_COVERAGE.md`, `patch_llvm.sh` |
| All non-QEMU targets | `-t all` | All of the above |

## Manifest Kinds

- `ISA` — Root orchestrator: registers, architectural CSRs, machine layout, file includes
- `Schema` — Instruction bit-layout (field positions, roles)
- `Instruction` — Operation with encoding constants and semantic behavior
- `Operand` — Complex semantic data types (recursive structs)
- `Constant` — Named numeric values reusable across instructions
- `Enum` — Grouped named values for instruction fields (e.g., `funct3`)
- `uArch` — Micro-architecture implementation (pipelines, blocks, implementation CSRs)
- `Pipeline` / `Block` — Micro-architectural pipeline stage and functional unit descriptors
- `CSR` — Control and Status Registers with per-field access protections

## Behavioral DSL

The `behavior` field in an `Instruction` manifest uses a Python-like DSL. The compiler automatically lowers it to C (QEMU), SystemVerilog, and Rust.

```yaml
# ALU operation
behavior: "rd = rs1 + rs2"

# Bitwise with temporaries (auto-typed)
behavior: |
  temp = rs1 ^ rs2
  rd = temp >> imm

# Memory load/store
behavior: "rd = sext(mem32[rs1 + imm])"
behavior: "mem8[rs1 + imm] = rs2"

# Conditional branch
behavior: |
  if rs1 == rs2:
    pc = pc + imm

# Bit slice and concatenation
behavior: "rd = {rs2[0:16], rs1[16:32]}"
```

## Project Layout

```
docs/
  getting-started/  ← Install, quickstart, concepts
  tutorial/         ← Pointer to the tutorial (which lives in examples/)
  yaml/             ← Manifest reference (one page per kind) + behavior DSL
  qemu/             ← QEMU backend guide
  compiler/         ← LLVM backend guide (roles, profiles, building clang)
  targets/          ← Assembler, intrinsics, Verilog, reference manuals
examples/
  tutorial/         ← Build pico32 from scratch (4 narrated parts + snapshots)
    pico32-part4/mul,fp,sys  ← extension layers (multiply, float, CSRs)
    scripts/        ← scripted end-to-end QEMU + LLVM build
  npu-probe/        ← Accelerator-style ISA (kernel-only, big-endian, vectors)
src/isa_archive/
  compiler/         ← Behavior IR, loader, and per-target language backends
  generators/       ← Per-target generators (qemu, asm, sv, sw, docs, llvm)
    templates/      ← Jinja templates: asm / qemu / sv / sw / docs / llvm
  models/           ← Pydantic manifest models
tests/              ← pytest unit + integration tests
```

## License

- **Tool Source Code:** GNU GPLv3
- **Generated Output:** Full ownership belongs to the user or organization. Provided "as is", without warranty of any kind.
