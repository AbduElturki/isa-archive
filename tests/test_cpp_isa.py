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
    return tmp_path / "Pico32"


def _gen_wide(tmp_path):
    reg = Registry()
    load_isa(str(WIDE), reg)
    generate_cpp_isa(reg, str(tmp_path))
    return tmp_path / "WideProbe"


def test_cpp_isa_creates_headers(tmp_path):
    out = _gen(tmp_path)
    for f in ("Pico32Enums.h", "Pico32InstrInfo.h", "Pico32Decoder.h",
              "Pico32Encoder.h", "Pico32.h", "example_main.cpp", "INTEGRATE.md"):
        assert (out / f).exists(), f
    assert (tmp_path / ".clang-format").exists()


def test_cpp_isa_has_expected_symbols(tmp_path):
    out = _gen(tmp_path)
    enums = (out / "Pico32Enums.h").read_text()
    info = (out / "Pico32InstrInfo.h").read_text()
    decode = (out / "Pico32Decoder.h").read_text()
    assert "enum class Op" in enums and "    ADD," in enums
    assert "const char *mnemonic(Op" in enums and 'case Op::ADD: return "add";' in enums
    assert "enum class RegClass" in enums and "    gpr," in enums
    assert "struct InstrInfo" in info and "const InstrInfo &info(Op" in info
    assert "Op decode(uint64_t word)" in decode
    assert "decode_imm(Op op, uint64_t word)" in decode


def test_cpp_isa_decode_table_matches_encoding(tmp_path):
    # ADD is RISC-V R-type: mask 0xfe00707f, match 0x33. DO NOT regress.
    info = (_gen(tmp_path) / "Pico32InstrInfo.h").read_text()
    assert '{ Op::ADD, "add",' in info
    assert "0xfe00707fULL, 0x33ULL" in info


def test_cpp_isa_latency_from_uarch(tmp_path):
    info = (_gen(tmp_path, with_uarch=True) / "Pico32InstrInfo.h").read_text()
    # LW is mem_load → LoadStoreUnit latency 2; ADD is alu_int → IntegerALU latency 1.
    assert '{ Op::LW, "lw",' in info and ', 2, LW_operands' in info
    assert '{ Op::ADD, "add",' in info and ", 1, ADD_operands" in info


