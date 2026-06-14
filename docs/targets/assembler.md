# The standalone assembler (`-t asm`)

```sh
isa-archive generate --isa my-isa/isa.yaml -t asm -o build/asm
```

Produces two files:

- **`{isa}_asm.py`** — a self-contained Python assembler. No dependencies, no
  build step. It knows your mnemonics, your register names *and ABI aliases*,
  your encodings (including [split immediates](../yaml/schemas.md#split-immediates)),
  and your machine's load address.
- **`linker.ld`** — a matching linker script for the `machine:` memory map.

## Assembling

```sh
$ python3 build/asm/pico32_asm.py hello.s -o hello.bin        # raw binary
$ python3 build/asm/pico32_asm.py hello.s -o hello.elf --elf  # ELF (for QEMU -kernel)
Written 120 bytes → hello.elf
```

Source syntax:

```asm
.text                       # sections: .text .data .bss .rodata
    lui   r1, 0x10000       # operands in schema order: registers, then immediates
    addi  r2, r0, 72
loop:                       # labels; branch/jump targets are labels or integers
    sw    r1, r2, 0         # comments with # or //
    bne   r2, r3, loop      # branches take labels (PC-relative, computed for you)
.data
    .word 1, 2, 3           # data directives: .byte .half .word .align
```

Operand order follows the schema's field order (destination register first,
then sources, then the immediate). Branch and jump targets are labels —
the assembler computes the PC-relative offsets and packs the scattered
immediate bits.

## When this is your whole toolchain

- **Bring-up** — it exists the moment your YAML parses; nothing to build.
  [Tutorial part 1](../../examples/tutorial/pico32-part1/README.md) runs its
  first program this way.
- **Invented encodings** — if your immediate placements don't match any
  existing architecture, [no stock linker can link your objects](../compiler/build-and-use.md#linking-the-elf-reality);
  this assembler doesn't need one (it places the whole program itself).
- **Encoding checks** — quick "does this assemble to the bits I expect"
  experiments, golden files in CI.

## Current boundaries

- One program, one placement: no relocatable objects, no cross-file symbols,
  no archives. For separate compilation and real linking, use the
  [generated LLVM toolchain](../compiler/build-and-use.md).
- Constants are evaluated per line; there's no expression language or macro
  facility.
