# Quickstart - five minutes, no builds

Everything here runs instantly: no QEMU build, no LLVM build. We'll use the
bundled pico32 example as the guinea pig, then scaffold an ISA of your own.

## 1. Validate a real ISA

```sh
$ isa-archive parse examples/tutorial/pico32-part4/isa.yaml
Validated examples/tutorial/pico32-part4/isa.yaml
  [pico32]  pico32 v0.4  xlen=32  8 schemas  13 instructions  0 operands  0 CSRs
```

The loader checks everything before any generator runs: bit-field bounds and
overlaps, register-field widths, duplicate encodings, unknown keys. A typo is
an error, not a silent default:

```sh
$ isa-archive parse bad.yaml
Error: 1 validation error for ISA
spec.byte_oder
  Extra inputs are not permitted [type=extra_forbidden, ...]
```

## 2. Generate a reference manual

```sh
$ isa-archive generate --isa examples/tutorial/pico32-part4/isa.yaml -t docs -f html -o build/manual
$ open build/manual/pico32_reference.html
```

A browsable instruction reference - encodings, fields, behaviors, CSRs - straight
from the YAML.

## 3. Generate an assembler and assemble a program

```sh
$ isa-archive generate --isa examples/tutorial/pico32-part4/isa.yaml -t asm -o build/asm
$ cat > tiny.s <<'EOF'
.text
    addi a0, zero, 42
    add  a1, a0, a0
EOF
$ python3 build/asm/pico32_asm.py tiny.s -o tiny.bin
Written 8 bytes → tiny.bin
$ xxd tiny.bin
00000000: 1305 a002 b305 a500                      ........
```

A self-contained assembler that knows your mnemonics, register names, ABI
aliases, and encodings - generated, not written.

## 4. Scaffold your own ISA

```sh
$ isa-archive init my-isa --xlen 32 --output-dir .
Created my-isa/ with 3 files.

  isa.yaml          - ISA root (xlen=32, 32 GPRs)
  layouts.yaml      - RType instruction schema
  instructions.yaml - ADD instruction

$ isa-archive parse my-isa/isa.yaml
```

Three small files, fully valid, every generator works on them.

## Where next

- **Build a complete ISA, run programs on it, compile C for it** → the
  [pico32 tutorial](../../examples/tutorial/README.md). It starts exactly where
  step 4 left off.
- **Understand the moving parts first** → [Concepts](concepts.md).
- **Look up YAML fields as you go** → the [manifest reference](../yaml/README.md).
