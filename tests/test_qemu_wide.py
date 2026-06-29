"""Tests for the QEMU target's >64-bit instruction path.

A wide ISA bypasses decodetree (which caps at 64 bits): the generator fetches the
word as a little-endian byte array and emits a hand-written decoder. <=64-bit ISAs
must keep using decodetree and stay byte-identical.
"""
import pathlib

from isa_archive.compiler.loader import load_isa, Registry
from isa_archive.generators.qemu import generate_qemu

EX = pathlib.Path(__file__).resolve().parent.parent / "examples"
WIDE = EX / "wide-probe/isa.yaml"          # 128-bit words, little-endian
WIDE_BE = EX / "wide-probe-be/isa.yaml"    # 128-bit words, big-endian
PICO32 = EX / "tutorial/pico32-part4/isa.yaml"  # 32-bit words (decodetree path)


def _gen(tmp_path, isa_yaml):
    reg = Registry()
    load_isa(str(isa_yaml), reg)
    generate_qemu(reg, str(tmp_path))
    (name,) = reg.isas
    return tmp_path / "target" / name


def test_qemu_wide_uses_handwritten_decoder(tmp_path):
    out = _gen(tmp_path, WIDE)
    # The hand-written decoder replaces decodetree's output; no .decode is emitted.
    assert (out / "decode-wide-probe.c.inc").exists()
    assert not (out / "wide-probe.decode").exists()


def test_qemu_wide_meson_skips_decodetree(tmp_path):
    meson = (_gen(tmp_path, WIDE) / "meson.build").read_text()
    assert "decodetree" not in meson
    # translate.c (which #includes the decoder) is still compiled.
    assert "wide-probe_translate.c" in meson


def test_qemu_wide_decoder_matches_fields_beyond_bit_64(tmp_path):
    dec = (_gen(tmp_path, WIDE) / "decode-wide-probe.c.inc").read_text()
    # The tag at bit 64 distinguishes WADD (1) from WSUB (2) - only a byte-array
    # decoder can reach it.
    assert "get_bits(insn, 64, 8) == 1ull" in dec
    assert "get_bits(insn, 64, 8) == 2ull" in dec
    # arg structs + dispatch the unchanged trans_* functions consume.
    assert "} arg_wadd;" in dec and "} arg_wsub;" in dec
    assert "return trans_wadd(ctx, &a);" in dec
    assert "return trans_wsub(ctx, &a);" in dec
    # signed immediate (bits 72..103) is sign-extended on extraction.
    assert "wsext(get_bits(insn, 72, 32), 32)" in dec


def test_qemu_wide_translate_fetches_byte_array(tmp_path):
    tr = (_gen(tmp_path, WIDE) / "wide-probe_translate.c").read_text()
    assert "uint8_t insn[16];" in tr
    assert "translator_ldub(env, &ctx->base, ctx->base.pc_next + _i)" in tr
    # Little-endian: byte _i goes straight to insn[_i] (the regression guard for BE).
    assert "insn[_i] = translator_ldub" in tr
    # No single-word load for a 128-bit ISA.
    assert "translator_ldq" not in tr and "translator_ldl" not in tr


def test_qemu_wide_big_endian_reverses_fetch(tmp_path):
    # On a big-endian guest the byte at the lowest address is most-significant, so
    # the fetch stores into insn[N-1-_i] to normalize to a little-endian byte array
    # (get_bits then stays byte-order-agnostic - same decoder body as little-endian).
    out = _gen(tmp_path, WIDE_BE)
    tr = (out / "wide-probe-be_translate.c").read_text()
    assert "insn[16 - 1 - _i] = translator_ldub" in tr
    assert "insn[_i] = translator_ldub" not in tr
    # Decoder is identical to the LE one (the tag-at-bit-64 dispatch is unchanged).
    dec = (out / "decode-wide-probe-be.c.inc").read_text()
    assert "get_bits(insn, 64, 8) == 2ull" in dec


def test_qemu_narrow_still_uses_decodetree(tmp_path):
    # Regression: a 32-bit ISA keeps the decodetree path unchanged.
    out = _gen(tmp_path, PICO32)
    assert (out / "pico32.decode").exists()
    assert not (out / "decode-pico32.c.inc").exists()  # produced by decodetree at build
    assert "decodetree.process" in (out / "meson.build").read_text()
    tr = (out / "pico32_translate.c").read_text()
    assert "translator_ldl" in tr and "uint8_t insn[" not in tr
