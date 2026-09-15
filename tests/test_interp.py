"""The reference interpreter: DSL semantics executed straight from the manifest.

Two kinds of test:
  * operator semantics on a synthetic register map (signedness, promotion,
    wrap-around, shifts, division, concatenation, memory, pc);
  * end-to-end on pico32: assemble with the generated assembler, run on the
    interpreter, check UART output / exit code / register state - the same
    program shape CI runs on the generated QEMU.
"""
import pathlib
import subprocess
import sys

import pytest

from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.interp import (HaltRequested, InterpError, Interpreter,
                                         MachineState, _Exec, _InstrInfo,
                                         attach_machine_devices, load_image,
                                         run_program)
from isa_archive.compiler.loader import Registry, load_isa
from isa_archive.generators.assembler import generate_asm

EX = pathlib.Path(__file__).resolve().parent.parent / "examples"
PICO32 = EX / "tutorial/pico32-part4/isa.yaml"
PICO32SYS = EX / "tutorial/pico32-part4/sys/isa.yaml"


# ── Behavior-level harness ───────────────────────────────────────────────────

class _FakeReg:
    def __init__(self, width):
        self.width = width
        self.zero_register = None


class _FakeInterp:
    """Just enough of Interpreter for _Exec: 32-bit xlen, one 32-bit 'gpr'."""
    xlen = 32
    regs = {"gpr": _FakeReg(32)}
    shapes = {}
    csr_info = {}
    trap = None

    class isa:
        constants = {}
        enums = {}


def run_behavior(behavior, regs=None, imm=None, pc=0, mem=None, xlen=32):
    """Execute `behavior` with rd/rs1/rs2 → gpr[1..3] and `imm` a 12-bit
    signed immediate. Returns the MachineState."""
    reg_map = {"rd": "gpr", "rs1": "gpr", "rs2": "gpr"}
    widths = {"rd": xlen, "rs1": xlen, "rs2": xlen, "imm": 12, "pc": xlen}
    ir = BehaviorIR(behavior, register_map=reg_map, var_widths=widths)
    info = _InstrInfo(name="T", schema_name="S", nbytes=4, mask=0, match=0,
                      fixed_bits=0, fields=[], reg_fields=reg_map, zero_regs={},
                      ir=ir, var_widths=ir.var_widths)
    st = MachineState(xlen=xlen, pc=pc, regs={"gpr": [0] * 4}, reg_widths={"gpr": xlen})
    for k, v in (regs or {}).items():
        st.regs["gpr"][k] = v
    for a, b in (mem or {}).items():
        st.mem[a] = b
    fields = {"rd": 1, "rs1": 2, "rs2": 3, "imm": imm if imm is not None else 0}
    fi = _FakeInterp()
    fi.xlen = xlen
    fi.regs = {"gpr": _FakeReg(xlen)}
    ex = _Exec(fi, info, fields, st)
    for stmt in ir.tree.body:
        ex.stmt(stmt)
    st.trace.append("pc_written" if ex.pc_written else "")
    return st


def rd(st):
    return st.regs["gpr"][1]


@pytest.mark.parametrize("behavior,rs1,rs2,expect", [
    ("rd = rs1 + rs2", 0xFFFFFFFF, 1, 0),                  # wraps at 32
    ("rd = rs1 - rs2", 0, 1, 0xFFFFFFFF),
    ("rd = rs1 * rs2", 0x10000, 0x10000, 0),                # low 32 bits only
    ("rd = rs1 & rs2", 0xF0F0, 0xFF00, 0xF000),
    ("rd = rs1 | rs2", 0xF0F0, 0x0F0F, 0xFFFF),
    ("rd = rs1 ^ rs2", 0xFFFF, 0x0F0F, 0xF0F0),
    ("rd = rs1 << rs2[0:5]", 1, 31, 0x80000000),
    ("rd = rs1 << rs2[0:5]", 1, 32, 1),                     # shamt masked to 5 bits
    ("rd = rs1 >> rs2[0:5]", 0x80000000, 31, 1),            # logical
    ("rd = signed(rs1) >> rs2[0:5]", 0x80000000, 31, 0xFFFFFFFF),  # arithmetic
    ("rd = rs1 / rs2", 0xFFFFFFFE, 2, 0x7FFFFFFF),          # unsigned
    ("rd = signed(rs1) / signed(rs2)", 0xFFFFFFFE, 2, 0xFFFFFFFF),  # -2 / 2 = -1
    ("rd = signed(rs1) / signed(rs2)", 0xFFFFFFF9, 2, 0xFFFFFFFD),  # -7 / 2 = -3 (trunc)
    ("rd = rs1 % rs2", 0xFFFFFFFF, 10, 5),                  # 4294967295 % 10
    ("rd = signed(rs1) % signed(rs2)", 0xFFFFFFF9, 2, 0xFFFFFFFF),  # -7 % 2 = -1 (C)
    ("rd = ~rs1", 0x0000FFFF, 0, 0xFFFF0000),
    ("rd = rs1[8:16]", 0x12345678, 0, 0x56),
    ("rd = {rs1[0:8], rs2[0:8]}", 0xAB, 0xCD, 0xABCD),
])
def test_alu_semantics(behavior, rs1, rs2, expect):
    assert rd(run_behavior(behavior, {2: rs1, 3: rs2})) == expect


