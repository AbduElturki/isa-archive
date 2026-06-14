"""Integration tests for the generated LLVM backend (generation-level, no clang build).

Covers the compiler-role contract, coverage report, constant-materialization
strategies, and the Phase-1 compare-then-branch / select lowering.

The example ISAs were consolidated to pico32 (+ its fp/ extension) and npu-probe;
scenarios pico32 deliberately can't express — OR-based constant materialization
(hi_lo_or) and compare-then-branch (SLT + BEQ/BNE, no direct ordering branch) —
use the dedicated tests/fixtures/cmpisa.yaml fixture instead.
"""
import pathlib
import pytest

from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.llvm import generate_llvm
from isa_archive.models import Metadata, Register
from isa_archive.models.operand import Operand, OperandSpec, OperandField

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
PICO32 = EXAMPLES / "tutorial/pico32-part4/isa.yaml"
PICO32F = EXAMPLES / "tutorial/pico32-part4/fp/isa.yaml"
CMPISA = FIXTURES / "cmpisa.yaml"


def _gen(isa_yaml: pathlib.Path, out: pathlib.Path, strict: bool = True) -> pathlib.Path:
    reg = Registry()
    top = load_isa(str(isa_yaml), reg)
    # An `extends:` child also loads its base; emit only the requested ISA
    # (the CLI does the same) so a single Target dir is produced.
    reg.isas = {top.name: top}
    generate_llvm(reg, str(out), strict=strict)
    targets = list((out / "llvm" / "lib" / "Target").iterdir())
    assert len(targets) == 1
    return targets[0]


def test_pico32_compiler_complete_hi_lo_add(tmp_path):
    tgt = _gen(PICO32, tmp_path)
    cov = (tgt / "COMPILER_COVERAGE.md").read_text()
    assert "STATUS: COMPILER-COMPLETE" in cov
    assert "`hi_lo_add`" in cov
    isel = (tgt / f"{tgt.name}ISelDAGToDAG.cpp").read_text()
    assert "hi_lo_add strategy" in isel
    # pico32 has direct ordering branches → no compare-then-branch reduction.
    lowering = (tgt / f"{tgt.name}ISelLowering.cpp").read_text()
    assert "setCondCodeAction" not in lowering


def test_pico32_control_flow_lowering(tmp_path):
    """Call/return/branch use proper pseudos + spill/reload + frame-index fix.

    These are what make the backend actually build & run (validated end-to-end:
    fib + hello on the generated qemu-system-pico32). Lock them in.
    """
    tgt = _gen(PICO32, tmp_path)
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    # Pseudos with link register fixed (r1=ra for calls, r0=zero for branch/return).
    assert "PseudoCALL" in td and "PseudoInstExpansion<(JAL r1," in td
    assert "PseudoBR" in td and "PseudoInstExpansion<(JAL r0," in td
    assert "PseudoRET" in td and "PseudoInstExpansion<(JALR r0, r1, 0)>" in td
    # Spill/reload for registers live across calls.
    instrinfo = (tgt / f"{tgt.name}InstrInfo.cpp").read_text()
    assert "storeRegToStackSlot" in instrinfo and "loadRegFromStackSlot" in instrinfo
    # Frame-index elimination sets the base to SP (r2), not an immediate.
    reginfo = (tgt / f"{tgt.name}RegisterInfo.cpp").read_text()
    assert "ChangeToRegister(PICO32::r2" in reginfo
    # The destination register leads the asm string.
    assert '"add\\t$rd, $rs1, $rs2"' in td
    # MCCodeEmitter no longer references an undefined JAL_CALL.
    emitter = (tgt / "MCTargetDesc" / f"{tgt.name}MCCodeEmitter.cpp").read_text()
    assert "_CALL" not in emitter


def test_cmpisa_compiler_complete_hi_lo_or_and_compare_branch(tmp_path):
    """The OR-based constant strategy + compare-then-branch + select inserter.

    pico32 uses ADDI for the low half (hi_lo_add) and direct ordering branches,
    so this fixture supplies the ORI/SLT shape that pico32 can't.
    """
    tgt = _gen(CMPISA, tmp_path)
    cov = (tgt / "COMPILER_COVERAGE.md").read_text()
    assert "STATUS: COMPILER-COMPLETE" in cov
    assert "`hi_lo_or`" in cov
    # cmp.* roles inferred from SLT/SLTU
    assert "lt ✓" in cov and "ltu ✓" in cov
    lowering = (tgt / f"{tgt.name}ISelLowering.cpp").read_text()
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    # No direct ordering branches → compare-then-branch path is active.
    assert "setCondCodeAction" in lowering
    assert "brcond GPR:$cond" in td
    assert "CMPISAISD::SELECTCC" in lowering  # custom select inserter
    # Has SLT → comparisons use it directly, no PseudoSetCC branch diamond.
    assert "def PseudoSetCC" not in td


