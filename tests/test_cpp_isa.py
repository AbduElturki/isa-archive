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
CXX = shutil.which("c++") or shutil.which("clang++") or shutil.which("g++")


def _gen(tmp_path, with_uarch=False):
    reg = Registry()
    load_isa(str(PICO32), reg)
    if with_uarch:
        load_uarch(str(PICO32_UARCH), reg)
    generate_cpp_isa(reg, str(tmp_path))
    return tmp_path / "pico32"


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
