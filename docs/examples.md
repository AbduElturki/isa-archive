# Examples tour

Every directory under `examples/` exists to prove something specific. Use
this map to find the one closest to what you're building.

| Example | What it demonstrates |
|---|---|
| `rv32/base` | The reference: full RV32I (35 instructions), CSRs, operand structs, machine config — **build-validated end to end** (its generated clang compiles C that runs on its generated QEMU) |
| `rv32/m`, `rv32/bitops` | Extending a base ISA with `extends:` |
| `rv32/uarch/` | Two micro-architectures for one ISA (in-order vs superscalar) |
| `rv32/base/demo/` | Scripts that automate the full QEMU+LLVM build and run (`examples/demo/`) |
| `minimips` | A second compiler-complete ISA: MIPS-style registers and ABI, a different constant-materialization idiom — proof nothing is RISC-V-hardcoded |
| `showcase` | Two register classes (integer + f32 float), 64-bit instruction words, compare-then-branch control flow |
| `npu-probe` | An accelerator-style ISA: `kernel-only` profile, big-endian, 128-bit vector registers with working arithmetic, 1-bit predicates, no stack/ABI |
| `tutorial/pico32-part1…4` | The [tutorial](tutorial/README.md)'s snapshots — each part's finished state, independently generatable |

## rv32/base — the proven reference

Start here when you want to see how a complete, real ISA is organized:
multi-file layout (`schemas.yaml`, `constants.yaml`, `types.yaml`,
`instructions/*.yaml`), every field role in use, CSRs with field access
modes, explicit ABI, and the QEMU machine block. The end-to-end demo:

```sh
bash examples/demo/01_build_qemu.sh   # ~15 min: YAML → qemu-system-rv32i
bash examples/demo/02_build_llvm.sh   # ~40 min: YAML → clang
bash examples/demo/03_run_demo.sh     # compile fib.c & hello.c, run them
```

## minimips — "it's really not RISC-V inside"

Same proven toolchain path, completely different architecture flavor:
`r0..r31` registers, MIPS ABI aliases (`v0`, `at`, `k0`…), 8-byte stack
alignment, and `LUI`+`ORI` (zero-extending) constant materialization instead
of RISC-V's `LUI`+`ADDI` — all expressed in YAML, inferred by the generator.
Its README walks exactly which lines make the difference.

## showcase — multiple register classes and wide instructions

A 64-bit instruction word ISA with both an integer and an `f32` float
register file: float arithmetic, float load/store, a hard-float calling
convention, and SLT-style compare-then-branch lowering.

## npu-probe — the not-a-CPU

What the tool does when your target *isn't* a C-running CPU: `profile:
kernel-only` (COMPILER-COMPLETE with no stack, no calls), big-endian, 128-bit
vector adds that run in the simulator, 1-bit predicate registers, and an
alias-less register file. Its README states which boundary each feature
exercises.

## tutorial/pico32-part1…4 — your path

The from-scratch ISA the [tutorial](tutorial/README.md) builds, snapshotted
after each part. Diff your work against them when something doesn't behave:

```sh
isa-archive parse examples/tutorial/pico32-part3/isa.yaml
diff -r my-pico32/ examples/tutorial/pico32-part3/
```
