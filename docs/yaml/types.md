# Operand, Enum, Constant, ScalarType

Small kinds that keep the big ones readable.

## Constant - a named number

```yaml
apiVersion: isa-archive/v1
kind: Constant
metadata: { name: STORE, description: memory store opcode }
spec: { value: 0x23, width: 7 }
```

Referenced by bare name wherever a fixed value is needed:
`opcode: STORE`.

## Enum - named values for a field

```yaml
apiVersion: isa-archive/v1
kind: Enum
metadata: { name: F3_BRANCH, description: branch condition selector }
spec:
  width: 3
  values: { BEQ: 0, BNE: 1, BLT: 4, BLTU: 6 }
```

Two reference forms:
- on a schema field: `type: enum.F3_BRANCH` - documents what the field means
  and validates fills against the member list;
- in an instruction: `funct3: F3_BRANCH.BEQ`.

## Operand - a structured value type

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

1. **Behaviors** - construct and access them like structs:
   ```yaml
   behavior: |
     v = Vec2(rs1, rs2)
     rd = v.lo + v.hi
   ```
2. **Immediate fields** - a schema field typed `struct.Vec2` decodes into the
   structured form.
3. **Register files** - `type: Vec2` on a register file declares that its
   registers hold this structure (the compiler treats the file as opaque
   `i<width>` storage; behaviors get the fields).

They also become real `struct`s in the generated C/Rust headers
([`-t c` / `-t rust`](../targets/intrinsics/README.md)), so software constructs the
same values the hardware decodes.

## ScalarType - a custom element type

The built-in element types (`i8`…`i128`, `f16`/`bf16`/`f32`/`f64`/`f128`) cover
integer and IEEE-float ISAs. A `kind: ScalarType` adds one the table doesn't
carry - sub-byte ints, FP8 formats, `tf32`, … - so a [register file's](isa.md)
`type:` can name it:

Each backend speaks a different language, so the type name (and the **header** that
provides it, when it isn't a built-in) are declared **per backend**, each part
optional. Providing a backend's `(type, include)` *enables* the type there.

```yaml
apiVersion: isa-archive/v1
kind: ScalarType
metadata: { name: fp8_e4m3, description: "8-bit float, E4M3" }
spec:
  width: 8
  arith_class: ieee_float    # "int" (default) or "ieee_float"

  llvm_mvt: f8E4M3           # LLVM value type. OMIT → files using this type are
                            #   simulator-only (not an LLVM register class).
  c_type: fp8_e4m3_t         # QEMU/C type (used in the u2f/f2u float helpers).
  c_include: "<fp8.h>"       # header for c_type; only needed if it isn't a C built-in.
  cpp_type: fp8_e4m3_t       # cpp-isa C++ type   (defaults to c_type)
  cpp_include: "<fp8.h>"     # header for cpp_type (defaults to c_include)
```

```yaml
# then, in state.registers:
- { name: qreg, width: 8, count: 16, type: fp8_e4m3 }
```

Every representation is **optional** - a type declares only the backends it supports:

- **`llvm_mvt`** - omit and the type has no LLVM value type, so register files using it
  stay simulator-only (the symmetric counterpart of an absent `c_type`). LLVM uses
  built-in MVTs and emits no header, so there is no LLVM include.
- **`c_type` / `c_include`** - the C type QEMU computes with and its header. With a
  `c_type`, QEMU arithmetic on the type is enabled and the header is `#include`d in the
  generated helpers; with `c_type: null` the type **stores and moves** but has no host
  arithmetic (like the built-in `f16`).
- **`cpp_type` / `cpp_include`** - the C++ type for the cpp-isa headers and its header,
  each defaulting to the C equivalent. cpp-isa emits the `#include` plus a
  `using <file>_elem_t = <cpp_type>;` typedef per register file.

An include is written with its delimiters (`<fp8.h>` or `"fp8.h"`), or bare (`fp8.h`,
emitted as `<fp8.h>`). Genuinely novel numerics (fixed-point, posit) still need custom
lowering and are out of scope. The [`npu-probe`](../../examples/npu-probe/) example
declares `fp8_e4m3` with a C type + header and types its `qreg`/`tile` files with it.

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

Operands map cleanly onto register files (`maps_to_state:`) and nest, so a
packed pair can name a register-file element directly.
