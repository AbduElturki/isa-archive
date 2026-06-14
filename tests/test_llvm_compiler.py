"""Integration tests for the generated LLVM backend (generation-level, no clang build).

Covers the compiler-role contract, coverage report, constant-materialization
strategies, and the Phase-1 compare-then-branch / select lowering.
"""
import pathlib
import pytest

from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.llvm import generate_llvm
from isa_archive.models.isa import Register

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


def _gen(isa_yaml: pathlib.Path, out: pathlib.Path, strict: bool = True) -> pathlib.Path:
    reg = Registry()
    load_isa(str(isa_yaml), reg)
    generate_llvm(reg, str(out), strict=strict)
    targets = list((out / "llvm" / "lib" / "Target").iterdir())
    assert len(targets) == 1
    return targets[0]


def test_rv32_compiler_complete_hi_lo_add(tmp_path):
    tgt = _gen(EXAMPLES / "rv32/base/isa.yaml", tmp_path)
    cov = (tgt / "COMPILER_COVERAGE.md").read_text()
    assert "STATUS: COMPILER-COMPLETE" in cov
    assert "`hi_lo_add`" in cov
    isel = (tgt / f"{tgt.name}ISelDAGToDAG.cpp").read_text()
    assert "hi_lo_add strategy" in isel
    # rv32 has direct ordering branches → no compare-then-branch reduction.
    lowering = (tgt / f"{tgt.name}ISelLowering.cpp").read_text()
    assert "setCondCodeAction" not in lowering


def test_rv32_control_flow_lowering(tmp_path):
    """Call/return/branch use proper pseudos + spill/reload + frame-index fix.

    These are what make the backend actually build & run (validated end-to-end:
    fib + hello on qemu-system-rv32i). Lock them in so they don't regress.
    """
    tgt = _gen(EXAMPLES / "rv32/base/isa.yaml", tmp_path)
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    # Pseudos with link register fixed (ra for calls, x0 for branch/return).
    assert "PseudoCALL" in td and "PseudoInstExpansion<(JAL x1," in td
    assert "PseudoBR" in td and "PseudoInstExpansion<(JAL x0," in td
    assert "PseudoRET" in td and "PseudoInstExpansion<(JALR x0, x1, 0)>" in td
    # Spill/reload for registers live across calls.
    instrinfo = (tgt / f"{tgt.name}InstrInfo.cpp").read_text()
    assert "storeRegToStackSlot" in instrinfo and "loadRegFromStackSlot" in instrinfo
    # Frame-index elimination sets the base to SP (not an immediate).
    reginfo = (tgt / f"{tgt.name}RegisterInfo.cpp").read_text()
    assert "ChangeToRegister(RV32I::x2" in reginfo
    # The destination register leads the asm string.
    assert '"add\\t$rd, $rs1, $rs2"' in td
    # MCCodeEmitter no longer references an undefined JAL_CALL.
    emitter = (tgt / "MCTargetDesc" / f"{tgt.name}MCCodeEmitter.cpp").read_text()
    assert "_CALL" not in emitter


def test_minimips_compiler_complete_hi_lo_or_and_compare_branch(tmp_path):
    tgt = _gen(EXAMPLES / "minimips/isa.yaml", tmp_path)
    cov = (tgt / "COMPILER_COVERAGE.md").read_text()
    assert "STATUS: COMPILER-COMPLETE" in cov
    assert "`hi_lo_or`" in cov
    # cmp.* roles inferred from SLT/SLTU/SLTI/SLTIU
    assert "lt ✓" in cov and "ltu ✓" in cov
    lowering = (tgt / f"{tgt.name}ISelLowering.cpp").read_text()
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    # No direct ordering branches → compare-then-branch path is active.
    assert "setCondCodeAction" in lowering
    assert "brcond GPR:$cond" in td
    assert "MINIMIPSISD::SELECTCC" in lowering  # custom select inserter


def test_select_pseudo_emitted_when_branch_and_zero_exist(tmp_path):
    # Both rv32 and minimips have a conditional branch + zero register → Select pseudo.
    tgt = _gen(EXAMPLES / "rv32/base/isa.yaml", tmp_path)
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    assert "Select_GPR" in td
    assert "usesCustomInserter = 1" in td


