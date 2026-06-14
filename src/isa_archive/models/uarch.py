from typing import List, Optional, Literal
from pydantic import BaseModel
from .base import ManifestBase, StrictModel
from .csr import CSRField

class uArchCSR(StrictModel):
    name: str
    address: int
    width: int
    reset_value: int = 0
    fields: List[CSRField] = []

class uArchState(StrictModel):
    csrs: List[uArchCSR] = []

class uArchBlock(StrictModel):
    name: str
    count: int
    latency: int = 1
    pipelined: bool = True
    handles: List[str] = []

class uArchSpec(StrictModel):
    isa: str # Name of the ISA it supports
    state: uArchState = uArchState()
    blocks: List[uArchBlock] = []
    includes: List[str] = [] # Glob patterns to include other YAML files

class uArch(ManifestBase):
    kind: Literal["uArch"] = "uArch"
    spec: uArchSpec
