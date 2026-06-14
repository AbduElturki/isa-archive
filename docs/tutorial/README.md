# Tutorial: build pico32 from an empty directory

You're going to design a CPU architecture and end up with a working toolchain
for it: a simulator that boots it, an assembler that targets it, and — by
part 3 — a `clang` that compiles C for it. Everything from YAML you write
yourself.

**pico32** is a 32-bit, little-endian ISA with one register file (`r0`–`r31`,
`r0` hardwired to zero) and 32-bit instruction words. It starts with four
instructions and ends with fourteen.

## The parts

| Part | You add | At the end, this runs |
|---|---|---|
| [1 — Hello, pico32](01-hello-pico32.md) | 4 instructions, a UART, a power switch | `qemu-system-pico32` prints `H` and exits cleanly |
| [2 — A real instruction set](02-a-real-instruction-set.md) | branches, loads, jumps; enums & constants; multi-file layout | assembly loops: the alphabet, an array sum |
| [3 — Compiling C](03-compiling-c.md) | ABI names, compiler roles, a target profile | your clang compiles `fib.c`; it runs on your QEMU |
| [4 — Growing the ISA](04-growing-the-isa.md) | a MUL extension via `extends:`, intrinsics, a manual, RTL | compiled C uses MUL; a PDF manual; SystemVerilog |

Time: the YAML work in each part is minutes. Two one-time toolchain builds
punctuate it: QEMU (~10–20 min, part 1) and LLVM (~40–60 min, part 3). After
those, rebuilds when you change the ISA are **seconds** (QEMU) to minutes
(LLVM).

## One design decision, made up front

pico32 reuses **RISC-V's field placements** — opcode in bits 0–6, registers
at bits 7/15/20, the same immediate bit-scattering — and registers its object
files under the `riscv32` triple. Everything else is ours: the mnemonics,
opcode values, register names, ABI, and the YAML describing it all.

Why: linkers only understand relocations they already know. A relocation
patches an address into *specific bit positions* of an instruction; by
keeping our immediate fields where RISC-V keeps them, the stock LLD that
ships with LLVM links pico32 programs out of the box. Invent your own
placements and everything still works **except** linking compiled C — you'd
keep the simulator and the [standalone assembler](../targets/assembler.md).
The full story is in [the compiler guide](../compiler/build-and-use.md#linking-the-elf-reality);
`examples/minimips` is the same trick wearing a MIPS costume.

## Snapshots

Each part's finished state is checked in under `examples/tutorial/`:

```
examples/tutorial/pico32-part1/   ← end of part 1 (plus programs/)
examples/tutorial/pico32-part2/
examples/tutorial/pico32-part3/
examples/tutorial/pico32-part4/
```

Stuck? `diff -r my-pico32/ examples/tutorial/pico32-partN/` — or generate
straight from the snapshot and keep moving.

## Prerequisites

The [quickstart](../getting-started/quickstart.md) (5 min) is worth doing
first. Part 1 needs the QEMU build tools, part 3 the LLVM ones — see
[installation](../getting-started/installation.md#what-youll-need-later-optional-now).

[**Start part 1 →**](01-hello-pico32.md)
