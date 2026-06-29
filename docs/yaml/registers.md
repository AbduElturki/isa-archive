# Register files

Register files are declared under an [ISA](isa.md)'s `state.registers`. Each file is
independent architectural state with its own width and count; an ISA can declare
several (integer, floating point, vector, predicate, …).

```yaml
state:
  registers:
    - name: gpr             # architectural name, used by schema fields
      width: 32             # bits per register
      count: 32
      zero_register: 0      # optional: this index always reads 0
      canonical_prefix: x   # registers named x0..x31 (default: first letter)
      type: i32             # element type (optional, see below)
      aliases:              # ABI names (optional but important - see below)
        zero: 0
        ra: 1
        sp: 2
        a0: 10
```

## `aliases` - the source of all CPU conventions

`sp`, `ra`, `zero`, and the argument/saved-register names come *only* from here.
Nothing is ever guessed from register positions: an ISA with no `sp` alias simply
has no stack pointer anywhere in the generated code (correct for accelerator-style
targets). To compile C, declare at least `zero`, `ra`, and `sp` - the coverage
report tells you if they're missing.

## `type` - the element type

- a built-in scalar - `i32` (default), `f32`/`f64` (an IEEE-float file: float
  arithmetic, float load/store, and a float calling convention),
- a custom [`kind: ScalarType`](types.md#scalartype---a-custom-element-type) you
  declared (sub-byte ints, FP8, tf32, …),
- or an [Operand](types.md) name - the file holds structured values, treated as
  opaque storage by the compiler.

## `shape` - vectors and tiles

`shape` makes each register an **N-dimensional array of `type` elements** - a vector
or a tile - instead of a scalar. `width` must equal element-width × product(shape).

```yaml
- { name: vec,  width: 128, count: 16, type: i32,      shape: [4] }     # 4-lane vector
- { name: tile, width: 512, count: 8,  type: fp8_e4m3, shape: [8, 8] }  # 8x8 tile
```

Behaviors index shaped registers down to an element - `vd[i]`, `td[i][j]` - and loop
over them (`for i in range(4): vd[i] = vs1[i] + vs2[i]`). In QEMU each file is an N-D
array in CPU state. In the compiler, a **1-D** integer/float vector file becomes a
vector register class (e.g. `v4i32`); the canonical element-wise loop lowers to a
vector op pattern, and a unit-stride load/store loop
(`for i: vd[i] = memW[base + i*esize]`) lowers to a vector load/store. Multi-dimensional
tiles and exotic-element files stay simulator-only. The
[`npu-probe`](../../examples/npu-probe/) example has both a `vec` and a `tile` file.

## `attributes` - per-register metadata

`attributes` declares per-register metadata carried alongside the data - a tile's
layout, dtype, a valid flag - read and written from behaviors as `reg.attr`:

```yaml
- name: tile
  width: 512
  count: 8
  type: fp8_e4m3
  shape: [8, 8]
  attributes:
    - { name: layout, width: 3 }
    - { name: valid,  width: 1 }
```

```yaml
behavior: "td.valid = 1"            # write an attribute
behavior: "td.layout = ts.layout"  # read and write
```

Attributes are per-register runtime state (an array indexed by register number in the
QEMU model). Like CSRs they're simulator-side: instructions that touch them are
custom-lowered in the compiler, not pattern-selected.

## Width matters per generator

Any width simulates. For the *compiler*, a file becomes an allocatable register class
only if it's float-typed, xlen-wide, or a 1-D vector; other files (1-bit predicates,
wide accumulators, multi-dimensional tiles) stay architectural state, and instructions
using them are simulator-only - see
[the compiler guide](../targets/compiler/README.md#register-files-and-the-compiler). In QEMU,
scalar files up to 64 bits (and exactly 128 bits) support direct arithmetic in
behaviors; details in [the QEMU guide](../targets/qemu/README.md#how-register-files-are-stored).

Three real configurations to crib from:

```yaml
# examples/tutorial/pico32-part4 - one integer file with ABI aliases
- { name: gpr, width: 32, count: 32, zero_register: 0, canonical_prefix: r,
    aliases: { zero: 0, ra: 1, sp: 2, a0: 10, ... } }

# examples/tutorial/pico32-part4/fp - integer + single-precision float
- { name: gpr, width: 32, count: 32, zero_register: 0 }
- { name: fpr, width: 32, count: 32, type: f32 }

# examples/npu-probe - accelerator: 128-bit vectors + 1-bit predicates
- { name: gpr,  width: 32,  count: 16 }
- { name: vreg, width: 128, count: 16 }
- { name: preg, width: 1,   count: 8 }
```

## Current boundaries

This project's boundaries are consolidated in one place - see [Limitations](../limitations.md#registers-and-state).
