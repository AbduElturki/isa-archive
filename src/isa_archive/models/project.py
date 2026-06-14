from typing import List, Literal, Optional
from .base import ManifestBase, StrictModel


class GenerateEntry(StrictModel):
    """One generation request: a target/sub-target and where to write it."""
    target: str                # a target name from the taxonomy (generators/targets.py)
    output: str                # output directory, relative to the project file
    on_exist: Literal["overwrite", "skip", "error"] = "overwrite"
    clang_format: bool = False  # run clang-format on generated C/C++
    strict: bool = False        # LLVM: fail on missing required compiler role
    format: Optional[str] = None  # docs: md | html | pdf (overrides the target default)


class ProjectSpec(StrictModel):
    isas: List[str]            # one or more ISA manifest paths (relative to this file)
    uarch: List[str] = []      # optional uArch manifest paths
    generate: List[GenerateEntry]


class Project(ManifestBase):
    kind: Literal["Project"] = "Project"
    spec: ProjectSpec
