# The ISA manifest

The root manifest. Everything else hangs off it. Full shape, then each
section in detail:

```yaml
apiVersion: isa-archive/v1
kind: ISA
metadata:
  name: my-isa
spec:
  version: "1.0"
  xlen: 32                  # data width: PC, pointers, primary registers
  byte_order: little        # or big
  extends: ../base/isa.yaml # optional: inherit another ISA
  includes: ["*.yaml"]      # globs for the other manifests

  state:
    registers: [...]        # register files
    csrs: [...]             # control/status registers

  abi: {...}                # calling convention (optional)
  machine: {...}            # QEMU machine layout (optional)
  compiler: {...}           # target profile (optional)

  # object-format identity (optional, for the LLVM backend)
  triple_arch: riscv32
  elf_machine: 243
  nop_encoding: "00000013"
  elf_relocations: {...}
```

## `xlen` and `byte_order`

`xlen` is the **data width** in bits: the width of the program counter,
of addresses/pointers, and of the primary integer register file. Allowed
values: **8, 16, 32, 64, 128**. It is independent of the *instruction encoding*
width (a 16-bit-data ISA can have 32-bit instructions - the encoding width
comes from the [schemas](schemas.md)).

Notes for the extremes:
- **8/16** - fully supported; QEMU emulates them over a 32-bit machine word
  with PC and addresses masked to xlen (your `machine:` layout must fit the
  small address space - you'll get a clear error if it doesn't).
- **128** - registers and arithmetic are true 128-bit; the PC and address
  space are 64-bit in QEMU (the simulator has no 128-bit addresses, matching
  how real 128-bit designs work).

`byte_order` (`little` default, or `big`) drives the QEMU target's
endianness, the LLVM data layout, and the byte order of emitted encodings.

`asm_comment` (default `"#"`) is the assembly line-comment string for the
generated LLVM assembler (`CommentString`); set it to `";"`, `"//"`, etc. if your
assembly syntax differs.

## `state.registers` - register files

```yaml
state:
  registers:
    - name: gpr             # architectural name, used by schema fields
      width: 32             # bits per register
      count: 32
      zero_register: 0      # optional: this index always reads 0
      canonical_prefix: x   # registers named x0..x31 (default: first letter)
      type: i32             # element type (optional)
      aliases: { zero: 0, ra: 1, sp: 2, a0: 10 }   # ABI names
```

Declare **several files** - integer, floating point, vector, predicate. Beyond a
plain scalar file, a register file can carry a custom element `type`, a `shape`
(vectors and tiles), and per-register `attributes`. **`aliases` are the source of
all CPU conventions** (`sp`/`ra`/`zero` and the argument/saved registers come *only*
from here - nothing is guessed from positions).

See **[registers.md](registers.md)** for the full reference - element types, shaped
vector/tile registers, attributes, and the per-generator width rules.

## `state.csrs` - control/status registers

```yaml
state:
  csrs:
    - name: mstatus
      address: 0x300
      width: 32
      reset_value: 0
      fields:
        - { name: mie, start: 3, end: 3, access: rw, description: "Interrupt enable" }
        - { name: mpp, start: 11, end: 12, access: ro }
```

