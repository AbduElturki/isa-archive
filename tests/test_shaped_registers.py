"""General structured (shaped) registers: element type + shape — the DSL element
indexing, QEMU N-D storage/lowering, loader validation, and LLVM vector classes."""
import ast
import pathlib

import pytest

from isa_archive.models import (ISA, ISASpec, ISAState, Metadata, Register, Schema,
                                Instruction)
from isa_archive.models.schema import SchemaSpec, SchemaField
from isa_archive.models.instruction import InstructionSpec
from isa_archive.models.scalar_types import (resolve, register, clear_registered,
                                             ScalarType, ArithClass)
from isa_archive.compiler.loader import ISARegistry, load_isa
from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.backends import QemuCBackend
from isa_archive.compiler.backends.llvm_dag import LLVMDagBackend
from isa_archive.generators.llvm.regclasses import _class_value_types, _is_vector_class
from isa_archive.generators.qemu.word import _regfile_storage

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _types():
    clear_registered()
    register(ScalarType("fp8_e4m3", 8, ArithClass.IEEE_FLOAT, "f8E4M3", None))
    yield
    clear_registered()


def _vec_shapes():
    return {"vec": (resolve("i32"), [4]), "tile": (resolve("fp8_e4m3"), [8, 8]),
            "fvec": (resolve("f32"), [4])}


def _attrs():
    return {"tile": {"layout": 3, "valid": 1}}


def _ir(behavior, register_map):
    return BehaviorIR(behavior, register_map=register_map,
                      var_widths={k: 128 for k in register_map},
                      regfile_shapes=_vec_shapes(), regfile_attrs=_attrs())


def _c(behavior, register_map):
    ir = _ir(behavior, register_map)
    return QemuCBackend(ir).translate(regfile_shapes=_vec_shapes(), regfile_attrs=_attrs())


# ── model ────────────────────────────────────────────────────────────────────

def test_register_shape_properties():
    r = Register(name="vec", width=128, count=16, type="i32", shape=[4])
    assert r.is_shaped and r.lane_count == 4 and r.element_width == 32
    t = Register(name="tile", width=512, count=8, type="fp8_e4m3", shape=[8, 8])
    assert t.lane_count == 64 and t.element_width == 8
    assert Register(name="g", width=32, count=32).is_shaped is False


# ── behavior DSL element indexing ────────────────────────────────────────────

def test_element_index_width_is_element_width():
    ir = _ir("vd[0] = vs1[0]", {"vd": "vec", "vs1": "vec"})
    assert ir.get_width(ast.parse("vd[0]", mode="eval").body) == 32


def test_partial_index_is_rejected():
    ir = _ir("td[0] = td[0]", {"td": "tile"})   # tile is 2-D; one index = partial
    with pytest.raises(ValueError, match="index all 2"):
        ir.get_width(ast.parse("td[0]", mode="eval").body)


def test_element_write_marks_register_written():
    ir = _ir("vd[0] = vs1[0]", {"vd": "vec", "vs1": "vec"})
    assert "vd" in ir.write_vars


# ── QEMU C lowering ──────────────────────────────────────────────────────────

def test_qemu_lowers_1d_and_2d_element_access():
    assert _c("vd[0] = vs1[0]", {"vd": "vec", "vs1": "vec"}) == "env->vec[vd][0] = env->vec[vs1][0];"
    assert _c("td[1][2] = ts[3][4]", {"td": "tile", "ts": "tile"}) == \
        "env->tile[td][1][2] = env->tile[ts][3][4];"


def test_qemu_elementwise_loop():
    c = _c("for i in range(4):\n    vd[i] = vs1[i] + vs2[i]",
           {"vd": "vec", "vs1": "vec", "vs2": "vec"})
    assert "env->vec[vd][i] = (env->vec[vs1][i] + env->vec[vs2][i]);" in c
    assert "for (uint32_t i = 0; i < 4; i++)" in c


def test_qemu_rejects_bare_shaped_register():
    with pytest.raises(ValueError, match="must be indexed"):
        _c("vd = vs1", {"vd": "vec", "vs1": "vec"})


def test_qemu_float_element_arithmetic_f32():
    c = _c("fd[0] = fs1[0] + fs2[0]", {"fd": "fvec", "fs1": "fvec", "fs2": "fvec"})
    assert c == "env->fvec[fd][0] = f2u32(u2f32(env->fvec[fs1][0]) + u2f32(env->fvec[fs2][0]));"