def test_select_pseudo_emitted_when_branch_and_zero_exist(tmp_path):
    # pico32 has a conditional branch + zero register → Select pseudo.
    tgt = _gen(PICO32, tmp_path)
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    assert "Select_GPR" in td
    assert "usesCustomInserter = 1" in td


def test_pico32f_float_register_class_and_hard_float(tmp_path):
    """The fp/ extension composes a second register class (f32) + hard-float ABI."""
    tgt = _gen(PICO32F, tmp_path)
    cov = (tgt / "COMPILER_COVERAGE.md").read_text()
    assert "STATUS: COMPILER-COMPLETE" in cov
    reginfo = (tgt / f"{tgt.name}RegisterInfo.td").read_text()
    lowering = (tgt / f"{tgt.name}ISelLowering.cpp").read_text()
    cc = (tgt / f"{tgt.name}CallingConv.td").read_text()
    # multiple register classes: integer GPR + float FPR
    assert 'def GPR : RegisterClass<"PICO32F", [i32]' in reginfo
    assert 'def FPR : RegisterClass<"PICO32F", [f32]' in reginfo
    assert "addRegisterClass(MVT::f32" in lowering
    # hard float: float arithmetic is legal, floats pass in fp registers
    assert "setOperationAction(ISD::FADD" in lowering
    assert "CCIfType<[f32]" in cc


def test_wide_instruction_width_rejected(tmp_path):
    reg = Registry()
    load_isa(str(PICO32), reg)
    isa_reg = next(iter(reg.isas.values()))
    # Bump every schema to an over-limit uniform width.
    for s in isa_reg.schemas.values():
        s.spec.length = 520
    with pytest.raises(ValueError, match="512"):
        generate_llvm(reg, str(tmp_path))


def test_mixed_instruction_widths_rejected(tmp_path):
    reg = Registry()
    load_isa(str(PICO32), reg)
    isa_reg = next(iter(reg.isas.values()))
    # Make widths non-uniform.
    schemas = list(isa_reg.schemas.values())
    schemas[0].spec.length = 64
    with pytest.raises(ValueError, match="uniform"):
        generate_llvm(reg, str(tmp_path))


def test_register_type_struct_resolves_to_opaque_int(tmp_path):
    # A register file whose unified `type:` names an Operand struct (a 32-bit
    # packed pair) becomes an opaque i32 register class.
    reg = Registry()
    load_isa(str(PICO32), reg)
    isa_reg = next(iter(reg.isas.values()))
    isa_reg.operands["Vec2"] = Operand(
        metadata=Metadata(name="Vec2"),
        spec=OperandSpec(width=32, fields=[
            OperandField(name="lo", start=0, width=16),
            OperandField(name="hi", start=16, width=16),
        ]),
    )
    isa_reg.registers.append(
        Register(name="vpr", width=32, count=8, canonical_prefix="v", type="Vec2")
    )
    generate_llvm(reg, str(tmp_path))
    td = (tmp_path / "llvm/lib/Target/PICO32/PICO32RegisterInfo.td").read_text()
    assert 'def VPR : RegisterClass<"PICO32", [i32], 32' in td


def test_register_type_unknown_rejected(tmp_path):
    reg = Registry()
    load_isa(str(PICO32), reg)
    isa_reg = next(iter(reg.isas.values()))
    isa_reg.registers.append(
        Register(name="zpr", width=32, count=4, canonical_prefix="z", type="NotAType")
    )
    with pytest.raises(ValueError, match="neither a scalar"):
        generate_llvm(reg, str(tmp_path))


def test_strict_raises_on_missing_required_role(tmp_path):
    # Load pico32, then delete its BNE so a required role (branch.ne) is unfilled.
    reg = Registry()
    load_isa(str(PICO32), reg)
    isa_reg = next(iter(reg.isas.values()))
    bne_key = next(k for k in isa_reg.instructions if k.lower() == "bne")
    del isa_reg.instructions[bne_key]
    with pytest.raises(ValueError, match="branch.ne"):
        generate_llvm(reg, str(tmp_path), strict=True)
