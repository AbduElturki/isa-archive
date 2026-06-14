# Operand, Enum, Constant

Three small kinds that keep the big ones readable.

## Constant — a named number

```yaml
apiVersion: isa-archive/v1
kind: Constant
metadata: { name: STORE, description: memory store opcode }
spec: { value: 0x23, width: 7 }
```

Referenced by bare name wherever a fixed value is needed:
`opcode: STORE`.

## Enum — named values for a field

```yaml
apiVersion: isa-archive/v1
kind: Enum
metadata: { name: F3_BRANCH, description: branch condition selector }
spec:
  width: 3
  values: { BEQ: 0, BNE: 1, BLT: 4, BLTU: 6 }
```

Two reference forms:
- on a schema field: `type: enum.F3_BRANCH` — documents what the field means
  and validates fills against the member list;
- in an instruction: `funct3: F3_BRANCH.BEQ`.

## Operand — a structured value type

An Operand gives internal structure to a value: named bit-fields, optionally
nested, with constraints.

```yaml
apiVersion: isa-archive/v1
kind: Operand
metadata:
  name: Vec2
  description: Pair of 16-bit lanes packed into a 32-bit register
spec:
  width: 32
  fields:
    - { name: lo, start: 0,  width: 16 }
    - { name: hi, start: 16, width: 16 }
  constraints:
    - { expr: "lo != hi", message: "Vec2 lanes must be distinct" }
```

Operands appear in three places:

1. **Behaviors** — construct and access them like structs:
   ```yaml
   behavior: |
     v = Vec2(rs1, rs2)
     rd = v.lo + v.hi
   ```
2. **Immediate fields** — a schema field typed `struct.Vec2` decodes into the
   structured form.
3. **Register files** — `type: Vec2` on a register file declares that its
   registers hold this structure (the compiler treats the file as opaque
   `i<width>` storage; behaviors get the fields).

They also become real `struct`s in the generated C/Rust headers
([`-t c` / `-t rust`](../targets/intrinsics.md)), so software constructs the
same values the hardware decodes.

## All three together

```yaml
apiVersion: isa-archive/v1
kind: Constant
metadata: { name: VOP }
spec: { value: 0x0B, width: 7 }
---
apiVersion: isa-archive/v1
kind: Enum
metadata: { name: VFUNC }
spec: { width: 3, values: { PACK: 0, SWAP: 1 } }
---
apiVersion: isa-archive/v1
kind: Schema
metadata: { name: VType }
spec:
  length: 32
  fields:
    - { name: opcode, start: 0,  width: 7, role: opcode }
    - { name: rd,     start: 7,  width: 5, role: register, type: gpr }
    - { name: vfunc,  start: 12, width: 3, role: constant, type: enum.VFUNC }
    - { name: rs1,    start: 15, width: 5, role: register, type: gpr }
    - { name: rs2,    start: 20, width: 5, role: register, type: gpr }
    - { name: pad,    start: 25, width: 7, role: reserved }
---
apiVersion: isa-archive/v1
kind: Instruction
metadata: { name: VPACK, description: pack two halfwords }
spec:
  schema: VType
  opcode: VOP
  vfunc: VFUNC.PACK
  behavior: "rd = {rs2[0:16], rs1[0:16]}"
  description: "rd ← {rs2[15:0], rs1[15:0]}"
```

`examples/rv32/base/types.yaml` and `examples/showcase/` have more.
