"""Register-class value-type and ABI-alias resolution for the LLVM backend."""
from typing import Optional

from ...models.scalar_types import of_register


def _class_value_types(reg) -> list[str]:
    """LLVM value types a register file holds.

    The scalar element type (modern ``type:``, the legacy ``float`` flag, or a
    plain integer default) resolves through the single scalar-type source of truth
    (``scalar_types.of_register``). A 1-D shaped file (``shape: [N]``) is a vector
    of its element type → ``vN<elem-mvt>`` (e.g. ``v4i32``). The legacy
    ``value_types`` field remains an explicit verbatim override.
    """
    if not getattr(reg, "type", None) and reg.value_types:
        return list(reg.value_types)        # legacy explicit override
    elem = of_register(reg).llvm_mvt
    if getattr(reg, "is_shaped", False) and len(reg.shape) == 1:
        return [f"v{reg.lane_count}{elem}"]   # 1-D vector value type
    return [elem]


def _is_vector_class(reg) -> bool:
    """A 1-D shaped file of an int/IEEE-float element → an LLVM vector register class.
    Multi-dimensional tiles and exotic-element files are not codegen classes."""
    from ...models.scalar_types import ArithClass
    if not getattr(reg, "is_shaped", False) or len(reg.shape) != 1:
        return False
    st = of_register(reg)
    return st.arith_class in (ArithClass.INT, ArithClass.IEEE_FLOAT)


def _resolve_reg_name(registers, alias: Optional[str]) -> Optional[str]:
    """Return the canonical register name (prefix+index) for the given alias.

    Resolution is by declared alias only. There is deliberately NO positional
    fallback: silently designating e.g. register #2 as the stack pointer on an
    alias-less ISA produced wrong backends for accelerator-style targets. An ISA
    that wants the CPU conventions declares the aliases (or an explicit ABI);
    the c-baremetal profile reports unresolved sp/ra/zero as missing.
    """
    if not registers or not alias:
        return None
    first = registers[0]
    if alias in first.aliases:
        return f"{first.prefix}{first.aliases[alias]}"
    return None
