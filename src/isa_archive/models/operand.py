from __future__ import annotations
from typing import List, Optional, Union, Literal
from pydantic import BaseModel, Field
from .base import ManifestBase, StrictModel
from .constraint import Constraint

class OperandField(StrictModel):
    name: str
    start: int
    width: Optional[int] = None
    type: Optional[str] = None # Reference to another Operand metadata.name
    fields: Optional[List[OperandField]] = None

    @property
    def end(self) -> Optional[int]:
        if self.width is not None:
            return self.start + self.width - 1
        return None

class OperandSpec(StrictModel):
    width: int
    maps_to_state: Optional[str] = None # Name of architectural register array (e.g. gpr, fpr)
    fields: Optional[List[OperandField]] = None
    constraints: List[Constraint] = []

class Operand(ManifestBase):
    kind: Literal["Operand"] = "Operand"
    spec: OperandSpec

OperandField.model_rebuild()
