# The manifest format

An ISA is described by YAML manifests. Every manifest — regardless of kind —
has the same envelope:

```yaml
apiVersion: isa-archive/v1
kind: ISA | Schema | Instruction | Operand | Enum | Constant | uArch
metadata:
  name: <unique name>
  description: <optional, shows up in generated manuals>
spec:
  # ...kind-specific fields...
```

Multiple manifests can live in one file, separated by `---`.

## Kinds at a glance

| Kind | Declares | Reference |
|---|---|---|
| `ISA` | The root: data width, registers, CSRs, ABI, machine, target identity | [isa.md](isa.md) |
| `Schema` | One instruction bit-layout, shared by many instructions | [schemas.md](schemas.md) |
| `Instruction` | One operation: schema + fixed field values + behavior | [instructions.md](instructions.md) |
| `Operand` | A structured value type (named bit-fields) | [types.md](types.md) |
| `Enum` | Named values for a field (`F3_BRANCH.BEQ`) | [types.md](types.md) |
| `Constant` | A named number (`opcode: STORE`) | [types.md](types.md) |
| `uArch` | A micro-architecture implementing the ISA (for `-t verilog`) | [uarch.md](uarch.md) |
| `Project` | A build config: which targets to generate, and where (`isa-archive build`) | [project.md](project.md) |

Instruction semantics are written in a small Python-like language — the
[behavior DSL](behavior.md).

## Validation is strict

Unknown keys are **errors**, never silently ignored. A typo can't quietly
change your architecture:

```
$ isa-archive parse bad.yaml
Error: 1 validation error for ISA
spec.byte_oder
  Extra inputs are not permitted [type=extra_forbidden, input_value='big', input_type=str]
```

Beyond key checking, `isa-archive parse` validates: field positions stay
inside the schema length and don't overlap; register fields are wide enough to
address their register file; no two instructions share the same fixed
encoding (decoder collisions); every referenced schema/enum/constant exists;
behavior expressions have consistent bit widths.

## Multi-file projects: `includes:`

Real ISAs don't fit one file. The ISA manifest pulls in the rest by glob,
relative to its own location:

```yaml
spec:
  includes:
    - "schemas.yaml"
    - "constants.yaml"
    - "instructions/*.yaml"
```

A typical layout (this is `examples/tutorial/pico32-part4/`):

```
pico32-part4/
  isa.yaml              # the ISA root
  schemas.yaml          # all the bit layouts
  constants.yaml        # opcodes, enums
  instructions/
    alu.yaml
    memory.yaml
    control.yaml
```

## Building on another ISA: `extends:`

An ISA can inherit everything from a base and add to it:

```yaml
# examples/tutorial/pico32-part4/mul/isa.yaml — the MUL extension
spec:
  version: "0.4"
  extends: "../isa.yaml"
  includes:
    - "instructions.yaml"     # adds MUL
```

The extension gets the base's registers, schemas, constants, and
instructions, then layers on its own. Generate from the extension's
`isa.yaml` and you get the combined ISA.
[Tutorial part 4](../../examples/tutorial/pico32-part4/README.md) builds one.
