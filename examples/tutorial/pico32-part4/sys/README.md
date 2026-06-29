# pico32sys - control/status registers + traps

An extension layer (`extends: ../isa.yaml`) that declares pico32's
**control/status registers** and the **system instructions that use them** -
taking a trap, returning from one, and reading/writing CSRs. Because this layer
adds no register file of its own, it inherits pico32's `gpr` file, ABI, triple
and machine unchanged, and *adds* to its instruction set.

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

These flow, with their fields and `ro`/`rw` access modes, into the **QEMU
`CPUArchState`**, the **reference manual**, the **SystemVerilog**, and the
C/Rust CSR headers.

A `trap:` block names which CSRs are the vector / saved-PC / cause, and five
instructions (opcode `SYSTEM`, 0x73) make the state usable:

| Instruction | Behavior | Does |
|---|---|---|
| `ECALL`      | `trap(ecall_m)`              | save pc→`mepc`, cause→`mcause`, jump to `mtvec` |
| `MRET`       | `trap_return()`              | restore pc from `mepc` |
| `CSRW_TVEC`  | `rd = csr.mtvec; csr.mtvec = rs1` | install a trap vector |
| `CSRR_CAUSE` | `rd = csr.mcause`            | read the trap cause |
| `CSRR_MIE`   | `rd = zext(csr.mstatus.mie)` | read one CSR field |

The QEMU helpers contain the real trap sequence - inspect them with:

## Try it

```sh
uv run isa-archive generate -i isa.yaml -t qemu-isa -o build/sys-qemu
grep -A6 'HELPER(ecall)' build/sys-qemu/pico32sys_helpers.c
uv run isa-archive generate -i isa.yaml -t docs     -o build/sys-docs
```

## Interrupts

Hardware interrupts are delivered too: the generated QEMU CPU vectors an external
IRQ (and synchronous exceptions) through the trap CSRs instead of halting, and the
machine declares an `irq_test` device whose register raises the CPU's IRQ line.
[`programs/irq.c`](programs/irq.c) is a runnable demo - it points `mtvec` at an
ISR, enables `mstatus.mie`, writes the device to take an interrupt, and exits PASS.
See that file's header for the exact build/run command (needs `-march=rv32i_zicsr`).

## Current boundaries

- Traps and interrupts both vector through `mtvec` in **direct mode**; there is no
  interrupt-controller / priority model, and software traps are taken via `trap()`
  in a behavior.
- CSR access is to a CSR fixed per instruction; a single `csrrw` that selects
  its CSR from a runtime immediate isn't modeled yet.
- `ECALL`/`MRET`/CSR instructions are simulator-side (custom-lowered in the LLVM
  backend) - you reach them from C via inline assembly, not codegen.
