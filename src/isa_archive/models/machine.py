from typing import List, Optional
from pydantic import BaseModel
from .base import StrictModel


class DeviceDef(StrictModel):
    name: str
    type: str          # "ns16550" | "sifive_test" | "irq_test"
    base: int
    irq: Optional[int] = None


class QemuConfig(StrictModel):
    devices: List[DeviceDef] = []


class MachineLayout(StrictModel):
    ram_base: int = 0x80000000
    ram_size: int = 128 * 1024 * 1024
    reset_vector: Optional[int] = None  # None → ram_base at render time
    qemu: Optional[QemuConfig] = None

    def effective_reset_vector(self) -> int:
        return self.reset_vector if self.reset_vector is not None else self.ram_base