def test_qemu_fp8_element_arithmetic_needs_softfloat():
    # fp8 element with c_type=None → arithmetic still rejected (loudly).
    with pytest.raises(ValueError, match="softfloat"):
        _c("td[0][0] = ts[0][0] + ts[1][1]", {"td": "tile", "ts": "tile"})


def test_qemu_float_element_arithmetic_enabled_by_ctype():
    # A library float type that provides a c_type ENABLES QEMU arithmetic (via u2f/f2u);
    # the header is what makes it compile.
    register(ScalarType("fp8c", 8, ArithClass.IEEE_FLOAT, "f8E4M3", "fp8_t", c_include="<fp8.h>"))
    shapes = {"t": (resolve("fp8c"), [4])}
    ir = BehaviorIR("td[0] = ts[0] + ts[1]", register_map={"td": "t", "ts": "t"},
                    var_widths={"td": 32, "ts": 32}, regfile_shapes=shapes)
    c = QemuCBackend(ir).translate(regfile_shapes=shapes)
    assert c == "env->t[td][0] = f2u8(u2f8(env->t[ts][0]) + u2f8(env->t[ts][1]));"


def test_qemu_partial_index_subarray_copy():
    c = _c("for i in range(8):\n    td[i] = ts[i]", {"td": "tile", "ts": "tile"})
    assert "env->tile[td][i][_p0] = env->tile[ts][i][_p0];" in c
    assert "for (uint32_t _p0 = 0; _p0 < 8; _p0++)" in c


def test_qemu_partial_copy_requires_matching_subarray():
    with pytest.raises(ValueError, match="sub-array"):
        _c("td[0] = ts[0][0]", {"td": "tile", "ts": "tile"})  # row vs scalar


# ── register attributes (metadata) ───────────────────────────────────────────

def test_attr_write_and_read_lowering():
    assert _c("td.valid = 1", {"td": "tile"}) == "env->tile_valid[td] = (1) & 0x1;"
    assert _c("td.layout = ts.layout", {"td": "tile", "ts": "tile"}) == \
        "env->tile_layout[td] = (env->tile_layout[ts]) & 0x7;"


def test_attr_width_inference_and_flags():
    ir = _ir("td.layout = 0", {"td": "tile"})
    assert ir.get_width(ast.parse("td.layout", mode="eval").body) == 3
    assert ir.uses_sys and "td" in ir.attr_regs        # forces index passing + degrade


def test_unknown_attr_is_recorded():
    ir = _ir("td.bogus = 1", {"td": "tile"})
    assert ("td", "bogus") in ir.unknown_reg_attrs


def test_loader_rejects_unknown_attr():
    fp8 = Register(name="tile", width=512, count=8, type="fp8_e4m3", shape=[8, 8])
    m = ISA(metadata=Metadata(name="t"), spec=ISASpec(name="t", version="1.0",
            state=ISAState(registers=[Register(name="g", width=32, count=8, zero_register=0), fp8])))
    fp8.attributes = []   # no attributes declared
    reg = ISARegistry(m)
    reg.add(Schema(metadata=Metadata(name="T"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0, width=8, role="opcode"),
        SchemaField(name="td", start=8, width=3, role="register", type="tile"),
        SchemaField(name="pad", start=11, width=21, role="reserved")])))
    reg.add(Instruction(metadata=Metadata(name="I"),
            spec=InstructionSpec(**{"schema": "T", "opcode": 0x73, "behavior": "td.nope = 1"})))
    with pytest.raises(ValueError, match="no attribute 'nope'"):
        reg.validate()


# ── QEMU storage ─────────────────────────────────────────────────────────────

def _reg_isa(registers):
    m = ISA(metadata=Metadata(name="t"), spec=ISASpec(name="t", version="1.0",
            state=ISAState(registers=registers)))
    return ISARegistry(m)


def test_storage_is_nd_array():
    isa = _reg_isa([Register(name="gpr", width=32, count=32, zero_register=0),
                    Register(name="vec", width=128, count=16, type="i32", shape=[4]),
                    Register(name="tile", width=512, count=8, type="fp8_e4m3", shape=[8, 8])])
    st = _regfile_storage(isa)
    assert st["vec"]["shaped"] and st["vec"]["shape"] == [4] and st["vec"]["elem_ctype"] == "uint32_t"
    assert st["tile"]["shape"] == [8, 8] and st["tile"]["elem_ctype"] == "uint8_t"


