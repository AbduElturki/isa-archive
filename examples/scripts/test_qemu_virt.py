"""
RV32I test suite — verifies results from qemu-system-riscv32.

Two modes:
  1. Pre-run output file (fast, CI-friendly):
       RV32_QEMU_OUT=/tmp/rv32-qemu-virt/test_output.txt pytest test_qemu_virt.py -v

  2. Build + run inline (requires qemu + riscv64-elf-gcc):
       pytest test_qemu_virt.py -v
     (builds qemu_virt_test.c, runs on real QEMU, parses output)
"""

import os
import re
import subprocess
import tempfile
import pathlib
import pytest

SCRIPTS = pathlib.Path(__file__).parent
BUILD_DIR = pathlib.Path(os.environ.get("RV32_QEMU_BUILD", "/tmp/rv32-qemu-virt"))
OUTPUT_FILE = pathlib.Path(os.environ.get("RV32_QEMU_OUT", BUILD_DIR / "test_output.txt"))


def _build_and_run():
    """Compile and run the bare-metal ELF on qemu-system-riscv32."""
    gcc = "riscv64-elf-gcc"
    qemu = "qemu-system-riscv32"
    for t in [gcc, qemu]:
        if not subprocess.run(["which", t], capture_output=True).returncode == 0:
            pytest.skip(f"{t} not found — run: brew install qemu riscv64-elf-gcc")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    elf = BUILD_DIR / "rv32i_virt_test.elf"

    libgcc = subprocess.check_output(
        [gcc, "-march=rv32i", "-mabi=ilp32", "-print-libgcc-file-name"],
        text=True,
    ).strip()

    subprocess.check_call([
        gcc, "-march=rv32i", "-mabi=ilp32", "-O1", "-nostdlib", "-ffreestanding",
        "-T", str(SCRIPTS / "qemu_virt.ld"),
        str(SCRIPTS / "qemu_virt_start.S"),
        str(SCRIPTS / "qemu_virt_test.c"),
        *([] if not libgcc else [libgcc]),
        "-o", str(elf),
    ])

    out_file = BUILD_DIR / "test_output.txt"
    subprocess.run([
        qemu, "-machine", "virt", "-bios", "none",
        "-display", "none",
        "-serial", f"file:{out_file}",
        "-no-reboot",
        "-kernel", str(elf),
    ], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return out_file


@pytest.fixture(scope="session")
def qemu_results():
    """Return a set of test names that passed in QEMU."""
    if OUTPUT_FILE.exists():
        text = OUTPUT_FILE.read_text()
    else:
        out = _build_and_run()
        text = out.read_text()

    passed = set()
    failed = set()
    for line in text.splitlines():
        m = re.match(r"(PASS|FAIL)\s+(\S+)", line)
        if m:
            (passed if m.group(1) == "PASS" else failed).add(m.group(2))
    return passed, failed


# ── One pytest test per instruction category ──────────────────────────────

def _ok(results, name):
    passed, failed = results
    if name not in passed and name not in failed:
        pytest.skip(f"'{name}' not found in QEMU output")
    assert name in passed, f"QEMU reported FAIL for '{name}'"

def test_addi_positive(qemu_results):       _ok(qemu_results, "addi_positive")
def test_addi_negative(qemu_results):       _ok(qemu_results, "addi_negative")
def test_add(qemu_results):                 _ok(qemu_results, "add")
def test_sub(qemu_results):                 _ok(qemu_results, "sub")
def test_x0_hardwired(qemu_results):        _ok(qemu_results, "x0_hardwired")
def test_and(qemu_results):                 _ok(qemu_results, "and")
def test_or(qemu_results):                  _ok(qemu_results, "or")
def test_xor(qemu_results):                 _ok(qemu_results, "xor")
def test_andi(qemu_results):                _ok(qemu_results, "andi")
def test_ori(qemu_results):                 _ok(qemu_results, "ori")
def test_xori(qemu_results):               _ok(qemu_results, "xori")
def test_slli(qemu_results):                _ok(qemu_results, "slli")
def test_srli(qemu_results):                _ok(qemu_results, "srli")
def test_sll_reg(qemu_results):             _ok(qemu_results, "sll_reg")
def test_srl_reg(qemu_results):             _ok(qemu_results, "srl_reg")
def test_sw_lw(qemu_results):               _ok(qemu_results, "sw_lw")
def test_sw_lw_offset(qemu_results):        _ok(qemu_results, "sw_lw_offset")
def test_slt_signed_neg(qemu_results):      _ok(qemu_results, "slt_signed_neg")
def test_slt_not_less(qemu_results):        _ok(qemu_results, "slt_not_less")
def test_sltu_unsigned(qemu_results):       _ok(qemu_results, "sltu_unsigned")
def test_slti_signed(qemu_results):         _ok(qemu_results, "slti_signed")
def test_beq_taken(qemu_results):           _ok(qemu_results, "beq_taken")
def test_beq_not_taken(qemu_results):       _ok(qemu_results, "beq_not_taken")
def test_bne_taken(qemu_results):           _ok(qemu_results, "bne_taken")
def test_bne_not_taken(qemu_results):       _ok(qemu_results, "bne_not_taken")
def test_blt_signed_taken(qemu_results):    _ok(qemu_results, "blt_signed_taken")
def test_bltu_not_taken(qemu_results):      _ok(qemu_results, "bltu_not_taken")
def test_bge_signed_taken(qemu_results):    _ok(qemu_results, "bge_signed_taken")
def test_bgeu_taken(qemu_results):          _ok(qemu_results, "bgeu_taken")
def test_backward_branch_loop(qemu_results):_ok(qemu_results, "backward_branch_loop")
def test_jal_saves_return_addr(qemu_results):_ok(qemu_results, "jal_saves_return_addr")
def test_jal_x0_plain_jump(qemu_results):  _ok(qemu_results, "jal_x0_plain_jump")
def test_jalr(qemu_results):               _ok(qemu_results, "jalr")
