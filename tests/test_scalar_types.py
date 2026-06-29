"""Tests for the single scalar-type source of truth (models/scalar_types.py).

The resolver is what makes "float" data-driven instead of special-cased: every
generator decision about int vs float, the LLVM MVT, and the QEMU host C type
flows through `resolve`/`of_register`.
"""
import pathlib

import pytest

from isa_archive.models.scalar_types import (
    resolve, of_register, ScalarType, ArithClass,
    register, clear_registered, register_from_manifest,
)
from isa_archive.models import Register, ScalarTypeDef, Metadata

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_registered_types():
    """Registered scalar types are process-global; keep tests independent."""
    clear_registered()
    yield
    clear_registered()


def test_resolve_known_scalars():
    i32 = resolve("i32")
    assert (i32.width, i32.arith_class, i32.llvm_mvt, i32.c_type) == (
        32, ArithClass.INT, "i32", "uint32_t")
    f32 = resolve("f32")
    assert (f32.width, f32.arith_class, f32.llvm_mvt, f32.c_type) == (
        32, ArithClass.IEEE_FLOAT, "f32", "float")
    f64 = resolve("f64")
    assert (f64.arith_class, f64.c_type) == (ArithClass.IEEE_FLOAT, "double")
    bf16 = resolve("bf16")
    assert (bf16.width, bf16.arith_class, bf16.llvm_mvt) == (
        16, ArithClass.IEEE_FLOAT, "bf16")


def test_resolve_unknown_returns_none():
    assert resolve("notatype") is None
    assert resolve("v2f32") is None      # vector - not a native scalar
    assert resolve("Vec2") is None       # Operand struct name


def test_sixteen_bit_floats_have_no_native_host_ctype():
    # Built-in scope: f16/bf16/f128 have an LLVM MVT but no portable host C type.
    assert resolve("f16").c_type is None
    assert resolve("bf16").c_type is None
    assert resolve("f128").c_type is None
    # f128 is still IEEE_FLOAT on the LLVM side.
    assert resolve("f128").arith_class == ArithClass.IEEE_FLOAT


def _reg(**kw):
    base = dict(name="r", width=32, count=32)
    base.update(kw)
    return Register(**base)


def test_of_register_modern_scalar():
    assert of_register(_reg(type="f32")).arith_class == ArithClass.IEEE_FLOAT
    assert of_register(_reg(type="i32")).arith_class == ArithClass.INT


def test_of_register_operand_struct_is_opaque_int():
    st = of_register(_reg(type="Vec2", width=32))
    assert st.arith_class == ArithClass.INT
    assert st.llvm_mvt == "i32"


def test_of_register_legacy_float_flag():
    st = of_register(_reg(float=True, width=64))
    assert st.arith_class == ArithClass.IEEE_FLOAT
    assert st.llvm_mvt == "f64" and st.c_type == "double"


def test_of_register_legacy_value_types():
    st = of_register(_reg(value_types=["f32"]))
    assert st.arith_class == ArithClass.IEEE_FLOAT


def test_of_register_bare_integer_default():
    st = of_register(_reg(width=32))
    assert st.arith_class == ArithClass.INT and st.llvm_mvt == "i32"


def test_register_is_float_parity_across_all_forms():
    assert _reg(type="f32").is_float is True
    assert _reg(type="i32").is_float is False
    assert _reg(type="Vec2").is_float is False
    assert _reg(float=True).is_float is True
    assert _reg(value_types=["f32"]).is_float is True
    assert _reg(value_types=["i32"]).is_float is False
    assert _reg().is_float is False


# ── kind: ScalarType - the load-time registry ────────────────────────────────

def _stdef(name, **spec):
    return ScalarTypeDef(metadata=Metadata(name=name), spec={**spec})


def test_register_then_resolve_and_clear():
    assert resolve("i4") is None                       # not a builtin
    register(ScalarType("i4", 4, ArithClass.INT, "i4", None))
    assert resolve("i4").width == 4
    clear_registered()
    assert resolve("i4") is None                       # registry reset


