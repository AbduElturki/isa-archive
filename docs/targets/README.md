# Generation targets

Every target is generated from the same manifests, so they can't drift apart. Pick one with
`-t <target>` (see the [CLI reference](../cli.md) for the full list, including parent/sub-targets).

| Target | `-t` | What it makes | Guide |
|---|---|---|---|
| QEMU emulator | `qemu` | A `qemu-system-{isa}` that boots and runs your programs | [QEMU guide](qemu/README.md) |
| LLVM backend | `llvm` | A real `clang`/`llc` that compiles C for your ISA | [Compiler guide](compiler/README.md) |
| Assembler | `asm` | A standalone Python assembler + linker script (zero deps) | [assembler.md](assembler/README.md) |
| C / Rust intrinsics | `c` · `rust` | Inline-asm wrappers, operand structs, and CSR headers | [intrinsics.md](intrinsics/README.md) |
| SystemVerilog | `verilog` | Synthesizable datapath/RTL skeletons from a uArch model | [verilog.md](verilog/README.md) |
| Reference manual | `docs` | A human-readable manual (Markdown / HTML / PDF) | [reference-manuals.md](reference-manuals/README.md) |
| C++ ISA headers | `cpp-isa` | Descriptive C++ enums + decode + metadata for your own models | [cpp-isa.md](cpp-isa/README.md) |

QEMU and LLVM are large enough to have their own guide sections; the rest are documented on this
page's links.
