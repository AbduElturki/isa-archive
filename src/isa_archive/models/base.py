from pydantic import BaseModel, Field
from typing import Dict, Optional


class StrictModel(BaseModel):
    """Base for all manifest models: unknown YAML keys are an error, not silently
    ignored — a typo'd key must never produce a quietly different model."""
    model_config = {"extra": "forbid"}


class Metadata(StrictModel):
    name: str
    description: Optional[str] = None
    annotations: Dict[str, str] = Field(default_factory=dict)


class ManifestBase(StrictModel):
    apiVersion: str = "isa-archive/v1"
    kind: str
    metadata: Metadata
