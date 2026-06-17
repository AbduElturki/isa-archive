# C++ ISA-description headers (`-t cpp-isa`)

```sh
isa-archive generate --isa my-isa/isa.yaml -t cpp-isa -o build/model
isa-archive generate --isa my-isa/isa.yaml -t cpp-isa -o build/model --clang-format
```

Standalone, header-only **C++17** that *describes* your ISA - the enums, decode logic, and
per-instruction metadata - so you can drop one shared definition into your own C++ code. It is
**opt-in** (not part of `-t all`).

Produces, per ISA, in `<out>/<isa>/`:

| File | Contents |
|---|---|
| `<isa>_enums.h` | `enum class Op` (one per instruction) + `mnemonic()`; `RegClass` + `reg_class_name()`; `Category` (from each instruction's `exec_type`); `OperandKind`. For register files typed with a custom [`ScalarType`](../yaml/types.md), a `using <file>_elem_t = …;` typedef and its `#include`. |
| `<isa>_info.h` | `struct InstrInfo` / `OperandInfo` and an `info(Op)` table: opcode, `mask`/`match`, operands (name, bit range, kind, register class), category, `exec_type`, the source `behavior:` string, and `latency`. |
| `<isa>_decode.h` | `Op decode(word)` (most-specific match first), `get_bits()` / `sext()`, and `decode_imm(Op, word)` that reassembles split and signed immediates. |
| `<isa>_model.h` | umbrella header - includes the three above. |
| `example_main.cpp`, `INTEGRATE.md` | a usage sketch and step-by-step adoption notes. |

A `.clang-format` is written beside them; pass `--clang-format` to format the output in place.

Include the umbrella and you have the whole description:

```cpp
#include "npu_probe/npu_probe_model.h"

npu_probe::Op op = npu_probe::decode(word);
const auto &i  = npu_probe::info(op);          // opcode, operands, category, latency, …
const char *m  = npu_probe::mnemonic(op);
```

## What it is - and isn't

It **is** a description: enums, a decoder, and instruction metadata generated from the same
manifests as every other target, so it can never drift from your simulator or compiler. The
intended use is to drop the shared decode + metadata into an **existing C++ performance or cycle
model** instead of hand-maintaining a parallel copy.

It **is not** a simulator. The `behavior:` string is carried verbatim as a *reference* (it is not
executable) - you write the compute in your own model. For an executable model, generate
[`-t qemu`](../qemu/README.md) instead.

## Custom element types

A register file whose element is a [`kind: ScalarType`](../yaml/types.md) with a `cpp_type` /
`cpp_include` gets a `using <file>_elem_t = <cpp_type>;` typedef in `<isa>_enums.h`, and the header
is `#include`d so the typedef resolves. The bundled
[`npu-probe`](../../examples/npu-probe/) example does this for its `fp8_e4m3` tile/vector files
(`#include <npu_fp8.h>`, `using qreg_elem_t = fp8e4m3_t;`).

## Current boundaries

- Descriptive only - `behavior:` is a string, not generated code; multi-statement or exotic
  behaviors are not decomposed into anything beyond that string.
- You supply any headers your custom element types name (the generated `#include` just references
  them).
