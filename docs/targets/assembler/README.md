# The standalone assembler (`-t asm`)

```sh
isa-archive generate --isa my-isa/isa.yaml -t asm -o build/asm
```

## Files generated

| File | Purpose |
|---|---|
| `{isa}_asm.py` | A self-contained Python assembler (executable, zero dependencies). Knows your mnemonics, register names *and ABI aliases*, encodings (including [split immediates](../../yaml/schemas.md#split-immediates)), and the machine's load address. |
| `linker.ld` | A matching linker script for the `machine:` memory map (one per output directory). |

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
then sources, then the immediate). Branch and jump targets are labels -
the assembler computes the PC-relative offsets and packs the scattered
immediate bits.

## When this is your whole toolchain

- **Bring-up** - it exists the moment your YAML parses; nothing to build.
  [Tutorial part 1](../../../examples/tutorial/pico32-part1/README.md) runs its
  first program this way.
- **Invented encodings** - if your immediate placements don't match any
  existing architecture, [no stock linker can link your objects](../compiler/build-and-use.md#linking-the-elf-reality);
  this assembler doesn't need one (it places the whole program itself).
- **Encoding checks** - quick "does this assemble to the bits I expect"
  experiments, golden files in CI.

## Current boundaries

This project's boundaries are consolidated in one place - see [Limitations](../../limitations.md#assembler).
