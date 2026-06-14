# Instruction — one operation

```yaml
apiVersion: isa-archive/v1
kind: Instruction
metadata:
  name: BEQ
  description: Branch if equal          # shows in the generated manual
spec:
  schema: BType                         # which bit layout
  opcode: BRANCH                        # int literal, or a Constant name
  funct3: F3_BRANCH.BEQ                 # fills the schema's constant fields
  behavior: |
    if rs1 == rs2:
        pc = pc + sext({imm_12, imm_11, imm_10_5, imm_4_1, 0}, 13)
  description: "if rs1 == rs2: pc ← pc + sext(offset)"
  exec_type: alu_branch                 # optional: routes to uArch blocks
  constraints: []                       # optional decode-time checks
  compiler: { roles: [...] }            # optional role tags
```

## The minimum viable instruction

Three keys: `schema`, `opcode`, `behavior`.

```yaml
spec:
  schema: RType
  opcode: 0x33
  funct3: 0          # every constant field of the schema needs a value
  funct7: 0
  behavior: "rd = rs1 + rs2"
```

## Filling constant fields

Any key in `spec:` that isn't a known keyword is taken as a **constant-field
fill**: `funct3: 0` sets the schema's `funct3` field. Values can be:

- an integer literal: `funct3: 2`
- a `Constant` name: `opcode: STORE`
- an `Enum` member: `funct3: F3_BRANCH.BEQ` (when the field is typed
  `enum.F3_BRANCH`)

Every `constant`-role field in the schema must be filled; the loader rejects
two instructions whose fixed fields collide (they'd be undecodable).

## `behavior`

What the instruction does, in the [behavior DSL](behavior.md). Field names
from the schema are the variables. This single definition drives the
simulator, the compiler's instruction selection, the hardware model, and the
manual.

## `exec_type`

A free-form tag (e.g. `alu_int`, `mem_load`, `alu_branch`) connecting the
instruction to [uArch](uarch.md) functional blocks: a block that
`handles: [alu_int]` executes every instruction tagged `alu_int`. Only the
Verilog generator consumes it — omit it until you generate hardware.

## `compiler.roles`

Instruction-level role tags, the most specific layer of the
[role system](../compiler/roles-and-coverage.md). You rarely need them — most
roles are inferred from the behavior. The classic exceptions (constant
materialization, stack adjustment) look like:

```yaml
# on LUI
compiler: { roles: [const.hi, global.hi] }
# on ADDI
compiler: { roles: [const.lo, global.lo, frame.sp_adjust] }
```

## `description`

Free text. `metadata.description` is the one-liner; `spec.description` is the
semantics line. Both flow into `-t docs` reference manuals and into comments
in generated code.

## Current boundaries

- A behavior the DSL can't express fails generation **loudly, naming the
  instruction**:

  ```
  Error: pico32: QEMU generation failed for 1 instruction(s):
    - instruction 'WEIRD': Unsupported syntax in behavior: 'while x: ...'
  ```

  The supported constructs are listed in [behavior.md](behavior.md).
- A behavior the *compiler* can't turn into a selection pattern still
  simulates fine; the instruction is listed under "Custom-lowered
  instructions" in `COMPILER_COVERAGE.md` with the reason.
