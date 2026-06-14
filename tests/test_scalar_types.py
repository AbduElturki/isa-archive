"""Tests for the single scalar-type source of truth (models/scalar_types.py).

The resolver is what makes "float" data-driven instead of special-cased: every
generator decision about int vs float, the LLVM MVT, and the QEMU host C type
flows through `resolve`/`of_register`.
"""
from isa_archive.models.scalar_types import (
    resolve, of_register, ScalarType, ArithClass,
)
from isa_archive.models import Register


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
    assert resolve("v2f32") is None      # vector — not a native scalar
    assert resolve("Vec2") is None       # Operand struct name


def test_sixteen_bit_floats_have_no_native_host_ctype():
    # Honest ceiling: f16/bf16/f128 have an LLVM MVT but no portable host C type.
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
