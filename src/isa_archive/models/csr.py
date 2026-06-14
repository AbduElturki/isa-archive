from typing import List, Optional
from pydantic import BaseModel
from .base import StrictModel
from .enums import AccessMode


class CSRField(StrictModel):
    name: str
    start: int
    end: int
    description: Optional[str] = None
    access: AccessMode = AccessMode.RW

