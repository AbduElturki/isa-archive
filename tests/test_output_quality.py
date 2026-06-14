"""Guardrails on the *quality* of generated QEMU / LLVM output.

These lock in the adoption-quality pass: every generated file is whitespace-clean,
the trees ship a clang-format config, and the QEMU/LLVM artifacts carry the
provenance comments and readable shapes a human maintainer relies on.
"""
import re
import shutil
import subprocess
import pathlib

import pytest

from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.qemu import generate_qemu
from isa_archive.generators.llvm import generate_llvm

PICO32 = (pathlib.Path(__file__).resolve().parent.parent
          / "examples/tutorial/pico32-part4/isa.yaml")
_C_LIKE = {".c", ".h", ".cpp", ".cc", ".inc"}


def _load():
    reg = Registry()
    top = load_isa(str(PICO32), reg)
    reg.isas = {top.name: top}
    return reg


# ── whitespace normalizer invariants (all generators) ────────────────────────

@pytest.mark.parametrize("gen", ["qemu", "llvm"])
def test_generated_files_are_whitespace_clean(tmp_path, gen):
    out = tmp_path / gen
    (generate_qemu if gen == "qemu" else generate_llvm)(_load(), str(out))
    offenders = []
    for f in out.rglob("*"):
        if not f.is_file():
            continue
        text = f.read_text()
        if any(line != line.rstrip() for line in text.split("\n")):
            offenders.append(f"{f.name}: trailing whitespace")
        if re.search(r"\n{4,}", text):
            offenders.append(f"{f.name}: >2 consecutive blank lines")
        if text and not text.endswith("\n"):
            offenders.append(f"{f.name}: missing trailing newline")
        if text.endswith("\n\n"):
            offenders.append(f"{f.name}: multiple trailing newlines")
    assert not offenders, offenders


# ── shipped clang-format config ──────────────────────────────────────────────

def test_qemu_ships_clang_format(tmp_path):
    generate_qemu(_load(), str(tmp_path))
    cfg = (tmp_path / ".clang-format").read_text()
    assert "BasedOnStyle: LLVM" in cfg
    assert "SortIncludes: false" in cfg   # QEMU needs osdep.h to stay first


def test_llvm_ships_clang_format(tmp_path):
    generate_llvm(_load(), str(tmp_path))
    cfg = (tmp_path / "llvm/lib/Target/PICO32/.clang-format").read_text()
    assert "BasedOnStyle: LLVM" in cfg


# ── QEMU provenance + readable expressions ───────────────────────────────────

def test_qemu_trans_has_provenance_and_readable_sext(tmp_path):
    generate_qemu(_load(), str(tmp_path))
    t = tmp_path / "target/pico32"
    trans = (t / "pico32_trans.c.inc").read_text()
    helpers = (t / "pico32_helpers.c").read_text()
    # Every trans function is preceded by a provenance comment.
    assert trans.count("static bool trans_") == trans.count("Lowered ")
    assert "DO NOT EDIT" in trans
    # Sign-extension reads as isa_sextN(...), not a nested double-cast.
    assert "isa_sext32(" in helpers
    assert "(int32_t)((uint32_t)" not in helpers
    # Conditional include: pico32 has loads/stores, so cpu_ldst.h is pulled in.
    assert "exec/cpu_ldst.h" in helpers


# ── LLVM provenance + warning-free shapes ────────────────────────────────────

def test_llvm_instr_defs_have_provenance(tmp_path):
    generate_llvm(_load(), str(tmp_path))
    t = tmp_path / "llvm/lib/Target/PICO32"
    td = (t / "PICO32InstrInfo.td").read_text()
    assert "//   from:" in td and "//   category:" in td
    # Reserved registers self-document their ABI alias.
    reginfo = (t / "PICO32RegisterInfo.cpp").read_text()
    assert re.search(r"Reserved\.set\(PICO32::\w+\);\s*//\s*\w+", reginfo)
    # The dead StackPtr that tripped -Wunused-variable is gone.
    assert "StackPtr" not in (t / "PICO32ISelLowering.cpp").read_text()


# ── opt-in clang-format leaves the tree clang-format clean ───────────────────

@pytest.mark.skipif(shutil.which("clang-format") is None,
                    reason="clang-format not installed")
def test_format_flag_produces_clang_format_clean_cpp(tmp_path):
    generate_llvm(_load(), str(tmp_path), clang_format=True)
    target = tmp_path / "llvm/lib/Target/PICO32"
    cpp = [f for f in target.rglob("*")
           if f.suffix.lower() in _C_LIKE]
    assert cpp
    dirty = []
    for f in cpp:
        r = subprocess.run(["clang-format", "--dry-run", "-Werror",
                            "-style=file", f"-assume-filename={f}", str(f)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            dirty.append(f.name)
    assert not dirty, f"clang-format found violations after --format: {dirty}"