@pytest.mark.parametrize("behavior,rs1,rs2,expect", [
    ("if rs1 < rs2:\n    rd = 1\nelse:\n    rd = 0", 0xFFFFFFFF, 1, 0),   # unsigned
    ("if signed(rs1) < signed(rs2):\n    rd = 1\nelse:\n    rd = 0", 0xFFFFFFFF, 1, 1),
    ("if rs1 == rs2:\n    rd = 1\nelse:\n    rd = 0", 7, 7, 1),
    ("if rs1 != rs2 and rs1 > 3:\n    rd = 1\nelse:\n    rd = 0", 5, 2, 1),
])
def test_compare_semantics(behavior, rs1, rs2, expect):
    assert rd(run_behavior(behavior, {2: rs1, 3: rs2})) == expect


def test_signed_immediate_arrives_sign_extended():
    # decodetree hands a signed 12-bit field to the helper sign-extended; the
    # helper's uint32_t parameter then holds the two's-complement pattern.
    st = run_behavior("rd = rs1 + imm", {2: 100}, imm=-5)
    assert rd(st) == 95
    st = run_behavior("rd = rs1 + imm", {2: 0}, imm=-1)
    assert rd(st) == 0xFFFFFFFF


def test_sext_zext_and_shift_materialization():
    assert rd(run_behavior("rd = zext(imm) << 12", imm=0x7FF)) == 0x7FF000
    assert rd(run_behavior("rd = sext({imm, 0}, 13)", imm=0x800)) == 0xFFFFF000
    # sext'd value compares signed
    st = run_behavior("if sext(imm, 12) < 0:\n    rd = 1\nelse:\n    rd = 0", imm=-1)
    assert rd(st) == 1


def test_memory_little_endian_and_widths():
    st = run_behavior("mem32[rs1 + imm] = rs2", {2: 0x1000, 3: 0x11223344}, imm=4)
    assert [st.mem[0x1004 + i] for i in range(4)] == [0x44, 0x33, 0x22, 0x11]
    st.regs["gpr"][2] = 0x1004
    ex_st = run_behavior("rd = mem8[rs1]", {2: 0x1005}, mem=st.mem)
    assert rd(ex_st) == 0x33
    ex_st = run_behavior("rd = mem16[rs1]", {2: 0x1004}, mem=st.mem)
    assert rd(ex_st) == 0x3344


def test_big_endian_memory():
    st = MachineState(xlen=32, byte_order="big")
    st.store(0x10, 32, 0x11223344)
    assert [st.mem[0x10 + i] for i in range(4)] == [0x11, 0x22, 0x33, 0x44]
    assert st.load(0x12, 16) == 0x3344


def test_pc_relative_branch_and_pc_written_flag():
    st = run_behavior("if rs1 == rs2:\n    pc = pc + sext({imm, 0}, 13)",
                      {2: 1, 3: 1}, imm=-4, pc=0x100)
    assert st.pc == 0x100 - 8 and st.trace[-1] == "pc_written"
    st = run_behavior("if rs1 == rs2:\n    pc = pc + sext({imm, 0}, 13)",
                      {2: 1, 3: 2}, imm=-4, pc=0x100)
    assert st.pc == 0x100 and st.trace[-1] == ""      # not taken: caller advances


def test_temporaries_and_for_loop():
    st = run_behavior("acc = 0\nfor i in range(4):\n    acc = acc + rs1\nrd = acc", {2: 3})
    assert rd(st) == 12


def test_division_by_zero_is_an_error():
    with pytest.raises(InterpError, match="division by zero"):
        run_behavior("rd = rs1 / rs2", {2: 1, 3: 0})


def test_promotion_width_follows_widest_operand():
    # 64-bit registers: the arithmetic wraps at 64, not 32.
    st = run_behavior("rd = rs1 + rs2", {2: 0xFFFFFFFF, 3: 1}, xlen=64)
    assert rd(st) == 0x100000000


# ── ISA-level: decode + execute on pico32 ────────────────────────────────────

@pytest.fixture(scope="module")
def pico32():
    reg = Registry()
    return load_isa(str(PICO32), reg)


@pytest.fixture(scope="module")
def pico32_asm(tmp_path_factory, pico32):
    out = tmp_path_factory.mktemp("asm")
    reg = Registry()
    load_isa(str(PICO32), reg)
    generate_asm(reg, str(out))
    return out / "pico32_asm.py"


def assemble(asm_py, src_text, tmp_path, elf=False):
    src = tmp_path / "prog.s"
    src.write_text(src_text)
    out = tmp_path / ("prog.elf" if elf else "prog.bin")
    args = [sys.executable, str(asm_py), str(src), "-o", str(out)]
    if elf:
        args.append("--elf")
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out.read_bytes()


