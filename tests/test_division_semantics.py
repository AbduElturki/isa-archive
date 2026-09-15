"""`/` and `%` follow operand signedness in every backend; Python's `//` is
rejected at validation (it used to lower to the literal token `None` in C)."""
import pytest

from isa_archive.compiler.behavior import BehaviorIR, validate_ir
from isa_archive.compiler.backends import QemuCBackend, LLVMDagBackend

REGS = {"rd": "gpr", "rs1": "gpr", "rs2": "gpr"}
WIDTHS = {"rd": 32, "rs1": 32, "rs2": 32, "pc": 32}


def _ir(b):
    return BehaviorIR(b, register_map=REGS, var_widths=WIDTHS)


def test_floor_division_is_rejected():
    with pytest.raises(ValueError, match="'//' is not a DSL operator"):
        validate_ir(_ir("rd = rs1 // rs2"))


@pytest.mark.parametrize("op", ["**", "@"])
def test_other_python_operators_are_rejected(op):
    with pytest.raises(ValueError, match="not a DSL operator"):
        validate_ir(_ir(f"rd = rs1 {op} rs2"))


def test_qemu_c_division_follows_signedness():
    assert QemuCBackend(_ir("rd = rs1 / rs2")).translate() == \
        "env->gpr[rd] = (rs1_val / rs2_val);"
    assert QemuCBackend(_ir("rd = signed(rs1) / signed(rs2)")).translate() == \
        "env->gpr[rd] = ((int32_t)(rs1_val) / (int32_t)(rs2_val));"
    assert "None" not in QemuCBackend(_ir("rd = rs1 % rs2")).translate()


@pytest.mark.parametrize("behavior,op", [
    ("rd = rs1 / rs2", "udiv"),
    ("rd = signed(rs1) / signed(rs2)", "sdiv"),
    ("rd = rs1 % rs2", "urem"),
    ("rd = signed(rs1) % signed(rs2)", "srem"),
])
def test_llvm_dag_division_ops(behavior, op):
    pat = LLVMDagBackend(_ir(behavior), xlen=32).translate()
    assert pat.category == "alu_rr" and pat.op == op
    assert f"({op} GPR:$rs1, GPR:$rs2)" in pat.dag
