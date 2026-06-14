# Examples

Two example ISAs, each making a different point about what ISA-Archive can do.

## `tutorial/` — pico32, a CPU you build from scratch

The main event. **pico32** is a small 32-bit CPU grown across four snapshots —
from a four-instruction toy you can emulate, to a freestanding-C target with its
own clang backend, to an extensible ISA with a hardware-scheduling model. Each
snapshot is a working manifest set with its own `README.md` walkthrough:

| Snapshot | What it adds |
|---|---|
| [`pico32-part1`](tutorial/pico32-part1/) | Minimal ISA + QEMU board — 4 instructions, emulate `hello.s` |
| [`pico32-part2`](tutorial/pico32-part2/) | A real instruction set — constants, enums, split immediates, branches, jumps; assembly loops |
| [`pico32-part3`](tutorial/pico32-part3/) | Compiling C — ABI, compiler roles, object-format identity; an LLVM backend that compiles `fib.c` |
| [`pico32-part4`](tutorial/pico32-part4/) | Growing the ISA — `exec_type` tags, a uArch model, and `extends:`-based extension layers |

Part 4 ships three independent extension layers (each `extends:` the base),
showing one ISA can host many add-ons without forking:

- [`pico32-part4/mul/`](tutorial/pico32-part4/mul/) — a hardware multiply
- [`pico32-part4/fp/`](tutorial/pico32-part4/fp/) — single-precision floating point (a second register class + hard-float ABI)
- [`pico32-part4/sys/`](tutorial/pico32-part4/sys/) — control/status registers + system instructions

[`tutorial/scripts/`](tutorial/scripts/) automates the end-to-end QEMU + LLVM
build the tutorial walks through by hand.

Start at [the tutorial index](tutorial/README.md).

## `npu-probe/` — the not-a-CPU

A deliberately non-CPU target that proves the generator isn't secretly
RISC-V-shaped: **big-endian**, **128-bit vector** and **1-bit predicate**
register files, 64-bit instruction words, and a stack-less `kernel-only`
compiler profile. It exists to keep the "works for accelerators, not just CPUs"
claim honest. See [`npu-probe/README.md`](npu-probe/README.md).

## Generating from an example

```sh
uv run isa-archive generate -i examples/tutorial/pico32-part4/isa.yaml -t all -o build/pico32
uv run isa-archive generate -i examples/npu-probe/isa.yaml             -t c   -o build/npu
```

See [`docs/cli.md`](../docs/cli.md) for every target.
