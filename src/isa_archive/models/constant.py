from typing import Literal
from pydantic import BaseModel
from .base import ManifestBase, StrictModel

class ConstantSpec(StrictModel):
    value: int
    width: int

class Constant(ManifestBase):
    kind: Literal["Constant"] = "Constant"
    spec: ConstantSpec
