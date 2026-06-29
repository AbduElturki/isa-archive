"""Single source of truth for scalar numeric types.

isa-archive treats a register file's element type as a *scalar numeric type* with
three derived facts the code generators need:

* ``llvm_mvt``   - the LLVM machine value type (``i32``, ``f32``, ``bf16`` …).
* ``c_type``     - the QEMU host C type used to do arithmetic on it
                   (``uint32_t``, ``float``, ``double`` …), or ``None`` when the
                   host has no native type for it.
* ``arith_class``- the arithmetic semantics (integer vs IEEE float). The
                   generators branch on this to pick ``ISD::ADD`` vs ``ISD::FADD``,
                   integer vs float helpers, etc.

Float used to be a special case scattered across the generators (a boolean
``is_float`` flag plus three independent hardcoded width→type maps). This module
collapses all of that into one table so "float" is just one row, and adding a new
IEEE-float width (``f16``/``bf16``/``f128``) is a data change, not a code change.

Built-in scope: the built-ins cover types LLVM has a native MVT for (integers and
IEEE floats). Genuinely novel numerics (fixed-point, posit) still need *custom
lowering*; ``resolve`` returns ``None`` for an unknown token so callers fail loudly.

Extension point: a YAML-declared ``kind: ScalarType`` manifest registers additional
rows at load time via :func:`register_from_manifest` (sub-byte ints, FP8 formats,
bf16/tf32, …). ``resolve`` consults the registered rows first, then the built-ins.
Each backend representation is independent and optional: ``llvm_mvt`` (omit → not an
LLVM register-class element), ``c_type``/``c_include`` (QEMU C), ``cpp_type``/
``cpp_include`` (cpp-isa C++, defaulting to the C names). An ``*_include`` is needed
only when that backend's type isn't a built-in; providing the (type, include) pair
*enables* the type for that backend. The registry is process-global, keyed by token
(last definition wins); :func:`clear_registered` resets it (test isolation).
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
    llvm_mvt: Optional[str]  # MVT name (e.g. "f32"); None → no LLVM value type
    c_type: Optional[str]    # QEMU/C type name, or None
    c_include: Optional[str] = None    # header for c_type (only if not a C built-in)
    llvm_include: Optional[str] = None  # reserved (no LLVM emission site today)
    cpp_type: Optional[str] = None      # cpp-isa C++ type; defaults to c_type
    cpp_include: Optional[str] = None   # header for cpp_type; defaults to c_include

    @property
    def eff_cpp_type(self) -> Optional[str]:
        """The C++ type for cpp-isa, falling back to the C type."""
        return self.cpp_type or self.c_type

    @property
    def eff_cpp_include(self) -> Optional[str]:
        """The header for the C++ type, falling back to the C include."""
        return self.cpp_include or self.c_include


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


# Types registered at load time from `kind: ScalarType` manifests. Consulted
# before the built-ins so a manifest can also override a built-in token.
_REGISTERED: dict[str, ScalarType] = {}


def register(st: ScalarType) -> None:
    """Register a scalar type so :func:`resolve` (and thus every backend) sees it."""
    _REGISTERED[st.token] = st


def clear_registered() -> None:
    """Drop all load-time-registered scalar types (test isolation)."""
    _REGISTERED.clear()


def register_from_manifest(defn) -> ScalarType:
    """Register a `kind: ScalarType` manifest (`ScalarTypeDef`) and return the row.

    Defaults: ``llvm_mvt`` falls back to the token (float) or ``i{width}`` (int);
    ``c_type`` defaults to None (no native host arithmetic)."""
    token = defn.metadata.name
    spec = defn.spec
    arith = ArithClass.IEEE_FLOAT if spec.arith_class == "ieee_float" else ArithClass.INT
    st = ScalarType(token=token, width=spec.width, arith_class=arith,
                    llvm_mvt=spec.llvm_mvt,        # None if omitted → not LLVM-usable
                    c_type=spec.c_type, c_include=spec.c_include,
                    llvm_include=spec.llvm_include,
                    cpp_type=spec.cpp_type, cpp_include=spec.cpp_include)
    register(st)
    return st


def format_include(inc: str) -> str:
    """Format an include directive argument: verbatim if it already carries its
    delimiters (``<…>`` or ``"…"``), otherwise wrapped in angle brackets."""
    return inc if inc[:1] in '<"' else f"<{inc}>"


def resolve(token: str) -> Optional[ScalarType]:
    """Return the ScalarType for a token (registered rows first, then built-ins),
    or ``None`` if the token is not a known scalar type."""
    return _REGISTERED.get(token) or _BUILTINS.get(token)


def _opaque_int(width: int) -> ScalarType:
    """A register file with no scalar info (an Operand-struct file, or a bare
    integer file) is treated as opaque integer storage of its width."""
    c = f"uint{width}_t" if width in (8, 16, 32, 64) else None
    return ScalarType(f"i{width}", width, ArithClass.INT, f"i{width}", c)


def of_register(reg) -> ScalarType:
    """Resolve a register file's scalar element type, honoring legacy shims.

    Resolution order:
      1. modern ``type:`` - a scalar token (``i32``/``f32``); an Operand-struct
         name resolves to opaque ``i{width}`` storage.
      2. legacy ``value_types:`` - the first entry's token.
      3. legacy ``float: true`` - an IEEE float of the register's width.
      4. default - opaque integer of the register's width.
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
