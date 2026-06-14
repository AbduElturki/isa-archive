# End-to-End Demo: YAML → QEMU Sim → LLVM Compiler → Running Code

This demo proves the full isa-archive workflow in three steps:

```
examples/rv32/base/isa.yaml
        │
        ├─▶ bash 01_build_qemu.sh  →  qemu-system-rv32i  (custom CPU simulator)
        ├─▶ bash 02_build_llvm.sh  →  clang              (custom C compiler)
        └─▶ bash 03_run_demo.sh    →  compile fib.c + hello.c → run on simulator
```

## Prerequisites

| Tool | Install (macOS) |
|------|----------------|
| `uv` | `curl -Ls https://astral.sh/uv/install.sh \| sh` |
| `meson` / `ninja` | `brew install meson ninja` |
| `cmake` | `brew install cmake` |
| `git` | Xcode CLT |

Set `BUILD_DIR` (default: `/tmp/rv32-demo`) to override where things are built:

```bash
export BUILD_DIR=/tmp/rv32-demo
```

## Step 1 — Build the QEMU simulator (~15 min, one-time)

Generates a complete QEMU CPU target from the ISA YAML, clones QEMU v9.2.0,
integrates the generated files, and builds `qemu-system-rv32i`.

```bash
bash examples/demo/01_build_qemu.sh
```

## Step 2 — Build the LLVM compiler (~40 min, one-time)

Generates a complete LLVM backend from the ISA YAML, clones LLVM 18.1.8,
integrates the generated backend, and builds `clang` + `llc` targeting rv32i.

```bash
bash examples/demo/02_build_llvm.sh
```

## Step 3 — Compile and run programs

Compiles two bare-metal C programs with the generated compiler and runs them
on the generated QEMU simulator.

```bash
bash examples/demo/03_run_demo.sh
```

Expected output:

```
[1/2] Compiling fib.c ...
      Running on generated QEMU ...
      fib(10) == 55:  PASS

[2/2] Compiling hello.c ...
      Running on generated QEMU ...
      Output: Hello, rv32i!
      UART output:    PASS
```

## Demo programs

| File | What it tests |
|------|---------------|
| `programs/fib.c` | Recursive Fibonacci — arithmetic, branches, function calls |
| `programs/hello.c` | UART MMIO write — memory-mapped I/O, string iteration |
| `programs/start.S` | Bare-metal startup — stack setup, SiFive test device exit |
| `programs/link.ld` | Linker script — `.text` at 0x80000000, 64 KB stack |

## Tests

`tests/` contains the full QEMU integration test suite (moved from `examples/scripts/`):

```bash
# After 01_build_qemu.sh:
RV32_QEMU_BIN=$BUILD_DIR/qemu/build/qemu-system-rv32i \
  uv run pytest examples/demo/tests/test_qemu_target.py -v
```

## How it works

```
isa.yaml
  │
  ├─ isa-archive generate -t qemu  →  target/rv32i/*.c + hw/rv32i/*.c
  │                                    + patch_qemu.sh (integration script)
  │
  └─ isa-archive generate -t llvm  →  llvm/lib/Target/RV32I/*.td + *.cpp
                                       + patch_llvm.sh (integration script)
```

The generated QEMU target defines the CPU register file, instruction decode
loop, and machine peripherals (UART, test device) — all derived from the YAML.

The generated LLVM backend defines TableGen instruction descriptions, register
allocator info, calling convention, and SelectionDAG patterns — again, entirely
from the YAML — allowing `clang` to compile standard C for the custom ISA.
