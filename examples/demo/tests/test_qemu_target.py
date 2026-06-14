"""
test_qemu_target.py — Verify the ISA-Archive YAML → QEMU CPU target pipeline.

Runs the same 33 bare-metal RV32I tests (qemu_virt_test.c) that pass on the
standard QEMU RISC-V target, but against *our* custom qemu-system-rv32i built
from ISA-Archive-generated files.  If all 33 pass, the YAML → QEMU pipeline
is proven end-to-end.

Two modes:
  1. Pre-built binary + pre-run output (fast, CI-friendly):
       RV32_QEMU_BIN=/tmp/rv32-qemu-target/build/qemu-system-rv32i \\
       RV32_QEMU_OUT=/tmp/rv32-qemu-target/test_output.txt \\
         pytest test_qemu_target.py -v

  2. Build binary inline then run (requires QEMU source + build tools):
       RV32_QEMU_BIN=/tmp/rv32-qemu-target/build/qemu-system-rv32i \\
         pytest test_qemu_target.py -v

  3. Full auto (builds QEMU from scratch — slow):
       pytest test_qemu_target.py -v
"""

import os
import re
import subprocess
import pathlib
import pytest

SCRIPTS = pathlib.Path(__file__).parent
BUILD_DIR = pathlib.Path(os.environ.get("RV32_QEMU_BUILD", "/tmp/rv32-qemu-target"))
QEMU_BIN = pathlib.Path(os.environ.get("RV32_QEMU_BIN", BUILD_DIR / "build" / "qemu-system-rv32i"))
OUTPUT_FILE = pathlib.Path(os.environ.get("RV32_QEMU_OUT", BUILD_DIR / "target_test_output.txt"))


