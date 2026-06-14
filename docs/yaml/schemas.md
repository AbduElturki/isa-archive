# Schema — an instruction bit layout

A Schema names the bit positions an instruction format uses. Many
instructions share one schema: all of RISC-V's register-register ALU ops are
one "R-type" schema with different fixed values.

```yaml
apiVersion: isa-archive/v1
kind: Schema
metadata:
  name: RType
  description: Register-register — rd ← rs1 op rs2
spec:
  length: 32
  compiler: { roles: [alu_rr] }          # optional, see "Compiler roles"
  fields:
    - { name: opcode, start: 0,  width: 7, role: opcode }
    - { name: rd,     start: 7,  width: 5, role: register, type: gpr }
    - { name: funct3, start: 12, width: 3, role: constant, type: enum.F3_ALU }
    - { name: rs1,    start: 15, width: 5, role: register, type: gpr }
    - { name: rs2,    start: 20, width: 5, role: register, type: gpr }
    - { name: funct7, start: 25, width: 7, role: constant, type: enum.F7_ALU }
  constraints:                            # optional decode-time checks
    - { expr: "rd != 0", message: "rd must not be r0" }
```

## `length`

The instruction width in bits. **All schemas in one ISA share one length**
(no variable-length encodings yet). Any uniform width up to 512 bits works
for the compiler; the QEMU simulator fetches up to 64-bit words and rejects
wider encodings with an explanatory error.

## Fields

Each field is `{name, start, width, role}` plus an optional `type`. `start`
is the least-significant bit (bit 0 is the LSB of the instruction word);
fields must not overlap and must fit within `length`.

| `role` | The bits hold… | `type` |
|---|---|---|
| `opcode` | the instruction's opcode, filled from its `opcode:` | — |
| `constant` | a per-instruction fixed value (`funct3: …`) | optional `enum.<Enum>` |
| `reserved` | always zero | — |
| `register` | an index into a register file | **required**: the file name (`gpr`) |
| `immediate` | an operand value | `signed`, `enum.<Enum>`, or `struct.<Operand>` |

The field *name* is how behaviors refer to it: a schema with fields `rd`,
`rs1`, `rs2` lets an instruction say `behavior: "rd = rs1 + rs2"`. Names are
yours to choose — nothing is special about `rd`/`rs1`.

An `immediate` typed `signed` is sign-extended where the behavior asks for it
and gets a signed operand type in the compiler; untyped immediates are
unsigned.

## Split immediates

Hardware often scatters an immediate's bits to keep register fields in fixed
positions. Name the pieces `imm_<high>` or `imm_<high>_<low>` (the bit range
of the *logical* immediate each piece carries) and the tool reassembles them
everywhere — decoder, compiler patterns, fixups, assembler:

```yaml
# A 13-bit branch offset, bit 0 implied zero, scattered across four fields
# (this is pico32's B-type, from the tutorial):
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

In the behavior you reassemble the logical value with bit concatenation —
note the literal `0` supplying the implied bit 0:

```yaml
behavior: |
  if rs1 == rs2:
      pc = pc + sext({imm_12, imm_11, imm_10_5, imm_4_1, 0}, 13)
```

The widths must be consistent: each piece's `width` must match its name's bit
range, or validation fails.

## `constraints`

Decode-time checks, written in the same expression language as behaviors.
They become rejection checks in the QEMU decoder (the instruction faults as
illegal, with your message logged) and assertions in the generated intrinsics:

```yaml
constraints:
  - { expr: "rs1 != rs2", message: "source registers must differ" }
  - "imm % 4 == 0"            # string shorthand — message defaults to the expr
```

## Schema-level compiler roles

`compiler: { roles: [...] }` on a schema declares the *shape* of every
instruction using it — e.g. "everything in this format is a register-register
ALU op" (`alu_rr`) or "everything here is a conditional branch" (`branch`).
The specific operation still comes from each instruction's behavior. See
[compiler roles & coverage](../compiler/roles-and-coverage.md).

## Current boundaries

- One uniform instruction length per ISA. Mixing 16- and 32-bit encodings in
  one ISA fails validation with `mixed instruction widths [16, 32] are not
  supported`.
- Encodings wider than 64 bits generate an LLVM backend but not a QEMU
  target; the error explains the simulator's fetch/decode ceiling.
