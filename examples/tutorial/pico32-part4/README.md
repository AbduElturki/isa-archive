# Part 4 — Growing the ISA

pico32 works end to end. This part is about *living* with it: adding an
instruction without forking, shipping headers and a manual, and a first look
at the hardware side. Shorter than the others — you already know the moves.

The finished files are in this directory.

## An extension, not a fork

C programs that multiply currently fail to link — pico32 has no multiply, so
the compiler emits a runtime-library call that isn't there. Let's add `MUL`.
Rather than edit pico32, we layer an extension on top with `extends:`.

A new directory `mul/` beside the ISA, with its own root:

```yaml
# mul/isa.yaml
apiVersion: isa-archive/v1
kind: ISA
metadata:
  name: pico32m
  description: pico32 + hardware multiply — an extension, not a fork.
spec:
  version: "0.4"
  extends: "../isa.yaml"      # inherit all of pico32
  includes:
    - "instructions.yaml"     # add what's new
```

```yaml
# mul/instructions.yaml
apiVersion: isa-archive/v1
kind: Instruction
metadata:
  name: MUL
  description: Integer multiplication (low 32 bits)
spec:
  schema: RType              # reuse pico32's R-type layout
  exec_type: mul_int
  opcode: OP
  funct3: F3_ALU.ADD_SUB
  funct7: F7_ALU.MULDIV      # a new F7_ALU enum value, added in part 4's constants.yaml
  behavior: "rd = rs1 * rs2"
  description: "rd ← (rs1 × rs2)[31:0]"
```

`pico32m` inherits pico32's registers, schemas, ABI, machine, *and* its target
identity (triple, profile) — `extends:` carries the architecture's identity,
not just its instruction list. Validate to see the combined ISA:

```sh
$ isa-archive parse pico32/mul/isa.yaml
Validated pico32/mul/isa.yaml
  [pico32m]  pico32m v0.4  xlen=32  8 schemas  14 instructions  0 operands  0 CSRs
  [pico32]   pico32  v0.4  xlen=32  8 schemas  13 instructions  0 operands  0 CSRs
```

Fourteen instructions: pico32's thirteen plus MUL. The compiler infers the
`alu_rr.mul` role from `rd = rs1 * rs2` — no tag needed.

This same pattern hosts **several independent extensions** off one base — this
directory also ships [`fp/`](fp/README.md) (single-precision floating point: a
second register class + hard-float ABI) and [`sys/`](sys/README.md)
(control/status registers). Each `extends: ../isa.yaml` on its own.

## Rebuild and watch the compiler use it

Generating from `pico32m` produces one self-contained backend — the
extension already carries everything it inherited, so you build `pico32m`'s
toolchain the same way you built pico32's in parts 1 and 3 (the
`qemu-system-pico32` you built earlier stays put; this adds
`qemu-system-pico32m`). With the multiply instruction available, the same
`a * b` that failed to link now compiles to one instruction:

```sh
$ clang --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 \
        -nostdlib -ffreestanding -O1 -S mul.c -o -
...
	mul	r3, r4, r3           # one instruction, not a library call
```

(Source and the full build commands are in this directory's `programs/` and in
[the compiler build guide](../../../docs/compiler/build-and-use.md).) Run it:

```sh
$ qemu-system-pico32m -M pico32m-virt -display none -serial stdio \
      -monitor none -bios none -kernel mul.elf
6 x 7 = OK
$ echo $?
0
```

One YAML file turned `*` from a link error into a hardware multiply.

## Ship headers for software

Custom instructions are only useful if software can reach them.
[`-t c` / `-t rust`](../../../docs/targets/intrinsics.md) emit typed headers —
inline-asm wrappers, operand structs, CSR accessors:

```sh
isa-archive generate --isa pico32/mul/isa.yaml -t c -o build/sw
```

```c
// build/sw/pico32m_intrinsics.h — call MUL from C
static inline uint32_t isa_archive_mul(uint32_t rs1, uint32_t rs2) { ... }
```

## Ship a manual

[`-t docs`](../../../docs/targets/reference-manuals.md) turns the same
manifests — and your `description:` fields — into a reference manual:

```sh
isa-archive generate --isa pico32/mul/isa.yaml -t docs -f pdf -o build/manual
# build/manual/pico32m_reference.pdf
```

## A glimpse of hardware

Everything so far was software. The [`verilog` target](../../../docs/targets/verilog.md)
turns your ISA + a [uArch manifest](../../../docs/yaml/uarch.md) into
SystemVerilog. A minimal chassis, `uarch.yaml`:

```yaml
apiVersion: isa-archive/v1
kind: uArch
metadata: { name: pico32-tiny }
spec:
  isa: pico32
  blocks:
    - { name: IntegerALU,    count: 1, latency: 1, pipelined: true,
        handles: [alu_int, alu_branch, alu_jump] }
    - { name: LoadStoreUnit, count: 1, latency: 2, pipelined: true,
        handles: [mem_load, mem_store] }
```

The `handles:` lists match the `exec_type` tags we added to the instructions
in this part. Generate:

```sh
isa-archive generate --isa pico32/isa.yaml --uarch pico32/uarch.yaml \
    -t verilog -o build/rtl
# pico32_operands.sv, pico32-tiny_IntegerALU.sv, pico32-tiny_LoadStoreUnit.sv,
# pico32-tiny_top.sv
```

Each block module implements — in synthesizable SystemVerilog, from the same
`behavior:` lines — every instruction it handles. It's a starting skeleton,
not a finished core (see [the Verilog target's boundaries](../../../docs/targets/verilog.md#what-it-is--and-isnt)).

## Where to go from here

You've taken an ISA from an empty directory to a simulator, an assembler, a C
compiler, software headers, a manual, and an RTL skeleton — all from YAML.
Next:

- **[`fp/`](fp/README.md)** — a second register class (floating point) and a
  hard-float calling convention, layered on with `extends:`.
- **[`sys/`](sys/README.md)** — the machine's control/status registers.
- **[`examples/npu-probe`](../../npu-probe/README.md)** — what this looks like
  for an accelerator that *doesn't* run C (`kernel-only` profile, 128-bit
  vectors, big-endian).
- **The [manifest reference](../../../docs/yaml/README.md)** — your companion now
  that you know the shape of the thing.

## Current boundaries

- The MUL extension shares pico32's R-type layout; a genuinely new encoding
  shape would add a Schema too.
- The generated SystemVerilog parameterizes structure from `count` / `latency`
  / `pipelined` but doesn't synthesize hazard or pipeline-control logic — that
  remains yours to build.
