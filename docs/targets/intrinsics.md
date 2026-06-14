# C and Rust intrinsics (`-t c`, `-t rust`)

```sh
isa-archive generate --isa my-isa/isa.yaml -t c    -o build/sw
isa-archive generate --isa my-isa/isa.yaml -t rust -o build/sw
```

Three files per language, for software that targets your ISA:

| File | Contents |
|---|---|
| `{isa}_intrinsics.h` / `.rs` | one inline-assembly wrapper per instruction |
| `{isa}_structs.h` / `.rs` | a typed struct per [Operand](../yaml/types.md), with constructors and field accessors matching the declared bit layout |
| `{isa}_csrs.h` / `.rs` | constants and accessors for your CSRs and their fields |

A generated struct, from `examples/rv32/base`'s `Point` operand:

```c
typedef struct __attribute__((packed)) {
    uint32_t x;
    uint32_t y;
} Point_t;

static inline Point_t Point(uint32_t x, uint32_t y) { ... }
```

The same bit layout your decoder decodes and your behaviors access — software
and hardware can't drift apart, because both come from the one manifest.

Schema/instruction [`constraints:`](../yaml/schemas.md#constraints) become
runtime assertions in the wrappers, so invalid operand combinations fail at
the call site rather than as silent wrong encodings.

## Using them

Compile with [your generated clang](../compiler/build-and-use.md) and include
the headers — the inline-assembly mnemonics are yours, so only your toolchain
assembles them:

```c
#include "pico32_intrinsics.h"
```

Register and immediate operands are both wired from the instruction's
operands. Instructions that take an immediate are emitted as function-like
macros (C) or `const`-generic functions (Rust), so the immediate reaches the
assembler as a literal. Instructions the compiler can't otherwise reach from
plain C — those with no automatic code-generation pattern — are flagged in
their doc comment, since the wrapper is the only way to call them.

## Current boundaries

- Wrappers cover operands that live in a single general-purpose register.
  Instructions whose operands are floating-point or wider-than-word register
  files (e.g. vector registers) are skipped, with a note on the console — they
  need typed register classes or vector intrinsics, not a scalar asm wrapper.
- Memory and branch-target immediates are wrapped as plain integer constants;
  review the inline assembly before relying on them for those operand kinds.
- Operand structs wider than standard C types carry a comment noting the
  packing caveat.
