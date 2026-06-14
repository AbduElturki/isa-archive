# Compiler roles and the coverage report

## Why roles exist

To generate instruction selection, the compiler must know which of your
instructions is "the add", "the 32-bit load", "the equality branch", "the
stack-pointer adjustment". A **role** is that assignment: a tag like
`alu_rr.add` or `frame.sp_adjust` binding a duty to an instruction.

You almost never write them. Roles resolve in three layers, each refining the
last:

1. **Inference from behavior** — `rd = rs1 + rs2` *is* `alu_rr.add`;
   `rd = mem32[rs1 + imm]` *is* `mem.load32`; `if rs1 == rs2: pc = …` *is*
   `branch.eq`. This covers most of a typical ISA with zero annotations.
2. **Schema-level shape defaults** — a schema can declare what its
   instructions are, e.g. `compiler: { roles: [branch] }` on a branch format;
   the specific condition still comes from each behavior.
3. **Instruction-level tags** — explicit assignments for duties that can't be
   inferred, because they're conventions rather than semantics.

Layer 3 in practice — the classic pair every `c-baremetal` ISA tags by hand:

```yaml
# LUI: "this is how you set the top of a register"
compiler: { roles: [const.hi, global.hi] }

# ADDI: "…and this is the low half, the SP adjuster, and the address low half"
compiler: { roles: [const.lo, global.lo, frame.sp_adjust] }
```

Why by hand? Nothing in `rd = rs1 + imm` says "use me to build large
constants" — that's a *convention* you're choosing, so you declare it.

## The role catalog

| Role family | Members | Typically |
|---|---|---|
| `alu_rr.*` | `add sub and or xor shl srl sra` | inferred from `rd = rs1 OP rs2` |
| `alu_ri.*` | `add and or xor shl srl sra` | inferred from `rd = rs1 OP imm` |
| `mem.*` | `load8s load8u load16s load16u load32 store8 store16 store32` (widths follow your xlen) | inferred from `mem*[...]` behaviors |
| `branch.*` | `eq ne lt ge ltu geu` | inferred from the branch condition |
| `cmp.*` | `lt ltu lti ltui` | inferred from compare-into-0/1 behaviors (SLT-style) |
| `control.*` | `jump call call_indirect ret` | `jump` inferred; `call`/`ret` tagged on your JAL/JALR equivalents |
| `const.*` | `hi lo load` | **tagged** (convention) |
| `global.*` | `hi lo` | **tagged** (address materialization) |
| `frame.sp_adjust` | — | **tagged** on your add-immediate |

## Constant materialization — a worked contrast

How does the compiler put `0x12345678` in a register? It infers a strategy
from your `const.*` tags plus the tagged instructions' *behaviors*:

- **pico32** (the tutorial): `const.hi` on LUI, `const.lo` on ADDI. ADDI
  **sign-extends** its immediate → strategy `hi_lo_add` (the high part
  compensates for the sign).
- A MIPS-style ISA with `const.hi` on LUI and `const.lo` on ORI: ORI
  **zero-extends** → strategy `hi_lo_or` (no compensation).
- An ISA with one full-width load-immediate tags it `const.load` →
  `single_imm`.

Same tags, different generated code — derived from what your instructions
actually do.

## Reading COMPILER_COVERAGE.md

Every `-t llvm` run writes one. The tutorial's pico32 report:

```markdown
# PICO32 compiler coverage

Profile: `c-baremetal`

- **ALU rr**: add ✓  sub ✓  and ✗  or ✓  xor ✗  shl ✗  srl ✗  sra ✗
- **ALU ri**: add ✓  and ✗  or ✗  xor ✗  shl ✗  srl ✗  sra ✗
- **Const**: hi ✓  lo ✓  load ✗
- **Memory**: load8s ✗  load8u ✗  load16s ✗  load16u ✗  load32 ✓  store8 ✗  store16 ✗  store32 ✓
- **Branch**: eq ✓  ne ✓  lt ✓  ge ✗  ltu ✓  geu ✗
- **Control**: jump ✓  call ✓  call_indirect ✓  ret ✓
- **Frame**: sp_adjust ✓
- **Global**: hi ✓  lo ✓
- **Const strategy**: `hi_lo_add`

## Custom-lowered instructions (no selectable pattern)
- `LUI`: complex expression

**STATUS: COMPILER-COMPLETE ✓** (profile `c-baremetal`)
```

How to read it:

- **✓/✗ per role** — what the backend can select directly. A ✗ isn't
  necessarily a problem: pico32 has no AND instruction, so C's `&` is simply
  not selectable — fine until a program needs it.
- **STATUS** — measured against the profile's *required* set, not every row.
  `c-baremetal` requires the core (add/sub, word load/store, eq/ne plus
  ordering comparisons, jump/ret, sp_adjust, a constant strategy, and the
  `zero`/`ra`/`sp` aliases). Missing items are listed by name — including
  `alias:sp`-style entries when register aliases are the gap.
- **Custom-lowered instructions** — behaviors the pattern-matcher couldn't
  turn into a selection pattern, with the reason. They still simulate; the
  compiler handles some (like LUI here) through dedicated code paths
  instead — that's why pico32 is COMPLETE despite the entry.

## `--strict`

```sh
isa-archive generate --isa my-isa/isa.yaml -t llvm -o build/llvm --strict
```

Turns an INCOMPLETE report into a non-zero exit with the missing items in the
error — make it your CI gate once your ISA first reaches COMPLETE:

```
Error: MY_ISA: profile 'c-baremetal' is missing ['branch.ne', 'alias:sp'].
Tag instructions with compiler.roles, declare the missing register aliases,
or set spec.compiler.profile to match the target...
```

## Current boundaries

- Inference covers single-statement ALU/load/store/compare/branch/jump
  shapes. Multi-statement or exotic behaviors become custom-lowered entries —
  correctness is unaffected; only selection quality is.
- Two instructions claiming one role is reported as a conflict in the report
  (first one wins) — resolve it by removing a tag.
- If a duty exists in your ISA but inference misses it, tag it explicitly at
  the instruction level; layer 3 always wins.
