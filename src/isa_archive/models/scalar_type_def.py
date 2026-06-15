"""`kind: ScalarType` — a YAML-declared scalar numeric type.

Registers an element type (its token = `metadata.name`) that a register file's
`type:` can then name, extending the built-in int/IEEE-float table in
`scalar_types.py` without editing Python. Used for sub-byte ints, FP8 formats,
bf16/tf32, etc. The `arith_class` is limited to the two classes with native
lowering; a type with `c_type: null` stores and moves but has no host
arithmetic (exactly like the built-in f16/f128 — QEMU rejects arithmetic on it
loudly, LLVM still uses its MVT)."""
from typing import Literal, Optional

from .base import ManifestBase, StrictModel


class ScalarTypeSpec(StrictModel):
    """Each backend speaks a different language, so the type name + header are declared
    per backend, each optional. An include is only needed when that backend's type
    isn't a built-in; providing the (type, include) ENABLES the type for that backend."""
    width: int
    arith_class: Literal["int", "ieee_float"] = "int"
    # LLVM: the value-type MVT (e.g. "f8E4M3"). Optional — omit and a register file using
    # this type is not an LLVM register class (it stays simulator-only in the compiler).
    llvm_mvt: Optional[str] = None
    llvm_include: Optional[str] = None  # reserved: the LLVM backend emits only built-in MVTs,
                                        # so there is no header-emission site today.
    # QEMU (C): the host C type used in the u2f/f2u float helpers, and its header.
    c_type: Optional[str] = None
    c_include: Optional[str] = None
    # cpp-isa (C++): the C++ type and its header; each defaults to the C equivalent.
    cpp_type: Optional[str] = None
    cpp_include: Optional[str] = None


class ScalarTypeDef(ManifestBase):
    kind: Literal["ScalarType"] = "ScalarType"
    spec: ScalarTypeSpec