def test_decode_matches_assembler_encoding(pico32):
    it = Interpreter(pico32)
    # ADD r1, r2, r3  (RISC-V layout: funct7=0 rs2=3 rs1=2 funct3=0 rd=1 opcode=0x33)
    word = (3 << 20) | (2 << 15) | (1 << 7) | 0x33
    name, fields = it.decode(word)
    assert name == "ADD" and fields == {"rd": 1, "rs1": 2, "rs2": 3}
    # ADDI r1, r2, -1: signed field comes back sign-extended
    word = (0xFFF << 20) | (2 << 15) | (1 << 7) | 0x13
    name, fields = it.decode(word)
    assert name == "ADDI" and fields["imm"] == -1


def test_decode_rejects_illegal_word(pico32):
    with pytest.raises(InterpError, match="illegal instruction"):
        Interpreter(pico32).decode(0)


def test_zero_register_writes_are_dropped(pico32):
    it = Interpreter(pico32)
    st = MachineState.for_isa(pico32)
    it.execute("ADDI", {"rd": 0, "rs1": 0, "imm": 42}, st)
    assert st.regs["gpr"][0] == 0
    assert st.pc == pico32.machine.effective_reset_vector() + 4


LOOP_PROGRAM = """\
.text
    lui   r1, 0x10000       # UART base
    addi  r5, r0, 0         # sum = 0
    addi  r6, r0, 10        # i = 10
loop:
    add   r5, r5, r6        # sum += i
    addi  r6, r6, -1
    bne   r6, r0, loop
    addi  r2, r5, 10        # 55 + 10 = 'A'
    sw    r2, 0(r1)         # UART <- 'A'
    lui   r3, 0x100         # sifive_test
    lui   r4, 0x5
    addi  r4, r4, 0x555     # 0x5555: pass
    sw    r4, 0(r3)
    addi  r7, r0, 1         # never reached
"""


def _run(pico32, image, out):
    it = Interpreter(pico32)
    st = MachineState.for_isa(pico32)
    attach_machine_devices(st, pico32, out=out)
    st.pc = load_image(st, image)
    code = run_program(it, st, max_steps=10_000)
    return code, st


def test_end_to_end_flat_binary(pico32, pico32_asm, tmp_path):
    import io
    out = io.StringIO()
    code, st = _run(pico32, assemble(pico32_asm, LOOP_PROGRAM, tmp_path), out)
    assert code == 0
    assert out.getvalue() == "A"
    assert st.regs["gpr"][5] == 55 and st.regs["gpr"][6] == 0
    assert st.regs["gpr"][7] == 0            # halted before the last instruction


def test_end_to_end_elf(pico32, pico32_asm, tmp_path):
    import io
    out = io.StringIO()
    code, st = _run(pico32, assemble(pico32_asm, LOOP_PROGRAM, tmp_path, elf=True), out)
    assert code == 0 and out.getvalue() == "A"


def test_step_limit_returns_minus_one(pico32, pico32_asm, tmp_path):
    image = assemble(pico32_asm, "loop:\n    jal r0, loop\n", tmp_path)
    it = Interpreter(pico32)
    st = MachineState.for_isa(pico32)
    st.pc = load_image(st, image)
    assert run_program(it, st, max_steps=50) == -1
    assert st.pc == pico32.machine.effective_reset_vector()


# ── CSRs and traps on the pico32 sys extension ───────────────────────────────

@pytest.fixture(scope="module")
def pico32sys():
    reg = Registry()
    return load_isa(str(PICO32SYS), reg)


def test_trap_and_return_vector_through_csrs(pico32sys):
    it = Interpreter(pico32sys)
    st = MachineState.for_isa(pico32sys)
    base = st.pc
    # CSRW_TVEC: rd ← mtvec; mtvec ← rs1   (set the vector to base+0x100)
    st.regs["gpr"][2] = base + 0x100
    st.csrs["mstatus"] = 1 << 3                       # mie = 1
    it.execute("CSRW_TVEC", {"rd": 1, "rs1": 2}, st)
    assert st.csrs["mtvec"] == base + 0x100 and st.pc == base + 4
    # ECALL traps: epc = pc, cause = ecall_m, mie -> mpie, mie = 0, pc = vector
    it.execute("ECALL", {}, st)
    assert st.csrs["mepc"] == base + 4
    assert st.csrs["mcause"] == pico32sys.trap.causes["ecall_m"]
    assert st.csrs["mstatus"] == 1 << 7               # mpie=1, mie=0
    assert st.pc == base + 0x100
    # CSRR_MIE reads a CSR field
    it.execute("CSRR_MIE", {"rd": 3, "rs1": 0}, st)
    assert st.regs["gpr"][3] == 0
    # MRET restores mie from mpie and returns to mepc
    it.execute("MRET", {}, st)
    assert st.pc == base + 4 and st.csrs["mstatus"] & (1 << 3)


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_run(pico32_asm, tmp_path):
    from typer.testing import CliRunner
    from isa_archive.cli import app
    image = tmp_path / "prog.bin"
    image.write_bytes(assemble(pico32_asm, LOOP_PROGRAM, tmp_path))
    r = CliRunner().invoke(app, ["run", str(image), "-i", str(PICO32), "--dump"])
    assert r.exit_code == 0, r.output
    assert "A" in r.output
    assert "r5  = 0x00000037" in r.output
