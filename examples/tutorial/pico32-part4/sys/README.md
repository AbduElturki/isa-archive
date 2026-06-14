# pico32sys — control/status registers

An extension layer (`extends: ../isa.yaml`) that declares pico32's
**control/status registers** — the machine's counters and trap state. Because
this layer declares only `state.csrs` and no register file of its own, it
inherits pico32's `gpr` file, ABI, instructions, triple and machine unchanged.

## What it adds

Six CSRs, each with its bit-field layout and per-field access mode:

| CSR | Address | Purpose |
|---|---|---|
| `cycle`   | 0xC00 | cycle counter (read-only) |
| `instret` | 0xC02 | retired-instruction counter (read-only) |
| `mstatus` | 0x300 | machine status (`mie`, `mpie`, `mpp`) |
| `mtvec`   | 0x305 | trap-vector base + mode |
| `mepc`    | 0x341 | exception program counter |
| `mcause`  | 0x342 | trap cause (`code`, `interrupt`) |

These flow, with their fields and `ro`/`rw` access modes, into:

- the **QEMU `CPUArchState`** (`grep csr build/sys-qemu/pico32sys_arch.h`),
- the **reference manual** (`-t docs` renders a per-CSR field table),
- the **SystemVerilog** and the C/Rust CSR headers.

## Try it

```sh
uv run isa-archive generate -i isa.yaml -t qemu-isa -o build/sys-qemu
uv run isa-archive generate -i isa.yaml -t docs     -o build/sys-docs
```

## Current boundaries

- This layer *declares* the CSRs (architectural state); it does not add CSR
  read/write instructions. The QEMU C backend does not yet emulate
  CSR-accessing behaviors (`rd = mstatus`), so shipping `CSRRW`/`CSRRS` here
  would generate helpers with wrong semantics. Declaring the registers — which
  every backend consumes correctly — is the honest showcase until CSR behavior
  lands in the QEMU/TCG backend.
