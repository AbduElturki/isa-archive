# minimips — a second ISA that proves the pipeline is not RISC-V-specific

`minimips` is a 32-bit integer ISA used as an end-to-end proof that isa-archive
generates a working **compiler** and **functional model** from an ISA description,
not just for RISC-V. It deliberately differs from `examples/rv32/base` in the two
dimensions a real experiment is most likely to vary, and which used to be
hardcoded:

| Dimension | rv32i | minimips |
|-----------|-------|----------|
| Register file | `x0..x31` | `r0..r31` |
| ABI names | RISC-V (`ra`, `t0`, `a0`, `s0`…) | MIPS (`ra`, `t0`, `a0`, `v0`, `s0`, `at`, `k0`…) |
| Constant materialization | `LUI`+`ADDI` (`hi_lo_add`, sign-extended low) | `LUI`+`ORI` (`hi_lo_or`, zero-extended low) |
| Stack alignment | 16 | 8 |

Everything else (encoding shapes, relocations, the `riscv32` triple) is reused so
the generated backend links and runs through the same proven toolchain path — the
point is that **the YAML drives the register file, ABI, and constant strategy**,
none of which are baked into the generator.

## How the differences are expressed

* The register file and ABI live in `isa.yaml` under `spec.state.registers` and
  `spec.abi`.
* The constant strategy is **not** configured directly — it is *inferred* from
  the per-instruction `compiler.roles` tags:
  * `LUI` is tagged `const.hi`, and `ORI` (a zero-extending `ITypeALU_U`) is tagged
    `const.lo`. Because the low half zero-extends, the generator infers the
    `hi_lo_or` strategy.
  * `ADDI` is tagged `frame.sp_adjust` and `global.lo` (address materialization
    still uses the sign-extended `%hi/%lo` convention), but **not** `const.lo`.

## Verify it generates (fast)

```bash
uv run isa-archive generate -t llvm -i examples/minimips/isa.yaml -o /tmp/mm-llvm --strict
cat /tmp/mm-llvm/llvm/lib/Target/MINIMIPS/COMPILER_COVERAGE.md   # STATUS: COMPILER-COMPLETE ✓ (hi_lo_or)
uv run isa-archive generate -t qemu -i examples/minimips/isa.yaml -o /tmp/mm-qemu
```

## Build and run end-to-end (slow — one-time clones + builds)

```bash
bash examples/minimips/demo/01_build_qemu.sh     # → qemu-system-minimips
bash examples/minimips/demo/02_build_llvm.sh     # → clang with the MINIMIPS backend (~40 min)
bash examples/minimips/demo/03_run_demo.sh
#   Expected:  fib(10) == 55:  PASS   +   Hello, minimips!
```
