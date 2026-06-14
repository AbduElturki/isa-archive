from typing import Optional
from pydantic import BaseModel, model_validator
from .base import StrictModel


class Constraint(StrictModel):
    expr: str
    message: Optional[str] = None

    @model_validator(mode='before')
    @classmethod
    def accept_string_shorthand(cls, data):
        if isinstance(data, str):
            return {"expr": data}
        return data