def test_cpp_isa_default_latency_without_uarch(tmp_path):
    info = (_gen(tmp_path) / "Pico32InstrInfo.h").read_text()
    assert '{ Op::LW, "lw",' in info and ", 1, LW_operands" in info


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_compiles_standalone_cpp17(tmp_path):
    out = _gen(tmp_path)
    tu = tmp_path / "tu.cpp"
    tu.write_text(f'#include "{out / "Pico32.h"}"\nint main(){{return 0;}}\n')
    r = subprocess.run([CXX, "-std=c++17", "-fsyntax-only", str(tu)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_decode_is_correct(tmp_path):
    # 0x33 = `add x0, x0, x0` in the RISC-V-compatible pico32 encoding.
    out = _gen(tmp_path)
    drv = tmp_path / "drv.cpp"
    drv.write_text(
        f'#include "{out / "Pico32.h"}"\n'
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
    decode = (out / "WideProbeDecoder.h").read_text()
    info = (out / "WideProbeInstrInfo.h").read_text()
    # Word is a 16-byte array, get_bits reads it, decode takes the array.
    assert "using Word = std::array<uint8_t, 16>;" in decode
    assert "get_bits(const Word &word" in decode
    assert "Op decode(const Word &word)" in decode
    # The 64-bit mask/match fields are dropped for wide ISAs.
    assert "uint64_t mask;" not in info and "uint64_t match;" not in info


def test_cpp_isa_wide_decodes_fields_beyond_bit_64(tmp_path):
    # The tag that distinguishes WADD/WSUB lives at bit 64 — only a wide decode
    # path can reach it. Confirm both fixed-field checks are emitted.
    decode = (_gen_wide(tmp_path) / "WideProbeDecoder.h").read_text()
    assert "get_bits(word, 64, 8) == 1ull" in decode  # TAG.ADD → WADD
    assert "get_bits(word, 64, 8) == 2ull" in decode  # TAG.SUB → WSUB


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_wide_decode_is_correct(tmp_path):
    out = _gen_wide(tmp_path)
    drv = tmp_path / "wdrv.cpp"
    # Build two 128-bit words by hand: opcode=1 at bit 0, tag at bit 64,
    # imm at bit 72. WADD has tag 1, WSUB has tag 2.
    drv.write_text(
        f'#include "{out / "WideProbe.h"}"\n'
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


# ── Encoder ↔ decoder round-trip (both generated from the same YAML) ─────────────

def _run_roundtrip(tmp_path, header, namespace, body):
    drv = tmp_path / "rt.cpp"
    drv.write_text(f'#include "{header}"\nint main() {{ using namespace {namespace};\n'
                   f"{body}\n  return 0; }}\n")
    exe = tmp_path / "rt"
    c = subprocess.run([CXX, "-std=c++17", "-Wall", str(drv), "-o", str(exe)],
                       capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    assert subprocess.run([str(exe)]).returncode == 0


def test_cpp_isa_encoder_has_per_instruction_functions(tmp_path):
    enc = (_gen(tmp_path) / "Pico32Encoder.h").read_text()
    # Register-only ALU op, single-immediate load, and a split-immediate branch.
    assert "encode_ADD(unsigned rd, unsigned rs1, unsigned rs2)" in enc
    assert "encode_LW(unsigned rd, unsigned rs1, int64_t imm)" in enc
    assert "encode_BEQ(unsigned rs1, unsigned rs2, int64_t imm)" in enc
    assert "uint64_t w = 0x33ULL;" in enc  # ADD's fixed bits (opcode/funct)


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_encode_decode_roundtrip(tmp_path):
    # Encode with the generated encoder, decode with the generated decoder — the two
    # must agree because both derive from the same manifest field layout.
    out = _gen(tmp_path)
    _run_roundtrip(
        tmp_path, out / "Pico32.h", "pico32",
        "  uint64_t a = encode_ADD(1, 2, 3);\n"
        "  if (decode(a) != Op::ADD) return 1;\n"
        "  const InstrInfo &i = info(Op::ADD);\n"
        "  if (get_bits(a, i.operands[0].start, i.operands[0].width) != 1) return 2;\n"
        "  if (get_bits(a, i.operands[2].start, i.operands[2].width) != 3) return 3;\n"
        "  uint64_t l = encode_LW(5, 6, -4);\n"
        "  if (decode(l) != Op::LW || decode_imm(Op::LW, l) != -4) return 4;\n"
        "  uint64_t b = encode_BEQ(1, 2, 8);   // split B-type immediate\n"
        "  if (decode(b) != Op::BEQ || decode_imm(Op::BEQ, b) != 8) return 5;\n"
    )


@pytest.mark.skipif(CXX is None, reason="no C++ compiler")
def test_cpp_isa_wide_encode_decode_roundtrip(tmp_path):
    # Same round-trip for a 128-bit word — the encoder's set_bits and the decoder's
    # get_bits both reach the tag/immediate fields beyond bit 64.
    out = _gen_wide(tmp_path)
    _run_roundtrip(
        tmp_path, out / "WideProbe.h", "wide_probe",
        "  Word a = encode_WADD(1, 2, 5);\n"
        "  if (decode(a) != Op::WADD || decode_imm(Op::WADD, a) != 5) return 1;\n"
        "  Word s = encode_WSUB(3, 4, -7);   // negative imm, fields past bit 64\n"
        "  if (decode(s) != Op::WSUB || decode_imm(Op::WSUB, s) != -7) return 2;\n"
    )
