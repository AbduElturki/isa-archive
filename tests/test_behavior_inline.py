"""kind: BehaviorFunc - the AST inlining pass and its validation."""
import pathlib
import pytest

from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.backends import QemuCBackend
from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.qemu import generate_qemu_isa
from isa_archive.generators.cpp_isa import generate_cpp_isa
from isa_archive.models import (Metadata, BehaviorFunc, BehaviorFuncSpec, ArgDef,
                                Operand, OperandSpec, OperandField)

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "behavior_func.yaml"

RMAP = {"rd": "r", "rs1": "r", "rs2": "r"}
VW = {"rd": 32, "rs1": 32, "rs2": 32, "imm": 12}


def _bf(name, args, behavior, returns=None):
    """args: list of (name, type[, editable])."""
    argdefs = [ArgDef(name=a[0], type=a[1], editable=(len(a) > 2 and a[2])) for a in args]
    return BehaviorFunc(metadata=Metadata(name=name),
                        spec=BehaviorFuncSpec(args=argdefs, returns=returns, behavior=behavior))


def _funcs(*bfs):
    return {b.metadata.name: b for b in bfs}


def _ir(behavior, funcs, operands=None, rmap=None, vw=None):
    return BehaviorIR(behavior, register_map=rmap or RMAP, var_widths=vw or VW,
                      operands=operands or {}, behavior_funcs=funcs)


CLAMP = _bf("clamp", [("x", "i32"), ("lo", "i32"), ("hi", "i32")],
            "if x < lo: x = lo\nif x > hi: x = hi\nreturn x", returns="i32")
ADDF = _bf("addf", [("a", "i32"), ("b", "i32")], "return a + b", returns="i32")
SETZ = _bf("setz", [("t", "i32", True)], "t = 0")


def test_value_return_inlines_into_rhs():
    c = QemuCBackend(_ir("rd = clamp(rs1, 0, 15)", _funcs(CLAMP))).translate()
    # by-value param → local copy; the tail `return x` becomes the caller's assignment.
    assert "_clamp_x_1 = rs1_val;" in c
    assert "(_clamp_x_1 < 0)" in c and "(_clamp_x_1 > 15)" in c
    assert "env->r[rd] = _clamp_x_1;" in c


def test_readonly_only_read_arg_substitutes_directly_keeping_fast_path():
    # addf's params are read-only and never reassigned → direct substitution, so
    # `rd = addf(rs1, rs2)` collapses to a single BinOp assignment (TCG fast path).
    c = QemuCBackend(_ir("rd = addf(rs1, rs2)", _funcs(ADDF))).translate()
    assert c == "env->r[rd] = (rs1_val + rs2_val);"


def test_void_func_with_editable_arg_writes_back():
    c = QemuCBackend(_ir("setz(rd)", _funcs(SETZ))).translate()
    assert c == "env->r[rd] = 0;"


def test_unused_value_return_warns_but_still_generates():
    ir = _ir("addf(rs1, rs2)", _funcs(ADDF))   # value ignored
    assert any("addf" in w and "not used" in w for w in ir.inline_warnings)
    QemuCBackend(ir).translate()  # still lowers


def test_editable_arg_must_be_lvalue():
    with pytest.raises(ValueError, match="editable"):
        _ir("setz(rs1 + 1)", _funcs(SETZ))


def test_void_func_in_value_position_rejected():
    with pytest.raises(ValueError, match="returns nothing"):
        _ir("rd = setz(rs1)", _funcs(SETZ))


def test_nested_call_rejected():
    with pytest.raises(ValueError, match="whole right-hand side|nested"):
        _ir("rd = addf(rs1, rs2) + 1", _funcs(ADDF))


def test_recursion_rejected():
    rec = _bf("loopy", [("a", "i32")], "t = loopy(a)\nreturn t", returns="i32")
    with pytest.raises(ValueError, match="recursive"):
        _ir("rd = loopy(rs1)", _funcs(rec))


def test_local_gensym_avoids_caller_collision():
    # both the caller and the func use a local named `t`; they must not clash.
    f = _bf("bump", [("a", "i32")], "t = a + 2\nreturn t", returns="i32")
    c = QemuCBackend(_ir("t = rs1 + 1\nrd = bump(t)", _funcs(f))).translate()
    assert "t = (rs1_val + 1);" in c        # caller's own temp, untouched
    assert "_bump_t_1 = (t + 2);" in c       # func local, renamed
    assert "env->r[rd] = _bump_t_1;" in c


def test_operand_typed_param_field_access_resolves():
    pair = Operand(metadata=Metadata(name="Pair"),
                   spec=OperandSpec(width=32, fields=[
                       OperandField(name="lo", start=0, width=16),
                       OperandField(name="hi", start=16, width=16)]))
    # return a 32-bit concat of the two fields (accessing p.hi / p.lo through the
    # operand-typed param proves field-width resolution works after binding).
    phi = _bf("swap", [("p", "Pair")], "return {p.hi, p.lo}", returns="i32")
    ir = _ir("tmp = Pair(rs1, rs2)\nrd = swap(tmp)", _funcs(phi),
             operands={"Pair": pair})
    c = QemuCBackend(ir).translate()
    # `p.hi` / `p.lo` bound to the caller's operand temp `tmp`.
    assert "tmp.hi" in c and "tmp.lo" in c and "env->r[rd] =" in c


def test_end_to_end_generation(tmp_path):
    # The bfunc fixture ISA calls clamp + a void editable func; generate real targets.
    reg = Registry()
    load_isa(str(FIXTURE), reg)
    generate_qemu_isa(reg, str(tmp_path / "q"))
    helpers = (tmp_path / "q" / "bfunc_helpers.c").read_text()
    assert "_clamp_x_1" in helpers            # clamp inlined into CLAMP15
    assert "env->r[rd] = 0;" in helpers        # editable zero_reg wrote back
    generate_cpp_isa(reg, str(tmp_path / "cpp"))
    assert (tmp_path / "cpp" / "Bfunc" / "Bfunc.h").exists()
