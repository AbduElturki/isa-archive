import pytest
from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.backends import VerilogBackend, QemuCBackend, RustBackend


def test_verilog_translation():
    ir = BehaviorIR("rd = rs1 + rs2", register_map={"rd": "gpr", "rs1": "gpr", "rs2": "gpr"}, var_widths={"rd": 32, "rs1": 32, "rs2": 32})
    assert VerilogBackend(ir).translate() == "rd_val = (rs1_val + rs2_val);"

    ir = BehaviorIR("rd = (rs1 - rs2) << 2", register_map={"rd": "gpr", "rs1": "gpr", "rs2": "gpr"}, var_widths={"rd": 32, "rs1": 32, "rs2": 32})
    assert VerilogBackend(ir).translate() == "rd_val = ((rs1_val - rs2_val) << 2);"


def test_used_vars_detection():
    ir = BehaviorIR("rd = rs1 + imm", var_widths={"rd": 32, "rs1": 32, "imm": 12})
    assert "rd" in ir.used_vars
    assert "rs1" in ir.used_vars
    assert "imm" in ir.used_vars
    assert "rs2" not in ir.used_vars


def test_complex_expression():
    behavior = "rd = (rs1 * rs2) + (imm >> 1)"
    ir = BehaviorIR(behavior, register_map={"rd": "gpr", "rs1": "gpr", "rs2": "gpr"}, var_widths={"rd": 32, "rs1": 32, "rs2": 32, "imm": 12})

    assert VerilogBackend(ir).translate() == "rd_val = ((rs1_val * rs2_val) + (imm >> 1));"
    assert QemuCBackend(ir).translate() == "env->gpr[rd] = ((rs1_val * rs2_val) + (imm >> 1));"


def test_unsupported_syntax():
    with pytest.raises(Exception):
        QemuCBackend(BehaviorIR("yield rs1")).translate()


# --- Rust translation tests ---

def test_rust_basic_arithmetic():
    ir = BehaviorIR(
        "rd = rs1 + rs2",
        register_map={"rd": "gpr", "rs1": "gpr", "rs2": "gpr"},
        var_widths={"rd": 32, "rs1": 32, "rs2": 32}
    )
    result = RustBackend(ir)._translate(ir.tree.body[0])
    assert "rs1" in result and "rs2" in result and "+" in result


def test_rust_for_loop():
    ir = BehaviorIR(
        "for i in range(n): rd = rs1",
        register_map={"rd": "gpr", "rs1": "gpr"},
        var_widths={"rd": 32, "rs1": 32, "n": 5}
    )
    result = RustBackend(ir)._translate(ir.tree.body[0])
    assert "for i in 0..n" in result


def test_rust_struct_constructor():
    from isa_archive.models import Operand, Metadata
    from isa_archive.models.operand import OperandSpec, OperandField
    operand = Operand(
        metadata=Metadata(name="Point"),
        spec=OperandSpec(width=64, fields=[
            OperandField(name="x", start=0, width=32),
            OperandField(name="y", start=32, width=32),
        ])
    )
    ir = BehaviorIR(
        "p = Point(rs1, rs2)",
        register_map={"rs1": "gpr", "rs2": "gpr"},
        var_widths={"rs1": 32, "rs2": 32},
        operands={"Point": operand}
    )
    result = RustBackend(ir)._translate(ir.tree.body[0])
    assert "Point::new" in result
