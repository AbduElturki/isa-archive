"""LLVM backend generator. Public API is re-exported here so `from .llvm import …`
(and the internals imported by other generators and the tests) keep working after
the package split."""
from .core import generate_llvm
from .instr_defs import _build_instr_defs
from .encoding import _get_schema_combined_imm
from .coverage import _required_roles, _role_groups, _setcc_branch_entries

__all__ = [
    "generate_llvm",
    "_build_instr_defs",
    "_get_schema_combined_imm",
    "_required_roles",
    "_role_groups",
    "_setcc_branch_entries",
]