def test_register_from_manifest_float_defaults_and_mapping():
    st = register_from_manifest(_stdef("fp8_e4m3", width=8, arith_class="ieee_float",
                                       llvm_mvt="f8E4M3"))
    assert (st.arith_class, st.llvm_mvt, st.c_type) == (ArithClass.IEEE_FLOAT, "f8E4M3", None)
    assert resolve("fp8_e4m3") is st


def test_register_from_manifest_omitted_mvt_is_none():
    # llvm_mvt is optional: omit it and the type has no LLVM value type.
    st = register_from_manifest(_stdef("i4", width=4))   # arith_class defaults to int
    assert st.arith_class == ArithClass.INT and st.llvm_mvt is None


def test_register_from_manifest_per_backend_types_and_includes():
    st = register_from_manifest(_stdef("fp8", width=8, arith_class="ieee_float",
                                       llvm_mvt="f8E4M3",
                                       c_type="fp8_t", c_include="<fp8.h>"))
    assert (st.c_type, st.c_include) == ("fp8_t", "<fp8.h>")
    # cpp_* fall back to the C names
    assert (st.eff_cpp_type, st.eff_cpp_include) == ("fp8_t", "<fp8.h>")
    st2 = register_from_manifest(_stdef("fp8b", width=8, arith_class="ieee_float",
                                        c_type="cfp8_t", c_include="<cfp8.h>",
                                        cpp_type="ns::fp8", cpp_include="<nsfp8.h>"))
    assert (st2.eff_cpp_type, st2.eff_cpp_include) == ("ns::fp8", "<nsfp8.h>")


def test_format_include_wraps_bare_else_verbatim():
    from isa_archive.models.scalar_types import format_include
    assert format_include("fp8.h") == "<fp8.h>"
    assert format_include("<fp8.h>") == "<fp8.h>"
    assert format_include('"fp8.h"') == '"fp8.h"'


def test_register_with_no_mvt_type_has_no_llvm_value_type():
    # llvm_mvt omitted → the element has no LLVM value type, so a file using it can't
    # be a register class (it stays simulator-only; checked by core._is_codegen_class).
    register_from_manifest(_stdef("q8", width=8, arith_class="ieee_float",
                                  c_type="q8_t", c_include="<q.h>"))   # no llvm_mvt
    assert of_register(Register(name="q", width=8, count=4, type="q8")).llvm_mvt is None


def test_of_register_uses_registered_type():
    register_from_manifest(_stdef("fp8_e4m3", width=8, arith_class="ieee_float",
                                  llvm_mvt="f8E4M3"))
    r = Register(name="q", width=8, count=16, type="fp8_e4m3")
    assert of_register(r).arith_class == ArithClass.IEEE_FLOAT
    assert of_register(r).llvm_mvt == "f8E4M3"
    assert r.is_float is True


def test_registered_overrides_builtin():
    register(ScalarType("f32", 32, ArithClass.IEEE_FLOAT, "f32", None))  # shadow built-in
    assert resolve("f32").c_type is None


# ── loader integration ───────────────────────────────────────────────────────

def test_loader_registers_scalartype_from_npu_probe():
    from isa_archive.compiler.loader import load_isa
    reg = load_isa(str(REPO / "examples/npu-probe/isa.yaml"))
    assert "fp8_e4m3" in reg.scalar_types
    qreg = next(r for r in reg.registers if r.name == "qreg")
    assert of_register(qreg).llvm_mvt == "f8E4M3"   # resolved via the registered type


def test_loader_rejects_unknown_register_type():
    from isa_archive.compiler.loader import ISARegistry
    from isa_archive.models import ISA, ISASpec, ISAState

    manifest = ISA(metadata=Metadata(name="t"), spec=ISASpec(
        name="t", version="1.0",
        state=ISAState(registers=[Register(name="q", width=8, count=4, type="fp8_nope")]),
    ))
    with pytest.raises(ValueError, match="unknown type 'fp8_nope'"):
        ISARegistry(manifest).validate()
