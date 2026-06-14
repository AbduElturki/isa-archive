# npu-probe — generality probe for non-CPU targets

This example exists to keep the generators honest beyond RISC-style CPUs. It is
deliberately *not* a C-compilable target; it exercises the paths an accelerator
(GPU/NPU-style) ISA hits:

| Feature | What it exercises |
|---|---|
| `byte_order: big` | `TARGET_BIG_ENDIAN=y` in the QEMU config; big-endian NOP bytes in the LLVM AsmBackend |
| 64-bit instruction words | wide fetch (`translator_ldq`) and `bits<64>` formats |
| `vreg` (128-bit × 16) | native `__uint128_t` QEMU state (no TCG global), no LLVM register class |
| `VADD` | true 128-bit arithmetic in the QEMU helper; omitted from LLVM with a warning |
| `preg` (1-bit × 8) | QEMU helper-only access with masked writes (no `uint1_t`), excluded from LLVM (no `MVT::i1` class) |
| `gpr` with no aliases | alias-less register file handling — sp/ra/zero are *not* invented |
| `PSET_LT` | an instruction writing a non-codegen file: generated for QEMU, omitted from LLVM with a warning |
| `compiler.profile: kernel-only` | a stack-less compute target is COMPILER-COMPLETE on its own terms |

## Expected generation behavior

```sh
isa-archive generate --isa examples/npu-probe/isa.yaml -t qemu -o build/npu-qemu
isa-archive generate --isa examples/npu-probe/isa.yaml -t llvm -o build/npu-llvm
```

Both succeed. The LLVM backend warns that `vreg`/`preg` are kept as
architectural state and that `VADD`/`PSET_LT` are omitted. Because the ISA declares
`compiler.profile: kernel-only`, `COMPILER_COVERAGE.md` reports
**COMPILER-COMPLETE** — nothing is required of a compute-only target. Remove
the profile (default `c-baremetal`) to see the C-lowering contract instead:
missing roles *and* missing `alias:sp`/`alias:ra`/`alias:zero` prerequisites.

## Known boundaries (current, intentional)

- **Arithmetic on >64-bit registers other than exactly 128 bits** (e.g. a
  256-bit file) is rejected loudly by the QEMU generator — 128-bit files
  compute natively via `__uint128_t`; other wide/vector widths arrive with
  vector element types (`type: v4i32`) per the generality plan.
- **Instruction words wider than 64 bits** are LLVM-only; the QEMU generator
  rejects them with an explanation (decodetree/fetch ceiling).