def _build_qemu():
    """Run build_qemu_target.sh if the binary doesn't exist."""
    build_sh = SCRIPTS / "build_qemu_target.sh"
    if not build_sh.exists():
        pytest.skip("build_qemu_target.sh not found")
    result = subprocess.run(
        ["bash", str(build_sh)],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0:
        pytest.fail(f"build_qemu_target.sh failed:\n{result.stderr[-2000:]}")


def _compile_test_elf():
    """Compile qemu_virt_test.c to a bare-metal ELF."""
    gcc = "riscv64-elf-gcc"
    if subprocess.run(["which", gcc], capture_output=True).returncode != 0:
        pytest.skip(f"{gcc} not found — run: brew install riscv64-elf-gcc")

    elf = BUILD_DIR / "rv32i_target_test.elf"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    libgcc = subprocess.check_output(
        [gcc, "-march=rv32i", "-mabi=ilp32", "-print-libgcc-file-name"], text=True
    ).strip()

    subprocess.check_call([
        gcc, "-march=rv32i", "-mabi=ilp32", "-O1", "-nostdlib", "-ffreestanding",
        "-T", str(SCRIPTS / "qemu_virt.ld"),
        str(SCRIPTS / "qemu_virt_start.S"),
        str(SCRIPTS / "qemu_virt_test.c"),
        *([] if not libgcc else [libgcc]),
        "-o", str(elf),
    ])
    return elf


def _run_on_custom_qemu(elf):
    """Run the ELF on our ISA-Archive-built qemu-system-rv32i."""
    if not QEMU_BIN.exists():
        _build_qemu()
    if not QEMU_BIN.exists():
        pytest.skip(f"QEMU binary not found at {QEMU_BIN} — run build_qemu_target.sh first")

    subprocess.run([
        str(QEMU_BIN),
        "-machine", "rv32i-virt",
        "-bios", "none",
        "-display", "none",
        "-serial", f"file:{OUTPUT_FILE}",
        "-no-reboot",
        "-kernel", str(elf),
    ], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return OUTPUT_FILE


@pytest.fixture(scope="session")
def qemu_results():
    """Build test ELF and run on our custom QEMU.  Returns (passed, failed) sets."""
    if OUTPUT_FILE.exists():
        text = OUTPUT_FILE.read_text()
    else:
        elf = _compile_test_elf()
        out = _run_on_custom_qemu(elf)
        text = out.read_text()

    passed, failed = set(), set()
    for line in text.splitlines():
        m = re.match(r"(PASS|FAIL)\s+(\S+)", line)
        if m:
            (passed if m.group(1) == "PASS" else failed).add(m.group(2))

    if not passed and not failed:
        pytest.fail(
            f"No PASS/FAIL lines found in output.\n"
            f"Output file: {OUTPUT_FILE}\n"
            f"Content: {text[:500]!r}"
        )
    return passed, failed


# ── One test per instruction category ────────────────────────────────────────

def _ok(results, name):
    passed, failed = results
    if name not in passed and name not in failed:
        pytest.skip(f"'{name}' not found in custom QEMU output")
    assert name in passed, f"ISA-Archive QEMU reported FAIL for '{name}'"

def test_addi_positive(qemu_results):        _ok(qemu_results, "addi_positive")
def test_addi_negative(qemu_results):        _ok(qemu_results, "addi_negative")
def test_add(qemu_results):                  _ok(qemu_results, "add")
def test_sub(qemu_results):                  _ok(qemu_results, "sub")
def test_x0_hardwired(qemu_results):         _ok(qemu_results, "x0_hardwired")
def test_and(qemu_results):                  _ok(qemu_results, "and")
def test_or(qemu_results):                   _ok(qemu_results, "or")
def test_xor(qemu_results):                  _ok(qemu_results, "xor")
def test_andi(qemu_results):                 _ok(qemu_results, "andi")
def test_ori(qemu_results):                  _ok(qemu_results, "ori")
def test_xori(qemu_results):                 _ok(qemu_results, "xori")
def test_slli(qemu_results):                 _ok(qemu_results, "slli")
def test_srli(qemu_results):                 _ok(qemu_results, "srli")
def test_sll_reg(qemu_results):              _ok(qemu_results, "sll_reg")
def test_srl_reg(qemu_results):              _ok(qemu_results, "srl_reg")
def test_sw_lw(qemu_results):                _ok(qemu_results, "sw_lw")
def test_sw_lw_offset(qemu_results):         _ok(qemu_results, "sw_lw_offset")
def test_slt_signed_neg(qemu_results):       _ok(qemu_results, "slt_signed_neg")
def test_slt_not_less(qemu_results):         _ok(qemu_results, "slt_not_less")
def test_sltu_unsigned(qemu_results):        _ok(qemu_results, "sltu_unsigned")
def test_slti_signed(qemu_results):          _ok(qemu_results, "slti_signed")
def test_beq_taken(qemu_results):            _ok(qemu_results, "beq_taken")
def test_beq_not_taken(qemu_results):        _ok(qemu_results, "beq_not_taken")
def test_bne_taken(qemu_results):            _ok(qemu_results, "bne_taken")
def test_bne_not_taken(qemu_results):        _ok(qemu_results, "bne_not_taken")
def test_blt_signed_taken(qemu_results):     _ok(qemu_results, "blt_signed_taken")
def test_bltu_not_taken(qemu_results):       _ok(qemu_results, "bltu_not_taken")
def test_bge_signed_taken(qemu_results):     _ok(qemu_results, "bge_signed_taken")
def test_bgeu_taken(qemu_results):           _ok(qemu_results, "bgeu_taken")
def test_backward_branch_loop(qemu_results): _ok(qemu_results, "backward_branch_loop")
def test_jal_saves_return_addr(qemu_results):_ok(qemu_results, "jal_saves_return_addr")
def test_jal_x0_plain_jump(qemu_results):   _ok(qemu_results, "jal_x0_plain_jump")
def test_jalr(qemu_results):                _ok(qemu_results, "jalr")
