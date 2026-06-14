from typing import List, Optional, Dict
from pydantic import BaseModel
from .base import StrictModel


class ABI(StrictModel):
    stack_alignment: int = 16
    arg_registers: List[str] = []
    ret_registers: List[str] = []
    callee_saved: List[str] = []
    frame_pointer: Optional[str] = None
    fp_arg_registers: List[str] = []   # floating-point argument registers (hard-float ABI)
    fp_ret_registers: List[str] = []   # floating-point return-value registers

    def resolve(self, aliases: Dict[str, int]) -> "ABI":
        """Fill empty lists by inferring from alias naming conventions.

        Registers whose ABI alias starts with 'a' → argument registers.
        Registers whose ABI alias starts with 's' → callee-saved.
        'ra', 'sp', 'gp', 'tp' are always treated as callee-saved.
        First two arg registers become return registers when ret_registers is empty.
        """
        if self.arg_registers and self.ret_registers and self.callee_saved:
            return self

        special = {"ra", "sp", "gp", "tp"}
        arg = list(self.arg_registers)
        ret = list(self.ret_registers)
        saved = list(self.callee_saved)
        fp = self.frame_pointer

        if not arg:
            arg = sorted(
                (name for name in aliases if name.startswith("a") and name[1:].isdigit()),
                key=lambda n: int(n[1:]),
            )
        if not ret:
            ret = arg[:2]
        if not saved:
            saved = sorted(
                [name for name in aliases if name in special or
                 (name.startswith("s") and (name[1:].isdigit() or name == "s0"))],
            )
        if fp is None and "s0" in aliases:
            fp = "s0"

        return ABI(
            stack_alignment=self.stack_alignment,
            arg_registers=arg,
            ret_registers=ret,
            callee_saved=saved,
            frame_pointer=fp,
            fp_arg_registers=list(self.fp_arg_registers),
            fp_ret_registers=list(self.fp_ret_registers),
        )
