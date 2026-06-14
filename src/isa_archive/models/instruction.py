from typing import Dict, List, Union, Optional, Any, Literal
from pydantic import BaseModel, Field, model_validator
from .base import ManifestBase, StrictModel
from .constraint import Constraint
from .compiler import CompilerRoles

_SPEC_KEYS = {"schema", "exec_type", "opcode", "behavior", "description", "constraints", "constants", "compiler"}

class InstructionSpec(StrictModel):
    schema_name: str = Field(..., alias="schema")
    exec_type: Optional[str] = None
    opcode: Union[int, str]
    constants: Dict[str, Union[int, str]] = {}
    behavior: str
    description: Optional[str] = None
    constraints: List[Constraint] = []
    compiler: Optional[CompilerRoles] = None  # per-instruction compiler-role override

    model_config = {"populate_by_name": True}

    @model_validator(mode='before')
    @classmethod
    def hoist_flat_constants(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for forbidden in ("opcodes", "encoding", "fixed"):
            if forbidden in data:
                raise ValueError(
                    f"use flat syntax 'fieldname: VALUE' directly in spec, not '{forbidden}: {{...}}'"
                )
        extra = {k: v for k, v in data.items() if k not in _SPEC_KEYS}
        clean = {k: v for k, v in data.items() if k in _SPEC_KEYS}
        if extra:
            existing = clean.get("constants", {})
            clean["constants"] = {**(existing if isinstance(existing, dict) else {}), **extra}
        return clean

class Instruction(ManifestBase):
    kind: Literal["Instruction"] = "Instruction"
    spec: InstructionSpec