# ── loader validation ────────────────────────────────────────────────────────

def test_loader_rejects_width_mismatch():
    isa = _reg_isa([Register(name="vec", width=100, count=4, type="i32", shape=[4])])
    with pytest.raises(ValueError, match="width 100 ≠"):
        isa.validate()


def test_loader_rejects_operand_element_with_shape():
    # an unknown/struct element with a shape is rejected (shaped = scalar elements)
    isa = _reg_isa([Register(name="vec", width=128, count=4, type="Pair", shape=[4])])
    with pytest.raises(ValueError):
        isa.validate()


# ── LLVM vector register classes ─────────────────────────────────────────────

def test_vector_class_value_types():
    vec = Register(name="vec", width=128, count=16, type="i32", shape=[4])
    assert _class_value_types(vec) == ["v4i32"] and _is_vector_class(vec)
    tile = Register(name="tile", width=512, count=8, type="fp8_e4m3", shape=[8, 8])
    assert _is_vector_class(tile) is False           # 2-D → not a vector class


def _dag(behavior, register_map, var_widths):
    ir = BehaviorIR(behavior, register_map=register_map, var_widths=var_widths,
                    regfile_shapes={"vec": (resolve("i32"), [4])})
    rci = {"vec": {"class": "VEC", "is_float": False},
           "gpr": {"class": "GPR", "is_float": False}}
    return LLVMDagBackend(ir, reg_class_info=rci).translate()


def test_canonical_elementwise_becomes_vector_dag():
    dag = _dag("for i in range(4):\n    vd[i] = vs1[i] + vs2[i]",
               {"vd": "vec", "vs1": "vec", "vs2": "vec"},
               {"vd": 128, "vs1": 128, "vs2": 128})
    assert dag.category == "vector"
    assert dag.dag == "(set VEC:$vd, (add VEC:$vs1, VEC:$vs2))"


def test_contiguous_vector_load_and_store_dag():
    ld = _dag("for i in range(4):\n    vr[i] = mem32[rs1 + i * 4]",
              {"vr": "vec", "rs1": "gpr"}, {"vr": 128, "rs1": 32})
    assert ld.category == "vector_load" and ld.dag == "(set VEC:$vr, (load GPR:$rs1))"
    st = _dag("for i in range(4):\n    mem32[rs1 + i * 4] = vr[i]",
              {"vr": "vec", "rs1": "gpr"}, {"vr": 128, "rs1": 32})
    assert st.category == "vector_store" and st.dag == "(store VEC:$vr, GPR:$rs1)"


def test_noncontiguous_vector_mem_is_not_a_vector_pattern():
    # wrong stride (i*8 for i32) → not a unit-stride vector load → custom
    ld = _dag("for i in range(4):\n    vr[i] = mem32[rs1 + i * 8]",
              {"vr": "vec", "rs1": "gpr"}, {"vr": 128, "rs1": 32})
    assert ld.category != "vector_load"


# ── integration: the npu-probe example ───────────────────────────────────────

def test_npu_probe_shaped_generates(tmp_path):
    from isa_archive.compiler.loader import Registry
    from isa_archive.generators.llvm import generate_llvm
    from isa_archive.generators.qemu import generate_qemu_isa
    reg = load_isa(str(REPO / "examples/npu-probe/isa.yaml"))
    registry = Registry(); registry.isas[reg.name] = reg
    generate_qemu_isa(registry, str(tmp_path / "q"))
    arch = next((tmp_path / "q").glob("*_arch.h")).read_text()
    assert "uint32_t vec[16][4];" in arch and "uint8_t tile[8][8][8];" in arch
    generate_llvm(registry, str(tmp_path / "l"))
    td = (tmp_path / "l" / "llvm/lib/Target/NPU_PROBE/NPU_PROBERegisterInfo.td").read_text()
    instr = (tmp_path / "l" / "llvm/lib/Target/NPU_PROBE/NPU_PROBEInstrInfo.td").read_text()
    assert "def VEC : RegisterClass" in td and "v4i32" in td
    assert "def TILE : RegisterClass" not in td          # 2-D tile → simulator-only
    assert "(set VEC:$vd, (add VEC:$vs1, VEC:$vs2))" in instr
    assert "def TMOV" not in instr                        # uses tile → omitted
