from typing import List, Optional, Dict, Literal
from pydantic import BaseModel
from .base import ManifestBase, StrictModel
from .csr import CSRField
from .machine import MachineLayout
from .abi import ABI
from .compiler import CompilerProfile
class Register(StrictModel):
    name: str
    width: int
    count: int
    zero_register: Optional[int] = None
    aliases: Dict[str, int] = {} # e.g. {"zero": 0, "ra": 1}
    canonical_prefix: Optional[str] = None  # e.g. "x" → x0..x31; defaults to first char of name
    type: Optional[str] = None  # element type: a scalar ("i32"/"f32") or an Operand
                                # struct name. Structs back an opaque i{width} class.
    float: bool = False  # (legacy, prefer type:) True → a floating-point register file
    value_types: Optional[List[str]] = None  # (legacy, prefer type:) explicit LLVM value types

    @property
    def prefix(self) -> str:
        return self.canonical_prefix or self.name[0]

    @property
    def is_float(self) -> bool:
        """True if this register file holds a floating-point scalar type.

        Delegates to the single scalar-type source of truth (scalar_types) so the
        int/float decision is made in exactly one place."""
        from .scalar_types import of_register, ArithClass
        return of_register(self).arith_class == ArithClass.IEEE_FLOAT

class ISACSR(StrictModel):
    name: str
    address: int
    width: int
    reset_value: int = 0
    fields: List[CSRField] = []

class ISAState(StrictModel):
    registers: List[Register] = []
    csrs: List[ISACSR] = []

class ISASpec(StrictModel):
    name: Optional[str] = None
    version: str
    xlen: int = 32
    extends: Optional[str] = None # Path to base ISA manifest
    state: ISAState = ISAState()
    includes: List[str] = [] # Glob patterns to include other YAML files
    machine: Optional[MachineLayout] = None
    abi: Optional[ABI] = None
    triple_arch: Optional[str] = None  # LLVM Triple arch name, e.g. "riscv32"
    elf_machine: Optional[int] = None  # ELF e_machine code (243=EM_RISCV, 40=EM_ARM, 0=custom)
    nop_encoding: Optional[str] = None  # NOP value as big-endian hex, e.g. "00000013"
    elf_relocations: Optional[Dict[str, str]] = None  # {"jal": "R_RISCV_JAL", ...}
    byte_order: Literal["little", "big"] = "little"
    compiler: Optional[CompilerProfile] = None  # target profile (default: c-baremetal)

class ISA(ManifestBase):
    kind: Literal["ISA"] = "ISA"
    spec: ISASpec