def test_showcase_composes_all_features(tmp_path):
    tgt = _gen(EXAMPLES / "showcase/isa.yaml", tmp_path)
    cov = (tgt / "COMPILER_COVERAGE.md").read_text()
    assert "STATUS: COMPILER-COMPLETE" in cov
    formats = (tgt / f"{tgt.name}InstrFormats.td").read_text()
    reginfo = (tgt / f"{tgt.name}RegisterInfo.td").read_text()
    lowering = (tgt / f"{tgt.name}ISelLowering.cpp").read_text()
    td = (tgt / f"{tgt.name}InstrInfo.td").read_text()
    cc = (tgt / f"{tgt.name}CallingConv.td").read_text()
    # #8 wide instructions
    assert "field bits<64> Inst" in formats and "let Size       = 8" in formats
    # #6 multiple register classes
    assert 'def GPR : RegisterClass<"SHOWCASE", [i32]' in reginfo
    assert 'def FPR : RegisterClass<"SHOWCASE", [f32]' in reginfo
    assert "addRegisterClass(MVT::f32" in lowering
    # #7 hard-float
    assert "setOperationAction(ISD::FADD" in lowering
    assert "CCIfType<[f32]" in cc
    # #1 compare-then-branch  &  #2 select
    assert "setCondCodeAction" in lowering
    assert "brcond GPR:$cond" in td
    assert "Select_GPR" in td


def test_wide_instruction_width_rejected(tmp_path):
    reg = Registry()
    load_isa(str(EXAMPLES / "showcase/isa.yaml"), reg)
    isa_reg = next(iter(reg.isas.values()))
    # Bump every schema to an over-limit uniform width.
    for s in isa_reg.schemas.values():
        s.spec.length = 520
    with pytest.raises(ValueError, match="512"):
        generate_llvm(reg, str(tmp_path))


def test_mixed_instruction_widths_rejected(tmp_path):
    reg = Registry()
    load_isa(str(EXAMPLES / "showcase/isa.yaml"), reg)
    isa_reg = next(iter(reg.isas.values()))
    # Make widths non-uniform.
    schemas = list(isa_reg.schemas.values())
    schemas[0].spec.length = 32
    with pytest.raises(ValueError, match="uniform"):
        generate_llvm(reg, str(tmp_path))


def test_register_type_struct_resolves_to_opaque_int(tmp_path):
    # A register file whose unified `type:` names an Operand struct (Vec2, a 32-bit
    # packed pair) becomes an opaque i32 register class.
    reg = Registry()
    load_isa(str(EXAMPLES / "rv32/base/isa.yaml"), reg)
    isa_reg = next(iter(reg.isas.values()))
    assert "Vec2" in isa_reg.operands
    isa_reg.registers.append(
        Register(name="vpr", width=32, count=8, canonical_prefix="v", type="Vec2")
    )
    generate_llvm(reg, str(tmp_path))
    td = (tmp_path / "llvm/lib/Target/RV32I/RV32IRegisterInfo.td").read_text()
    assert 'def VPR : RegisterClass<"RV32I", [i32], 32' in td


def test_register_type_unknown_rejected(tmp_path):
    reg = Registry()
    load_isa(str(EXAMPLES / "rv32/base/isa.yaml"), reg)
    isa_reg = next(iter(reg.isas.values()))
    isa_reg.registers.append(
        Register(name="zpr", width=32, count=4, canonical_prefix="z", type="NotAType")
    )
    with pytest.raises(ValueError, match="neither a scalar"):
        generate_llvm(reg, str(tmp_path))


def test_strict_raises_on_missing_required_role(tmp_path, monkeypatch):
    # Load minimips, then delete its BNE so a required role (branch.ne) is unfilled.
    reg = Registry()
    load_isa(str(EXAMPLES / "minimips/isa.yaml"), reg)
    isa_reg = next(iter(reg.isas.values()))
    bne_key = next(k for k in isa_reg.instructions if k.lower() == "bne")
    del isa_reg.instructions[bne_key]
    with pytest.raises(ValueError, match="branch.ne"):
        generate_llvm(reg, str(tmp_path), strict=True)
