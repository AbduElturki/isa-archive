"""Register-class value-type and ABI-alias resolution for the LLVM backend."""
from typing import Optional

from ...models.scalar_types import of_register


def _class_value_types(reg) -> list[str]:
    """LLVM value types a register file holds.

    The scalar element type (modern ``type:``, the legacy ``float`` flag, or a
    plain integer default) resolves through the single scalar-type source of truth
    (``scalar_types.of_register``). The legacy ``value_types`` field remains an
    explicit verbatim override of the LLVM value-type list.
    """
    if not getattr(reg, "type", None) and reg.value_types:
        return list(reg.value_types)        # legacy explicit override
    return [of_register(reg).llvm_mvt]


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
