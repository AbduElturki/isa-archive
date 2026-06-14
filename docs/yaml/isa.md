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
width (a 16-bit-data ISA can have 32-bit instructions — the encoding width
comes from the [schemas](schemas.md)).

Notes for the extremes:
- **8/16** — fully supported; QEMU emulates them over a 32-bit machine word
  with PC and addresses masked to xlen (your `machine:` layout must fit the
  small address space — you'll get a clear error if it doesn't).
- **128** — registers and arithmetic are true 128-bit; the PC and address
  space are 64-bit in QEMU (the simulator has no 128-bit addresses, matching
  how real 128-bit designs work).

`byte_order` (`little` default, or `big`) drives the QEMU target's
endianness, the LLVM data layout, and the byte order of emitted encodings.

## `state.registers` — register files

```yaml
state:
  registers:
    - name: gpr             # architectural name, used by schema fields
      width: 32             # bits per register
      count: 32
      zero_register: 0      # optional: this index always reads 0
      canonical_prefix: x   # registers named x0..x31 (default: first letter)
      type: i32             # element type (optional, see below)
      aliases:              # ABI names (optional but important — see below)
        zero: 0
        ra: 1
        sp: 2
        a0: 10
```

You can declare **several files** — integer + floating point, vector,
predicate. Each is independent state with its own width and count.

**`aliases` are the source of all CPU conventions.** `sp`, `ra`, `zero`, and
the argument/saved-register names come *only* from here. Nothing is ever
guessed from register positions: an ISA with no `sp` alias simply has no stack
pointer anywhere in the generated code (correct for accelerator-style
targets). If you want to compile C, declare at least `zero`, `ra`, and `sp` —
the coverage report will tell you if they're missing.

**`type`** sets the element type:
- a scalar — `i32` (default behavior), `f32`/`f64` (an IEEE-float file: gets
  float arithmetic, float load/store, and a float calling convention),
- or an [Operand](types.md) name — the file holds structured values, treated
  as opaque storage by the compiler.

**Width matters per generator.** Any width simulates. For the *compiler*, a
file becomes an allocatable register class only if it's float-typed or
xlen-wide; other files (1-bit predicates, wide accumulators) stay
architectural state, and instructions using them are simulator-only — see
[the compiler guide](../compiler/README.md#register-files-and-the-compiler).
In QEMU, files up to 64 bits (and exactly 128 bits) support direct arithmetic
in behaviors; details in [the QEMU guide](../qemu/README.md#how-register-files-are-stored).

Three real configurations to crib from:

```yaml
# examples/rv32/base — one integer file with full RISC-V ABI aliases
- { name: gpr, width: 32, count: 32, zero_register: 0, canonical_prefix: x,
    aliases: { zero: 0, ra: 1, sp: 2, a0: 10, ... } }

# examples/showcase — integer + single-precision float
- { name: gpr, width: 32, count: 32, zero_register: 0 }
- { name: fpr, width: 32, count: 32, type: f32 }

# examples/npu-probe — accelerator: 128-bit vectors + 1-bit predicates
- { name: gpr,  width: 32,  count: 16 }
- { name: vreg, width: 128, count: 16 }
- { name: preg, width: 1,   count: 8 }
```

## `state.csrs` — control/status registers

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
output, and tables in the manual.

## `abi` — the calling convention

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

## `machine` — the QEMU machine

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

This becomes `hw/{isa}/virt.c` — a minimal board with your RAM layout and
devices. Two device types are available today: `ns16550` (a UART — write a
byte to its base address and it appears on the console) and `sifive_test`
(a software power switch: write `0x5555` for "exit 0", `0x3333` for "exit
non-zero"). [Tutorial part 1](../tutorial/01-hello-pico32.md) uses both.

For a narrow `xlen`, the whole layout must fit in `2^xlen` bytes — generation
fails with a clear message otherwise.

## `compiler` — the target profile

```yaml
compiler:
  profile: c-baremetal    # default
  # profile: kernel-only
  # profile: custom
  # requires: [alu_rr.add, mem.load32]   # only with custom
```

Declares what the generated compiler is *for*, which decides what "complete"
means in the coverage report:

- **`c-baremetal`** (default) — the backend must lower freestanding C: full
  ALU, word-size load/store, branches, calls, a stack — and the `zero`/`ra`/`sp`
  aliases must exist.
- **`kernel-only`** — a compute target (GPU/NPU style). Nothing is required;
  a stack-less ISA is complete on its own terms. See `examples/npu-probe`.
- **`custom`** — exactly the roles listed under `requires:`.

Details: [compiler roles & coverage](../compiler/roles-and-coverage.md).

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
architecture's, you can register under its triple and reuse its relocations —
then any stock linker links your programs. That's exactly what
`examples/minimips` and the [tutorial's pico32](../tutorial/README.md) do
with `riscv32`. The full story: [linking — the ELF reality](../compiler/build-and-use.md#linking-the-elf-reality).

## Current boundaries

- `xlen` must be one of 8/16/32/64/128; QEMU additionally caps the address
  space at 64 bits (xlen=128 data is fine, 128-bit *addresses* are not):
  generation fails with a message explaining the limit.
- Register files wider than 64 bits (other than exactly 128) hold state but
  can't be operated on in behaviors yet — the error names the instruction and
  the file.
- The machine model offers the two device types listed above; other
  peripherals mean editing the generated `virt.c` (it's small and readable).
