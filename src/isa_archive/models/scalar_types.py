"""Single source of truth for scalar numeric types.

isa-archive treats a register file's element type as a *scalar numeric type* with
three derived facts the code generators need:

* ``llvm_mvt``   — the LLVM machine value type (``i32``, ``f32``, ``bf16`` …).
* ``c_type``     — the QEMU host C type used to do arithmetic on it
                   (``uint32_t``, ``float``, ``double`` …), or ``None`` when the
                   host has no native type for it.
* ``arith_class``— the arithmetic semantics (integer vs IEEE float). The
                   generators branch on this to pick ``ISD::ADD`` vs ``ISD::FADD``,
                   integer vs float helpers, etc.

Float used to be a special case scattered across the generators (a boolean
``is_float`` flag plus three independent hardcoded width→type maps). This module
collapses all of that into one table so "float" is just one row, and adding a new
IEEE-float width (``f16``/``bf16``/``f128``) is a data change, not a code change.

Honest ceiling: this only covers types LLVM has a native MVT for (integers and
IEEE floats). Genuinely novel numerics (fixed-point, posit, 8-bit floats) have no
MVT and need *custom lowering*, which is out of scope here — ``resolve`` returns
``None`` for them so callers fail loudly rather than silently mis-lowering.

Future extension: a YAML-declared ``kind: ScalarType`` manifest could register
additional rows at load time. That is deliberately not implemented yet — the
built-in table below covers every int + IEEE-float ISA, and exotic types need
custom lowering regardless.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ArithClass(Enum):
    """Arithmetic semantics of a scalar type. Extensible (FIXED_POINT/POSIT would
    go here) but only INT and IEEE_FLOAT have native LLVM MVTs today."""

    INT = "int"
    IEEE_FLOAT = "ieee_float"


@dataclass(frozen=True)
class ScalarType:
    token: str  # canonical key, e.g. "i32", "f32", "bf16"
    width: int  # bits
    arith_class: ArithClass
    llvm_mvt: str  # MVT name without the "MVT::" prefix, e.g. "f32", "i32"
    c_type: Optional[str]  # QEMU host C type, or None if no native host type


def _int(width: int, c_type: Optional[str]) -> ScalarType:
    return ScalarType(f"i{width}", width, ArithClass.INT, f"i{width}", c_type)


def _flt(width: int, c_type: Optional[str], token: Optional[str] = None) -> ScalarType:
    tok = token or f"f{width}"
    return ScalarType(tok, width, ArithClass.IEEE_FLOAT, tok, c_type)


# THE one place width→{mvt, c_type, arith} lives.
_BUILTINS: dict[str, ScalarType] = {
    st.token: st
    for st in (
        _int(8, "uint8_t"),
        _int(16, "uint16_t"),
        _int(32, "uint32_t"),
        _int(64, "uint64_t"),
        _int(128, "__uint128_t"),
        # IEEE floats. 16-bit floats and f128 have an MVT but no portable native
        # host C type → c_type=None (QEMU side needs softfloat; LLVM side is fine).
        _flt(16, None),  # f16
        _flt(16, None, token="bf16"),  # brain-float; distinct MVT
        _flt(32, "float"),
        _flt(64, "double"),
        _flt(128, None),
    )
}


def resolve(token: str) -> Optional[ScalarType]:
    """Return the ScalarType for an ``iN``/``fN``/``bf16`` token, or ``None`` if
    the token is not a known native scalar type."""
    return _BUILTINS.get(token)


def _opaque_int(width: int) -> ScalarType:
    """A register file with no scalar info (an Operand-struct file, or a bare
    integer file) is treated as opaque integer storage of its width."""
    c = f"uint{width}_t" if width in (8, 16, 32, 64) else None
    return ScalarType(f"i{width}", width, ArithClass.INT, f"i{width}", c)


def of_register(reg) -> ScalarType:
    """Resolve a register file's scalar element type, honoring legacy shims.

    Resolution order:
      1. modern ``type:`` — a scalar token (``i32``/``f32``); an Operand-struct
         name resolves to opaque ``i{width}`` storage.
      2. legacy ``value_types:`` — the first entry's token.
      3. legacy ``float: true`` — an IEEE float of the register's width.
      4. default — opaque integer of the register's width.
    """
    t = getattr(reg, "type", None)
    if t:
        st = resolve(t)
        if st is not None:
            return st
        # An Operand-struct name (validated elsewhere) → opaque integer storage.
        return _opaque_int(reg.width)
    vts = getattr(reg, "value_types", None)
    if vts:
        st = resolve(vts[0])
        if st is not None:
            return st
    if getattr(reg, "float", False):
        st = resolve(f"f{reg.width}")
        if st is not None:
            return st
    return _opaque_int(reg.width)
