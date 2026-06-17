"""Tests for the `asm` target — the generated Python assembler.

Focus: instruction-word widths. 4-byte ISAs must keep emitting `struct.pack`
(byte-identical), while wider ISAs use a width-agnostic `int.to_bytes`.
"""
import subprocess
import sys
import pathlib

from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.assembler import generate_asm

EX = pathlib.Path(__file__).resolve().parent.parent / "examples"
PICO32 = EX / "tutorial/pico32-part4/isa.yaml"
WIDE = EX / "wide-probe/isa.yaml"  # 128-bit words: exercises the >64-bit encode path


def _gen(tmp_path, isa_yaml):
    reg = Registry()
    load_isa(str(isa_yaml), reg)
    generate_asm(reg, str(tmp_path))
    (name,) = reg.isas
    return tmp_path / f"{name}_asm.py"


def test_asm_narrow_packs_four_bytes(tmp_path):
    # 32-bit pico32 stays on the original 4-byte struct.pack path. DO NOT regress.
    src = _gen(tmp_path, PICO32).read_text()
    assert 'return struct.pack("<I", word)' in src
    assert "word.to_bytes(" not in src  # the instruction-word path stays on struct.pack


def test_asm_wide_uses_width_agnostic_to_bytes(tmp_path):
    # 128-bit words can't fit a uint32; the encoder serialises the full word.
    src = _gen(tmp_path, WIDE).read_text()
    assert 'word.to_bytes(16, "little")' in src
    assert 'struct.pack("<I"' not in src


def test_asm_wide_round_trip(tmp_path):
    asm = _gen(tmp_path, WIDE)
    src = tmp_path / "in.s"
    src.write_text("wadd r1, r2, 5\nwsub r3, r4, 7\n")
    out = tmp_path / "out.bin"
    r = subprocess.run([sys.executable, str(asm), str(src), "-o", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = out.read_bytes()
    assert len(data) == 32, "two 16-byte words"

    def gb(word, start, width):
        v = 0
        for i in range(width):
            b = start + i
            v |= ((word[b >> 3] >> (b & 7)) & 1) << i
        return v

    w0, w1 = data[:16], data[16:]
    # opcode at bit 0; the tag that distinguishes the ops lives at bit 64;
    # the immediate at bit 72 — both beyond a 64-bit word.
    assert gb(w0, 0, 8) == 1 and gb(w0, 64, 8) == 1 and gb(w0, 72, 32) == 5  # wadd
    assert gb(w1, 0, 8) == 1 and gb(w1, 64, 8) == 2 and gb(w1, 72, 32) == 7  # wsub
