# Part 1 - Hello, pico32

Four instructions, a UART, a power switch, one real simulator. By the end,
a program you wrote, in an instruction set you invented, prints a character
and exits - under QEMU.

The finished files are in this directory; build your own alongside them and
`diff` when stuck.

## Scaffold

```sh
$ isa-archive init pico32 --xlen 32 --output-dir .
Created pico32/ with 3 files.

  isa.yaml          - ISA root (xlen=32, 32 GPRs)
  layouts.yaml      - RType instruction schema
  instructions.yaml - ADD instruction
```

Open the three files. `isa.yaml` declares the architecture's identity and
state; `layouts.yaml` has one bit layout; `instructions.yaml` one
instruction. Every manifest shares the same envelope -
`apiVersion` / `kind` / `metadata` / `spec` - and that's the whole format.

Two edits to `isa.yaml` before we grow it. First, name the registers our way
(`r0…r31` instead of the scaffold's `x`-prefix), keeping `r0` hardwired to
zero - and drop the scaffold's ABI aliases; we'll earn those in part 3:

```yaml
  state:
    registers:
      - name: gpr
        width: 32
        count: 32
        zero_register: 0      # r0 always reads as zero
        canonical_prefix: r   # registers are named r0..r31
```

## The machine

A CPU is no fun without somewhere to live. Add a `machine:` block to
`spec:` - this becomes a QEMU board:

```yaml
  machine:
    ram_base: 0x80000000      # RAM starts here…
    ram_size: 0x08000000      # …128 MiB of it
    reset_vector: 0x80000000  # execution starts at the bottom of RAM
    qemu:
      devices:
        - { name: uart,     type: ns16550,     base: 0x10000000, irq: 10 }
        - { name: poweroff, type: sifive_test, base: 0x00100000 }
```

Two memory-mapped devices: an `ns16550` UART - store a byte to
`0x10000000` and it appears on your terminal - and a `sifive_test` power
switch - store `0x5555` to `0x00100000` and QEMU exits with status 0.

## Three more instructions

`ADD` can't print anything. To write a byte to the UART we need to **build an
address** (it's `0x10000000` - too big for any immediate field), **make a
byte**, and **store it**. That's `LUI`, `ADDI`, and `SW`.

Add the layouts to `layouts.yaml` (the scaffold's RType stays as is):

```yaml
---
apiVersion: isa-archive/v1
kind: Schema
metadata:
  name: ITypeALU
  description: Register-immediate - rd ← rs1 op sext(imm12)
spec:
  length: 32
  fields:
    - { name: opcode, start: 0,  width: 7,  role: opcode }
    - { name: rd,     start: 7,  width: 5,  role: register, type: gpr }
    - { name: funct3, start: 12, width: 3,  role: constant }
    - { name: rs1,    start: 15, width: 5,  role: register, type: gpr }
    - { name: imm,    start: 20, width: 12, role: immediate, type: signed }

---
apiVersion: isa-archive/v1
kind: Schema
metadata:
  name: UType
  description: Upper immediate - 20 bits destined for the top of a register
spec:
  length: 32
  fields:
    - { name: opcode, start: 0,  width: 7,  role: opcode }
    - { name: rd,     start: 7,  width: 5,  role: register, type: gpr }
    - { name: imm,    start: 12, width: 20, role: immediate }

---
apiVersion: isa-archive/v1
kind: Schema
metadata:
  name: SType
  description: Store - the 12-bit offset is split across two fields
spec:
  length: 32
  fields:
    - { name: opcode,   start: 0,  width: 7, role: opcode }
    - { name: imm_4_0,  start: 7,  width: 5, role: immediate }
    - { name: funct3,   start: 12, width: 3, role: constant }
    - { name: rs1,      start: 15, width: 5, role: register, type: gpr }
    - { name: rs2,      start: 20, width: 5, role: register, type: gpr }
    - { name: imm_11_5, start: 25, width: 7, role: immediate }
```

Each field names its bits (`start` = least-significant bit) and its **role**:
`opcode` and `constant` get fixed per instruction, `register` fields index a
register file, `immediate` fields carry values. Field *names* are how
behaviors will refer to them.

SType smuggles in one powerful idea: the store offset is **split** - bits 4:0
live at position 7, bits 11:5 at position 25 (so the register fields can stay
in the same place across all layouts). Name the pieces `imm_<hi>_<lo>` and
the tool reassembles them everywhere - decoder, assembler, compiler. More in
[the schema reference](../../../docs/yaml/schemas.md#split-immediates).

Now the instructions, in `instructions.yaml`:

```yaml
---
apiVersion: isa-archive/v1
kind: Instruction
metadata:
  name: ADDI
  description: Add a sign-extended 12-bit immediate
spec:
  schema: ITypeALU
  opcode: 0x13
  funct3: 0
  behavior: "rd = rs1 + imm"
  description: "rd ← rs1 + sext(imm)"

---
apiVersion: isa-archive/v1
kind: Instruction
metadata:
  name: LUI
  description: Load upper immediate - set the top 20 bits of a register
spec:
  schema: UType
  opcode: 0x37
  behavior: "rd = zext(imm) << 12"
  description: "rd ← imm << 12"

---
apiVersion: isa-archive/v1
kind: Instruction
metadata:
  name: SW
  description: Store a 32-bit word
spec:
  schema: SType
  opcode: 0x23
  funct3: 2
  behavior: "mem32[rs1 + {imm_11_5, imm_4_0}] = rs2"
  description: "mem[rs1 + sext(offset)] ← rs2"
```

Read the behaviors aloud:

- `rd = rs1 + imm` - field names are variables; `imm` was declared `signed`,
  so it sign-extends.
- `rd = zext(imm) << 12` - zero-extend the 20-bit immediate, shift it into
  the top: `LUI r1, 0x10000` puts `0x10000000` in `r1`. There's our UART base.
- `mem32[rs1 + {imm_11_5, imm_4_0}] = rs2` - `{a, b}` is bit concatenation,
  reassembling the split offset; `mem32[...]` on the left of `=` is a store.

The full language: [the behavior DSL](../../../docs/yaml/behavior.md).

## Validate

```sh
$ isa-archive parse pico32/isa.yaml
Validated pico32/isa.yaml
  [pico32]  pico32 v0.1  xlen=32  4 schemas  4 instructions  0 operands  0 CSRs
```

Worth thirty seconds: break it on purpose. Misspell a key (`widht: 5`),
overlap two fields, give two instructions the same opcode - every mistake is
a named, located error. The validator is the safety net the rest of the
tutorial leans on.

## Build the simulator (one time, ~10-20 min)

```sh
$ isa-archive generate --isa pico32/isa.yaml -t qemu -o build/qemu-gen
Generated complete QEMU target in build/qemu-gen
```

Twenty files, mirroring QEMU's own source layout: a decoder spec, one C
helper per instruction (your behaviors, translated), the CPU model, and a
`virt.c` board built from your `machine:` block. Now feed them to QEMU:

```sh
git clone --depth=1 --branch v9.2.0 https://github.com/qemu/qemu.git qemu-src
bash build/qemu-gen/patch_qemu.sh qemu-src
mkdir qemu-build && cd qemu-build
../qemu-src/configure --target-list=pico32-softmmu \
    --disable-docs --disable-werror \
    --extra-cflags="-Wno-unused-function -Wno-unused-variable"
ninja -j$(nproc)
cd ..
```

(That's exactly what [`../scripts/01_build_qemu.sh`](../scripts/01_build_qemu.sh)
automates.) Coffee. When ninja finishes:

```sh
$ qemu-build/qemu-system-pico32 --version
QEMU emulator version 9.2.0
```

There is now a QEMU in the world that emulates an architecture that didn't
exist an hour ago.

## Run it

The program: write `'H'` and a newline to the UART, then hit the power
switch. Save as `hello.s`:

```asm
.text
    lui   r1, 0x10000       # r1 = 0x10000000 (UART base)
    addi  r2, r0, 72        # r2 = 'H'
    sw    r1, r2, 0         # UART ← 'H'
    addi  r2, r0, 10        # r2 = '\n'
    sw    r1, r2, 0         # UART ← '\n'

    lui   r3, 0x100         # r3 = 0x00100000 (sifive_test)
    lui   r4, 0x5           # r4 = 0x5000
    addi  r4, r4, 0x555     # r4 = 0x5555 (the "exit 0" magic value)
    sw    r3, r4, 0         # power off
```

Note the `0x5555` construction: LUI gives the top, ADDI adds the bottom -
the exact idiom the C compiler will use for every large constant in part 3.

Assemble with the [generated assembler](../../../docs/targets/assembler/README.md) and run:

```sh
$ isa-archive generate --isa pico32/isa.yaml -t asm -o build/asm
$ python3 build/asm/pico32_asm.py hello.s -o hello.elf --elf
Written 120 bytes → hello.elf

$ qemu-build/qemu-system-pico32 -M pico32-virt -display none -serial stdio \
      -monitor none -bios none -kernel hello.elf
H
$ echo $?
0
```

`H`, exit code 0. Your ISA, your program, a real simulator.

## Current boundaries (of this 4-instruction CPU)

- **No branches** -> no loops, no decisions. Part 2 fixes that.
- **The program "ends" by power-off.** After the final `sw`, the CPU would
  run into empty memory and hit an illegal instruction - the model halts the
  CPU cleanly (visible with `-d guest_errors`) while the power-off completes.
  Once part 2 adds jumps, well-mannered programs spin (`jal r0, 0`) instead
  of falling off the edge.
- **Every ISA change needs a QEMU rebuild** - but from now on it's
  incremental: regenerate, re-patch, `ninja` - about ten seconds. Part 2
  relies on that loop.

[**Part 2: a real instruction set ->**](../pico32-part2/README.md)
