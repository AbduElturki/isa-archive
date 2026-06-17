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
| `<Isa>Enums.h` | `enum class Op` (one per instruction) + `mnemonic()`; `RegClass` + `reg_class_name()`; `Category` (from each instruction's `exec_type`); `OperandKind`. For register files typed with a custom [`ScalarType`](../yaml/types.md), a `using <file>_elem_t = …;` typedef and its `#include`. |
| `<Isa>InstrInfo.h` | `struct InstrInfo` / `OperandInfo` and an `info(Op)` table: opcode, `mask`/`match`, operands (name, bit range, kind, register class), category, `exec_type`, the source `behavior:` string, and `latency`. |
| `<Isa>Decoder.h` | `Op decode(word)` (most-specific match first), `get_bits()` / `sext()`, and `decode_imm(Op, word)` that reassembles split and signed immediates. Words wider than 64 bits use a little-endian byte-array `Word` plus a `get_bits_wide()` accessor for fields that don't fit in 64 bits (see below). |
| `<Isa>Encoder.h` | `encode_<OP>(operands…) -> word`, one inline function per instruction - the **inverse** of the decoder. Sets the fixed bits from the manifest and places each register index / immediate (distributing split immediates) into its schema field. |
| `<Isa>.h` | umbrella header - includes the four above. |

`<Isa>` is the ISA name in PascalCase (`npu-probe` → `NpuProbe`); the C++ namespace is the
lowercase form (`npu_probe`).
| `example_main.cpp`, `INTEGRATE.md` | a usage sketch and step-by-step adoption notes. |

A `.clang-format` is written beside them; pass `--clang-format` to format the output in place.

Include the umbrella and you have the whole description:

```cpp
#include "NpuProbe/NpuProbe.h"

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

This is also **the decoder and encoder** for the project: `decode()` + `info()` + `decode_imm()`
recover an instruction and its operands from raw bytes, and `encode_<OP>(…)` builds the word back
from operands. Both derive from the same manifest field layout, so they round-trip - the test suite
encodes with the encoder and decodes with the decoder and checks they agree. There is intentionally
no separate LLVM `MCDisassembler`; the LLVM target covers the MC *encoder* path (code emitter,
fixups), and these headers are the standalone, compile-tested decode/encode side.

## Decoding any instruction width

For instruction words up to 64 bits the `Word` is a plain `uint64_t`. For wider encodings (up to the
512-bit cap - accelerator ISAs like a 320-bit NPU word) the `Word` is a little-endian
`std::array<uint8_t, N>` and:

- `get_bits(word, start, width)` reads up to **64 bits** of a field (the common case: opcodes,
  register indices, immediates).
- `get_bits_wide(word, start, width, out)` reads a field of **any width** into a little-endian byte
  buffer - for a field that doesn't fit in 64 bits, such as a structured operand. Its sub-fields,
  each ≤ 64 bits, are still read with `get_bits`.
- `decode()` matches fixed fields (opcode/constant/reserved) in ≤ 64-bit chunks, so every bit of a
  field wider than 64 bits is checked - a wide reserved or constant field is never truncated.

## Custom element types

A register file whose element is a [`kind: ScalarType`](../yaml/types.md) with a `cpp_type` /
`cpp_include` gets a `using <file>_elem_t = <cpp_type>;` typedef in `<Isa>Enums.h`, and the header
is `#include`d so the typedef resolves. The bundled
[`npu-probe`](../../examples/npu-probe/) example does this for its `fp8_e4m3` tile/vector files
(`#include <npu_fp8.h>`, `using qreg_elem_t = fp8e4m3_t;`).

## Current boundaries

- Descriptive only - `behavior:` is a string, not generated code; multi-statement or exotic
  behaviors are not decomposed into anything beyond that string.
- You supply any headers your custom element types name (the generated `#include` just references
  them).
