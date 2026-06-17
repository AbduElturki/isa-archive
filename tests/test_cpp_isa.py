"""Tests for the `cpp-isa` target — descriptive C++ ISA headers."""
import shutil
import subprocess
import pathlib

import pytest

from isa_archive.compiler.loader import load_isa, load_uarch, Registry
from isa_archive.generators.cpp_isa import generate_cpp_isa

EX = pathlib.Path(__file__).resolve().parent.parent / "examples"
PICO32 = EX / "tutorial/pico32-part4/isa.yaml"
PICO32_UARCH = EX / "tutorial/pico32-part4/uarch.yaml"
WIDE = EX / "wide-probe/isa.yaml"  # 128-bit words: exercises the >64-bit decode path
CXX = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")


def _gen(tmp_path, with_uarch=False):
    reg = Registry()
    load_isa(str(PICO32), reg)
    if with_uarch:
        load_uarch(str(PICO32_UARCH), reg)
    generate_cpp_isa(reg, str(tmp_path))
    return tmp_path / "pico32"


def _gen_wide(tmp_path):
    reg = Registry()
    load_isa(str(WIDE), reg)
    generate_cpp_isa(reg, str(tmp_path))
    return tmp_path / "wide_probe"


def test_cpp_isa_creates_headers(tmp_path):
    out = _gen(tmp_path)
    for f in ("pico32_enums.h", "pico32_info.h", "pico32_decode.h",
              "pico32_model.h", "example_main.cpp", "INTEGRATE.md"):
        assert (out / f).exists(), f
    assert (tmp_path / ".clang-format").exists()


def test_cpp_isa_has_expected_symbols(tmp_path):
    out = _gen(tmp_path)
    enums = (out / "pico32_enums.h").read_text()
    info = (out / "pico32_info.h").read_text()
    decode = (out / "pico32_decode.h").read_text()
    assert "enum class Op" in enums and "    ADD," in enums
    assert "const char *mnemonic(Op" in enums and 'case Op::ADD: return "add";' in enums
    assert "enum class RegClass" in enums and "    gpr," in enums
    assert "struct InstrInfo" in info and "const InstrInfo &info(Op" in info
    assert "Op decode(uint64_t word)" in decode
    assert "decode_imm(Op op, uint64_t word)" in decode


def test_cpp_isa_decode_table_matches_encoding(tmp_path):
    # ADD is RISC-V R-type: mask 0xfe00707f, match 0x33. DO NOT regress.
    info = (_gen(tmp_path) / "pico32_info.h").read_text()
    assert '{ Op::ADD, "add",' in info
    assert "0xfe00707fULL, 0x33ULL" in info


def test_cpp_isa_latency_from_uarch(tmp_path):
    info = (_gen(tmp_path, with_uarch=True) / "pico32_info.h").read_text()
    # LW is mem_load → LoadStoreUnit latency 2; ADD is alu_int → IntegerALU latency 1.
    assert '{ Op::LW, "lw",' in info and ', 2, LW_operands' in info
    assert '{ Op::ADD, "add",' in info and ", 1, ADD_operands" in info


def test_cpp_isa_default_latency_without_uarch(tmp_path):
    info = (_gen(tmp_path) / "pico32_info.h").read_text()
    assert '{ Op::LW, "lw",' in info and ", 1, LW_operands" in info


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_compiles_standalone_cpp17(tmp_path):
    out = _gen(tmp_path)
    tu = tmp_path / "tu.cpp"
    tu.write_text(f'#include "{out / "pico32_model.h"}"\nint main(){{return 0;}}\n')
    r = subprocess.run([CXX, "-std=c++17", "-fsyntax-only", str(tu)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_decode_is_correct(tmp_path):
    # 0x33 = `add x0, x0, x0` in the RISC-V-compatible pico32 encoding.
    out = _gen(tmp_path)
    drv = tmp_path / "drv.cpp"
    drv.write_text(
        f'#include "{out / "pico32_model.h"}"\n'
        "int main(){ using namespace pico32; "
        "return decode(0x33) == Op::ADD ? 0 : 1; }\n"
    )
    exe = tmp_path / "drv"
    c = subprocess.run([CXX, "-std=c++17", str(drv), "-o", str(exe)],
                       capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    assert subprocess.run([str(exe)]).returncode == 0


# ── Wide (>64-bit) instruction words ────────────────────────────────────────────

def test_cpp_isa_wide_uses_byte_array_word(tmp_path):
    out = _gen_wide(tmp_path)
    decode = (out / "wide_probe_decode.h").read_text()
    info = (out / "wide_probe_info.h").read_text()
    # Word is a 16-byte array, get_bits reads it, decode takes the array.
    assert "using Word = std::array<uint8_t, 16>;" in decode
    assert "get_bits(const Word &word" in decode
    assert "Op decode(const Word &word)" in decode
    # The 64-bit mask/match fields are dropped for wide ISAs.
    assert "uint64_t mask;" not in info and "uint64_t match;" not in info


def test_cpp_isa_wide_decodes_fields_beyond_bit_64(tmp_path):
    # The tag that distinguishes WADD/WSUB lives at bit 64 — only a wide decode
    # path can reach it. Confirm both fixed-field checks are emitted.
    decode = (_gen_wide(tmp_path) / "wide_probe_decode.h").read_text()
    assert "get_bits(word, 64, 8) == 1ull" in decode  # TAG.ADD → WADD
    assert "get_bits(word, 64, 8) == 2ull" in decode  # TAG.SUB → WSUB


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_wide_decode_is_correct(tmp_path):
    out = _gen_wide(tmp_path)
    drv = tmp_path / "wdrv.cpp"
    # Build two 128-bit words by hand: opcode=1 at bit 0, tag at bit 64,
    # imm at bit 72. WADD has tag 1, WSUB has tag 2.
    drv.write_text(
        f'#include "{out / "wide_probe_model.h"}"\n'
        "int main() { using namespace wide_probe;\n"
        "  Word add{}; add[0] = 1; add[8] = 1; add[9] = 5;\n"   # opcode, tag=ADD, imm low byte
        "  Word sub{}; sub[0] = 1; sub[8] = 2; sub[9] = 7;\n"   # opcode, tag=SUB, imm low byte
        "  if (decode(add) != Op::WADD) return 1;\n"
        "  if (decode(sub) != Op::WSUB) return 2;\n"
        "  if (decode_imm(Op::WADD, add) != 5) return 3;\n"
        "  if (decode_imm(Op::WSUB, sub) != 7) return 4;\n"
        "  return 0; }\n"
    )
    exe = tmp_path / "wdrv"
    c = subprocess.run([CXX, "-std=c++17", str(drv), "-o", str(exe)],
                       capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    assert subprocess.run([str(exe)]).returncode == 0
