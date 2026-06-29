# Part 2 - A real instruction set

Part 1's CPU computes straight ahead and falls off the end. This part adds
control flow and loads - and tidies the project the way real ISAs are
organized. By the end: thirteen instructions, and assembly programs with
loops running on your simulator.

The finished files are in this directory.

## What we're adding and why

| Instructions | Why |
|---|---|
| `SUB`, `OR` | a believable ALU |
| `LW` | read memory back (part 1 could only write) |
| `BEQ`, `BNE`, `BLT`, `BLTU` | decisions and loops |
| `JAL`, `JALR` | jumps - and, quietly, function calls for part 3 |

Not random picks: equality *and* ordering branches, word load *and* store,
jump *and* indirect-jump-with-link are exactly the shapes a C compiler will
demand in part 3. We're laying its table.

## Tidy first: constants, enums, multiple files

Magic numbers (`opcode: 0x33`, `funct3: 0`) don't scale. Move them to named
[Constants and Enums](../../../docs/yaml/types.md) in a `constants.yaml`:

```yaml
apiVersion: isa-archive/v1
kind: Constant
metadata: { name: BRANCH, description: conditional branch opcode }
spec: { value: 0x63, width: 7 }
---
apiVersion: isa-archive/v1
kind: Enum
metadata: { name: F3_BRANCH, description: branch condition selector }
spec:
  width: 3
  values: { BEQ: 0, BNE: 1, BLT: 4, BLTU: 6 }
```

…and split instructions into a directory. The ISA root pulls everything in
by glob:

```yaml
  includes:
    - "schemas.yaml"
    - "constants.yaml"
    - "instructions/*.yaml"
```

```
pico32/
  isa.yaml
  schemas.yaml          # was layouts.yaml
  constants.yaml
  instructions/
    alu.yaml            # ADD SUB OR ADDI LUI
    memory.yaml         # LW SW
    control.yaml        # BEQ BNE BLT BLTU JAL JALR
```

Instructions now read like a datasheet - `opcode: BRANCH`,
`funct3: F3_BRANCH.BEQ` - and a schema field can declare
`type: enum.F3_BRANCH` so fills are validated against the member list.

## The branch layout - split immediates, for real

Part 1's SType split an immediate in two. The branch format scatters a
13-bit, 2-byte-aligned offset across **four** fields (bit 0 is always zero
and isn't stored at all):

```yaml
apiVersion: isa-archive/v1
kind: Schema
metadata:
  name: BType
spec:
  length: 32
  fields:
    - { name: opcode,   start: 0,  width: 7, role: opcode }
    - { name: imm_11,   start: 7,  width: 1, role: immediate }
    - { name: imm_4_1,  start: 8,  width: 4, role: immediate }
    - { name: funct3,   start: 12, width: 3, role: constant, type: enum.F3_BRANCH }
    - { name: rs1,      start: 15, width: 5, role: register, type: gpr }
    - { name: rs2,      start: 20, width: 5, role: register, type: gpr }
    - { name: imm_10_5, start: 25, width: 6, role: immediate }
    - { name: imm_12,   start: 31, width: 1, role: immediate }
```

And the behavior reassembles the logical offset - the trailing literal `0`
supplies the implied bit:

```yaml
apiVersion: isa-archive/v1
kind: Instruction
metadata:
  name: BEQ
  description: Branch if equal
spec:
  schema: BType
  opcode: BRANCH
  funct3: F3_BRANCH.BEQ
  behavior: |
    if rs1 == rs2:
        pc = pc + sext({imm_12, imm_11, imm_10_5, imm_4_1, 0}, 13)
  description: "if rs1 == rs2: pc ← pc + sext(offset)"
```

Assigning `pc` *is* what makes it a branch - the simulator derives
taken/fall-through handling, and (next part) the compiler derives the branch
condition, from this one definition. `BNE` is the same with `!=`; for the
ordering pair note the signedness rule:

```yaml
# BLT - signed comparison must say so
behavior: |
  if signed(rs1) < signed(rs2):
      pc = pc + sext({imm_12, imm_11, imm_10_5, imm_4_1, 0}, 13)

# BLTU - comparisons are unsigned by default
behavior: |
  if rs1 < rs2:
      pc = pc + sext({imm_12, imm_11, imm_10_5, imm_4_1, 0}, 13)
```

## Jumps

`JAL` saves the return address and jumps PC-relative (its 21-bit offset
scatters across four fields - same trick, see this directory's `schemas.yaml`);
`JALR` jumps through a register:

```yaml
# JAL (JType)
behavior: |
  rd = pc + 4
  pc = pc + sext({imm_20, imm_19_12, imm_11, imm_10_1, 0}, 21)

# JALR (ITypeJalr)
behavior: |
  rd = pc + 4
  pc = rs1 + imm
```

Two statements: the link write and the jump. Call `JAL r1, target` /
return `JALR r0, r1, 0` - function calls, one part early.

## Rebuild and run

The ten-second loop:

```sh
$ isa-archive parse pico32/isa.yaml
  [pico32]  pico32 v0.2  xlen=32  8 schemas  13 instructions  0 operands  0 CSRs
$ isa-archive generate --isa pico32/isa.yaml -t qemu -o build/qemu-gen
$ bash build/qemu-gen/patch_qemu.sh qemu-src
$ ninja -C qemu-build        # ~10 s
$ isa-archive generate --isa pico32/isa.yaml -t asm -o build/asm
```

First loop - `loop.s` prints the alphabet with a backward branch:

```asm
.text
    lui   r1, 0x10000       # UART
    addi  r2, r0, 65        # 'A'
    addi  r3, r0, 91        # one past 'Z'

print_loop:
    sw    r1, r2, 0
    addi  r2, r2, 1
    bne   r2, r3, print_loop

    addi  r2, r0, 10        # '\n'
    sw    r1, r2, 0

    lui   r4, 0x100         # power off
    lui   r5, 0x5
    addi  r5, r5, 0x555
    sw    r4, r5, 0
```

Labels are all the assembler needs - it computes the PC-relative offset and
scatters the bits into the four immediate fields:

```sh
$ python3 build/asm/pico32_asm.py loop.s -o loop.elf --elf
$ qemu-build/qemu-system-pico32 -M pico32-virt -display none -serial stdio \
      -monitor none -bios none -kernel loop.elf
ABCDEFGHIJKLMNOPQRSTUVWXYZ
$ echo $?
0
```

Second program - `sum.s` (in this directory's `programs/`) stores 1 to 5 to RAM,
loads them back, sums them, and **checks its own answer** with `BEQ`,
printing `OK` or `NO`:

```sh
$ qemu-build/qemu-system-pico32 -M pico32-virt -display none -serial stdio \
      -monitor none -bios none -kernel sum.elf
OK
$ echo $?
0
```

A CPU that can be wrong and know it. Debugging tip for when yours is: add
`-d in_asm` to watch each instruction decode - wrong-looking disassembly
means an encoding bug in your schema, not your program.

## Current boundaries (of this 13-instruction CPU)

- It runs hand-written assembly only - one file, no symbols across files, no
  C. Part 3 is where the compiler arrives, and the ISA is already
  shape-complete for it.
- Programs are placed by the standalone assembler at your reset vector; real
  linking (sections, relocations) also arrives with part 3's toolchain.

[**Part 3: compiling C ->**](../pico32-part3/README.md)
