# Tutorial: build pico32 from an empty directory

The pico32 tutorial now lives **next to its code**, in
[`examples/tutorial/`](../../examples/tutorial/README.md) - each part's prose
sits beside the working manifests it produces, so the walkthrough and the
snapshot can never drift apart.

Build a 32-bit CPU from an empty directory and end up with a simulator, an
assembler, a C compiler, software headers, a manual, and an RTL skeleton -
all from YAML:

| Part | You add |
|---|---|
| [1 - Hello, pico32](../../examples/tutorial/pico32-part1/README.md) | 4 instructions, a UART, a power switch; emulate it under QEMU |
| [2 - A real instruction set](../../examples/tutorial/pico32-part2/README.md) | branches, loads, jumps; enums & constants; assembly loops |
| [3 - Compiling C](../../examples/tutorial/pico32-part3/README.md) | ABI, compiler roles, object-format identity; a clang that compiles `fib.c` |
| [4 - Growing the ISA](../../examples/tutorial/pico32-part4/README.md) | extension layers (`mul`/`fp`/`sys`) via `extends:`, intrinsics, a manual, RTL |

[**Start the tutorial →**](../../examples/tutorial/README.md)
