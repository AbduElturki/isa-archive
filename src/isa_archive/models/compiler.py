from typing import List, Literal, Union
from pydantic import BaseModel, model_validator
from .base import StrictModel


class CompilerProfile(StrictModel):
    """ISA-level declaration of what the compiler backend is *for* (G1).

    The profile decides which compiler roles are required for the backend to be
    considered complete, and whether CPU conveniences (a stack pointer, calls,
    globals) are expected at all:

    - ``c-baremetal`` (default): the current contract — the backend must lower
      freestanding C: full ALU, full-word load/store, branches, calls, a stack.
      Missing sp/ra aliases are an error rather than silently invented.
    - ``kernel-only``: a compute target (GPU/NPU style). Nothing is required;
      no stack/call/global support is expected, and the coverage report is
      informational. Stack-less ISAs are first-class, not "INCOMPLETE".
    - ``custom``: the required role set is exactly ``requires``.
    """
    profile: Literal["c-baremetal", "kernel-only", "custom"] = "c-baremetal"
    requires: List[str] = []

    @model_validator(mode="after")
    def check_requires_usage(self):
        if self.profile != "custom" and self.requires:
            raise ValueError(
                "compiler.requires is only meaningful with profile: custom"
            )
        return self


class CompilerRoles(StrictModel):
    """Compiler-role tags attached to a Schema (shape default for a whole format)
    or an Instruction (per-instruction override).

    A role names a slot the code generator must fill to lower LLVM IR, e.g.
    ``alu_rr.add``, ``const.hi``, ``branch.eq``, ``frame.sp_adjust``. A bare
    shape (``alu_rr``, ``branch``) declared at schema level is expanded to the
    specific role using the behavior-inferred operation/condition.

    Roles are resolved in three layers, each overriding the previous:
    behavior inference → schema-level → instruction-level.
    """
    roles: List[str] = []

    @model_validator(mode="before")
    @classmethod
    def accept_bare_list(cls, data: Union[list, dict, "CompilerRoles"]):
        # Allow `compiler: [alu_rr]` shorthand in addition to `compiler: {roles: [...]}`.
        if isinstance(data, list):
            return {"roles": data}
        return data