`access` is `rw`, `ro`, or `wo`. CSRs become state in the QEMU model, fields
and accessors in the generated C/Rust headers, CSR logic in the Verilog
output, and tables in the manual. Behaviors read and write them through the
`csr.<name>` namespace - see [the behavior DSL](behavior.md#csrs-and-traps).

## `trap` - exception / return wiring

Declaring a `trap:` block lets instruction behaviors use `trap()` and
`trap_return()` (see [CSRs and traps](behavior.md#csrs-and-traps)). It names
which CSRs - declared in `state.csrs` above - hold the trap vector, the saved
PC, and the cause:

```yaml
spec:
  trap:
    vector_csr: mtvec     # PC jumps here on a trap (direct mode: base & ~0x3)
    epc_csr:    mepc      # the trapping PC is saved here
    cause_csr:  mcause    # receives the cause code
    status_csr: mstatus   # optional; mie→mpie saved on trap, restored on return
    causes:               # named cause codes for trap(<name>)
      ecall_m: 11
      illegal: 2
```

On `trap(cause)` the generated QEMU saves the PC to `epc_csr`, writes the cause to
`cause_csr`, (if `status_csr` is given) shuffles `mie`/`mpie`, and jumps through
`vector_csr`; `trap_return()` restores the PC from `epc_csr`. The
[`pico32-part4/sys`](../../examples/tutorial/pico32-part4/sys/) ISA is a worked
example (`ecall`, `mret`, CSR access).

## `abi` - the calling convention

```yaml
abi:
  stack_alignment: 16
  arg_registers:    [a0, a1, a2, a3]
  ret_registers:    [a0, a1]
  callee_saved:     [ra, sp, s0, s1]
  frame_pointer:    s0
  fp_arg_registers: [fa0, fa1]    # hard-float ABI, if you have a float file
  fp_ret_registers: [fa0]
```

All names are **aliases** from your register files. If you omit the block (or
parts of it), the conventions are inferred from alias *naming*: `a0, a1, …`
become argument registers, `s*` plus `ra/sp/gp/tp` become callee-saved, the
first two argument registers return values, `s0` becomes the frame pointer.
Explicit beats implicit for anything you care about.

## `machine` - the QEMU machine

```yaml
machine:
  ram_base: 0x80000000
  ram_size: 0x08000000          # 128 MiB
  reset_vector: 0x80000000      # default: ram_base
  qemu:
    devices:
      - { name: uart,     type: ns16550,     base: 0x10000000, irq: 10 }
      - { name: poweroff, type: sifive_test, base: 0x00100000 }
```

This becomes `hw/{isa}/virt.c` - a minimal board with your RAM layout and
devices. Two device types are available today: `ns16550` (a UART - write a
byte to its base address and it appears on the console) and `sifive_test`
(a software power switch: write `0x5555` for "exit 0", `0x3333` for "exit
non-zero"). [Tutorial part 1](../../examples/tutorial/pico32-part1/README.md) uses both.

For a narrow `xlen`, the whole layout must fit in `2^xlen` bytes - generation
fails with a clear message otherwise.

## `compiler` - the target profile

```yaml
compiler:
  profile: c-baremetal    # default
  # profile: kernel-only
  # profile: custom
  # requires: [alu_rr.add, mem.load32]   # only with custom
```

Declares what the generated compiler is *for*, which decides what "complete"
means in the coverage report:

- **`c-baremetal`** (default) - the backend must lower freestanding C: full
  ALU, word-size load/store, branches, calls, a stack - and the `zero`/`ra`/`sp`
  aliases must exist.
- **`kernel-only`** - a compute target (GPU/NPU style). Nothing is required;
  a stack-less ISA is complete on its own terms. See `examples/npu-probe`.
- **`custom`** - exactly the roles listed under `requires:`.

Details: [compiler roles & coverage](../targets/compiler/roles-and-coverage.md).

## Object-format identity (`triple_arch`, `elf_machine`, …)

These tell the LLVM backend what object files to claim to be:

```yaml
triple_arch: riscv32        # LLVM triple to register under
elf_machine: 243            # ELF e_machine (243 = EM_RISCV)
nop_encoding: "00000013"    # your NOP, as hex (emitted in your byte order)
elf_relocations:            # optional: fixup → relocation name overrides
  jal:    R_RISCV_JAL
  branch: R_RISCV_BRANCH
  hi20:   R_RISCV_HI20
  lo12_i: R_RISCV_LO12_I
```

**Why you'd reuse a real triple:** generating a compiler is one thing;
*linking* its output is another. Linkers only understand relocations they
already know. If your immediate-field placements match an existing
architecture's, you can register under its triple and reuse its relocations -
then any stock linker links your programs. That's exactly what the
[tutorial's pico32](../../examples/tutorial/README.md) does with `riscv32`.
The full story: [linking - the ELF reality](../targets/compiler/build-and-use.md#linking-the-elf-reality).

## Current boundaries

This project's boundaries are consolidated in one place - see [Limitations](../limitations.md#isa-and-machine-model).
