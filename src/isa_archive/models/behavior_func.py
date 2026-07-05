from typing import List, Optional, Literal
from .base import ManifestBase, StrictModel


class ArgDef(StrictModel):
    """One argument of a BehaviorFunc.

    `type` is a scalar token (`i32`/`f32`, or a `kind: ScalarType`) or a
    `kind: Operand` name. Args are read-only by default; `editable: true` makes
    it an out/inout parameter - the caller must pass an lvalue (a register
    operand, `reg.attr`, or `vd[i]`) and writes inside the body reflect back.
    """
    name: str
    type: str
    editable: bool = False


class BehaviorFuncSpec(StrictModel):
    args: List[ArgDef] = []
    returns: Optional[str] = None   # omit → the function returns nothing (void)
    behavior: str                   # body, in the behavior DSL; one tail `return` if `returns` set


class BehaviorFunc(ManifestBase):
    """A reusable, typed function callable inside instruction `behavior:` strings.

    Calls are expanded by inlining the body into the caller before any backend
    runs, so a BehaviorFunc works in every target with no per-backend support.
    """
    kind: Literal["BehaviorFunc"] = "BehaviorFunc"
    spec: BehaviorFuncSpec
