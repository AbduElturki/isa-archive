from typing import List, Optional, Literal
from pydantic import BaseModel, model_validator
from .base import ManifestBase, StrictModel
from .constraint import Constraint
from .compiler import CompilerRoles
from .enums import FieldRole


class SchemaField(StrictModel):
    name: str
    start: int
    width: int
    role: FieldRole
    type: Optional[str] = None

    @model_validator(mode='after')
    def check_role_requirements(self):
        if self.role == FieldRole.REGISTER and not self.type:
            raise ValueError("Fields with role='register' must specify type (register file name)")
        return self

    @property
    def end(self) -> int: return self.start + self.width - 1
    @property
    def is_fixed_value(self) -> bool: return self.role in (FieldRole.OPCODE, FieldRole.CONSTANT, FieldRole.RESERVED)
    @property
    def is_reserved(self) -> bool: return self.role == FieldRole.RESERVED
    @property
    def is_opcode(self) -> bool: return self.role == FieldRole.OPCODE
    @property
    def is_constant(self) -> bool: return self.role == FieldRole.CONSTANT
    @property
    def maps_to_state(self) -> Optional[str]:
        return self.type if self.role == FieldRole.REGISTER else None
    @property
    def operand(self) -> Optional[str]:
        if self.role == FieldRole.IMMEDIATE and self.type and self.type.startswith("struct."):
            return self.type[len("struct."):]
        return None
    @property
    def is_signed(self) -> bool:
        return self.role == FieldRole.IMMEDIATE and self.type == "signed"
    @property
    def is_operand(self) -> bool:
        return self.role == FieldRole.IMMEDIATE
    @property
    def enum_ref(self) -> Optional[str]:
        if self.role in (FieldRole.CONSTANT, FieldRole.IMMEDIATE) and self.type and self.type.startswith("enum."):
            return self.type[len("enum."):]
        return None


class SchemaSpec(StrictModel):
    length: int
    fields: List[SchemaField]
    constraints: List[Constraint] = []
    compiler: Optional[CompilerRoles] = None  # format-level compiler-role shape default


class Schema(ManifestBase):
    kind: Literal["Schema"] = "Schema"
    spec: SchemaSpec
