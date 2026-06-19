"""QEMU hardware-interrupt delivery (the CPU side).

An ISA that declares a `trap:` block delivers exceptions and hardware interrupts
by vectoring through the trap CSRs (the same path software trap() uses) instead of
halting. An ISA without a `trap:` block keeps the halt-on-exception fallback,
byte-identical.
"""
import pathlib

from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.qemu import generate_qemu

EX = pathlib.Path(__file__).resolve().parent.parent / "examples"
SYS = EX / "tutorial/pico32-part4/sys/isa.yaml"      # declares a trap: block
PICO32 = EX / "tutorial/pico32-part4/isa.yaml"        # no trap: block


def _gen(tmp_path, isa_yaml):
    reg = Registry()
    top = load_isa(str(isa_yaml), reg)
    reg.isas = {top.name: top}   # emit only the requested ISA (extends base is resolution-only)
    generate_qemu(reg, str(tmp_path))
    return tmp_path, top.name


def _cpu_c(tmp_path, isa_yaml):
    root, name = _gen(tmp_path, isa_yaml)
    return (root / "target" / name / f"{name}_cpu.c").read_text()


def _virt_c(tmp_path, isa_yaml):
    root, name = _gen(tmp_path, isa_yaml)
    return (root / "hw" / name / "virt.c").read_text()


def test_trap_isa_delivers_interrupts(tmp_path):
    c = _cpu_c(tmp_path, SYS)
    # exec_interrupt takes a pending hard IRQ only when mstatus.mie (bit 3) is set.
    assert "interrupt_request & CPU_INTERRUPT_HARD" in c
    assert "((env->mstatus >> 3) & 1)" in c
    assert "cs->exception_index = EXCP_IRQ;" in c
    # do_interrupt vectors through the trap CSRs (interrupt marker vs exception cause).
    assert "env->mcause = cause;" in c
    assert "0x80000000" in c                       # mcause.interrupt bit (bit 31)
    assert "env->pc = env->mtvec & ~0x3;" in c
    assert "cpu_reset_interrupt(cs, CPU_INTERRUPT_HARD);" in c
    # no longer halts on an exception
    assert "cs->halted = 1;" not in c


def test_trap_isa_cpu_has_irq_input_line(tmp_path):
    # The CPU exposes one hard-IRQ input line a board can pulse.
    c = _cpu_c(tmp_path, SYS)
    assert "qdev_init_gpio_in(dev, pico32sys_cpu_set_irq, 1);" in c
    assert "cpu_interrupt(cs, CPU_INTERRUPT_HARD);" in c


def test_board_emits_irq_test_device(tmp_path):
    # The `irq_test` machine device becomes an MMIO register wired to the CPU's
    # IRQ line: writing it raises the interrupt.
    v = _virt_c(tmp_path, SYS)
    assert "#define VIRT_IRQTEST_BASE  0x02000000UL" in v
    assert "pico32sys_test_irq = qdev_get_gpio_in(DEVICE(cpuobj), 0);" in v
    assert "qemu_set_irq(pico32sys_test_irq, val != 0);" in v
    assert "memory_region_init_io(irqtest" in v


def test_non_trap_isa_keeps_halt_fallback(tmp_path):
    # Byte-identical guard: an ISA with no trap: block emits the original
    # no-interrupt / halt-on-exception bodies, untouched.
    c = _cpu_c(tmp_path, PICO32)
    assert "return false;  /* no interrupts */" in c
    assert "cs->halted = 1;" in c
    assert "EXCP_IRQ" not in c
    assert "CPU_INTERRUPT_HARD" not in c
    # and its board has no interrupt-test device
    v = _virt_c(tmp_path, PICO32)
    assert "irqtest" not in v and "qdev_get_gpio_in" not in v
