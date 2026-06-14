from typing import Dict, Literal
from pydantic import BaseModel
from .base import ManifestBase, StrictModel

class EnumDefSpec(StrictModel):
    width: int
    values: Dict[str, int]

class EnumDef(ManifestBase):
    kind: Literal["Enum"] = "Enum"
    spec: EnumDefSpec
