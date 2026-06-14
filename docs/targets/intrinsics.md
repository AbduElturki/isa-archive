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

## Current boundaries

- The intrinsic wrappers are scaffolding for custom-instruction experiments:
  review the operand constraints in the generated inline assembly before
  relying on them in anger (register operands are wired; complex
  immediate/memory operands may need hand-tuning for your use).
- Operand structs wider than standard C types carry a comment noting the
  packing caveat.
