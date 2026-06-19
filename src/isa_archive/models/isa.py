from typing import List, Optional, Dict, Literal
from pydantic import BaseModel
from .base import ManifestBase, StrictModel
from .csr import CSRField
from .machine import MachineLayout
from .abi import ABI
from .compiler import CompilerProfile
class RegAttr(StrictModel):
    """A named per-register attribute — runtime state carried alongside a register's
    data (e.g. a tile's layout/dtype/valid flag). Indexed by register number, read
    and written from behaviors as `reg.attr`."""
    name: str
    width: int
    description: Optional[str] = None


class Register(StrictModel):
    name: str
    width: int
    count: int
    attributes: List[RegAttr] = []  # per-register metadata fields (reg.attr in behaviors)
    zero_register: Optional[int] = None
    aliases: Dict[str, int] = {} # e.g. {"zero": 0, "ra": 1}
    canonical_prefix: Optional[str] = None  # e.g. "x" → x0..x31; defaults to first char of name
    type: Optional[str] = None  # element type: a scalar ("i32"/"f32"), a ScalarType, or an
                                # Operand struct name. Structs back an opaque i{width} class.
    shape: Optional[List[int]] = None  # if set, each register is an N-D array of `type`
                                       # elements (vector [4], tile [8,8]); omit = scalar.
    float: bool = False  # (legacy, prefer type:) True → a floating-point register file
    value_types: Optional[List[str]] = None  # (legacy, prefer type:) explicit LLVM value types

    @property
    def prefix(self) -> str:
        return self.canonical_prefix or self.name[0]

    @property
    def is_shaped(self) -> bool:
        return bool(self.shape)

    @property
    def lane_count(self) -> int:
        """Number of elements per register (product of the shape; 1 if scalar)."""
        n = 1
        for d in (self.shape or []):
            n *= d
        return n

    @property
    def element_width(self) -> int:
        """Bit width of one element (the element type's width, or `width` if scalar)."""
        from .scalar_types import of_register
        return of_register(self).width if self.is_shaped else self.width

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

class Trap(StrictModel):
    """Wiring for trap / exception / return semantics, so `trap()` / `trap_return()`
    in a behavior know which CSRs hold the vector, saved PC, and cause. The CSRs
    themselves are declared in `state.csrs`; this block only names their roles."""
    vector_csr: str             # CSR holding the trap vector (PC jumps here on trap)
    epc_csr: str                # CSR that saves the trapping PC
    cause_csr: str              # CSR that receives the trap cause code
    status_csr: Optional[str] = None  # optional; mie/mpie updated on trap/return
    causes: Dict[str, int] = {} # named cause codes, e.g. {"ecall_m": 11, "illegal": 2}

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
    asm_comment: str = "#"  # assembly line-comment string (LLVM CommentString), e.g. "#", ";", "//"
    compiler: Optional[CompilerProfile] = None  # target profile (default: c-baremetal)
    trap: Optional[Trap] = None  # trap/exception wiring (enables trap()/trap_return())

class ISA(ManifestBase):
    kind: Literal["ISA"] = "ISA"
    spec: ISASpec
