"""Regression tests for the loader/validation hardening pass:

- `extends:` - extension definitions override the base's; cycles are rejected
- fixed-field (opcode/constant) values must fit their schema field
- register aliases / bare CSR names in behaviors are rejected with targeted errors
- width mismatches are still caught at load time (backend-agnostic validate_ir)
- uArch files with extra documents warn instead of crashing; missing/unknown
  uArch targets fail with clear errors
- load_directory recognizes an ISA doc anywhere in a file, not just first
- `kind: ScalarType` definitions do not leak between ISAs in one process
- chained comparisons keep Python semantics in constraints and behaviors
- augmented assignments (`x += v`) go through the full assignment path
  (PC mask / branch tracking / zero-register guard)
- duplicate definitions of the same name warn instead of silently replacing
"""
import logging
import textwrap

import pytest

from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.backends import QemuCBackend
from isa_archive.compiler.loader import (Registry, load_directory, load_isa,
                                         load_uarch)
from isa_archive.compiler.utils import constraint_to_c


BASE_ISA = textwrap.dedent("""\
    apiVersion: isa-archive/v1
    kind: ISA
    metadata: {name: base-isa}
    spec:
      version: "1.0"
      xlen: 32
      state:
        registers:
          - {name: gpr, width: 32, count: 32, zero_register: 0,
             aliases: {zero: 0, ra: 1, sp: 2}}
    ---
    apiVersion: isa-archive/v1
    kind: Schema
    metadata: {name: RType}
    spec:
      length: 32
      fields:
        - {name: opcode, start: 0, width: 7, role: opcode}
        - {name: rd, start: 7, width: 5, role: register, type: gpr}
        - {name: rs1, start: 15, width: 5, role: register, type: gpr}
        - {name: rs2, start: 20, width: 5, role: register, type: gpr}
    ---
    apiVersion: isa-archive/v1
    kind: Instruction
    metadata: {name: ADD}
    spec:
      schema: RType
      opcode: 0x33
      behavior: "rd = rs1 + rs2"
    """)


def _write(path, text):
    path.write_text(textwrap.dedent(text))
    return path


# ── extends: override direction and cycles ──────────────────────────────────

def test_extension_overrides_base_definitions(tmp_path):
    _write(tmp_path / "base.yaml", BASE_ISA)
    _write(tmp_path / "ext.yaml", """\
        apiVersion: isa-archive/v1
        kind: ISA
        metadata: {name: ext-isa}
        spec:
          version: "1.0"
          extends: base.yaml
        ---
        apiVersion: isa-archive/v1
        kind: Instruction
        metadata: {name: ADD}
        spec:
          schema: RType
          opcode: 0x3B
          behavior: "rd = rs1 - rs2"
        """)
    reg = load_isa(str(tmp_path / "ext.yaml"))
    assert reg.instructions["ADD"].spec.opcode == 0x3B
    assert reg.instructions["ADD"].spec.behavior == "rd = rs1 - rs2"
    # base-only content is still inherited
    assert "RType" in reg.schemas
    assert reg.registers and reg.registers[0].name == "gpr"


def test_circular_extends_raises(tmp_path):
    _write(tmp_path / "a.yaml", """\
        apiVersion: isa-archive/v1
        kind: ISA
        metadata: {name: a-isa}
        spec: {version: "1.0", extends: b.yaml}
        """)
    _write(tmp_path / "b.yaml", """\
        apiVersion: isa-archive/v1
        kind: ISA
        metadata: {name: b-isa}
        spec: {version: "1.0", extends: a.yaml}
        """)
    with pytest.raises(ValueError, match="Circular extends"):
        load_isa(str(tmp_path / "a.yaml"))


def test_self_extends_raises(tmp_path):
    _write(tmp_path / "a.yaml", """\
        apiVersion: isa-archive/v1
        kind: ISA
        metadata: {name: a-isa}
        spec: {version: "1.0", extends: a.yaml}
        """)
    with pytest.raises(ValueError, match="Circular extends"):
        load_isa(str(tmp_path / "a.yaml"))


# ── fixed-field values must fit their field ─────────────────────────────────

def test_opcode_wider_than_field_rejected(tmp_path):
    _write(tmp_path / "isa.yaml", BASE_ISA.replace("opcode: 0x33", "opcode: 0x1FF"))
    with pytest.raises(ValueError, match="does not fit"):
        load_isa(str(tmp_path / "isa.yaml"))


def test_negative_fixed_value_rejected(tmp_path):
    _write(tmp_path / "isa.yaml", BASE_ISA.replace("opcode: 0x33", "opcode: -1"))
    with pytest.raises(ValueError, match="does not fit"):
        load_isa(str(tmp_path / "isa.yaml"))


# ── behavior variable hygiene ────────────────────────────────────────────────

def test_register_alias_in_behavior_rejected(tmp_path):
    _write(tmp_path / "isa.yaml",
           BASE_ISA.replace('behavior: "rd = rs1 + rs2"',
                            'behavior: "rd = sp + rs1"'))
    with pytest.raises(ValueError, match="register alias 'sp'"):
        load_isa(str(tmp_path / "isa.yaml"))


def test_bare_csr_name_in_behavior_rejected(tmp_path):
    isa = BASE_ISA.replace(
        "    registers:",
        "    csrs:\n"
        "      - {name: mstatus, address: 0x300, width: 32}\n"
        "    registers:",
    ).replace('behavior: "rd = rs1 + rs2"', 'behavior: "rd = mstatus"')
    assert "mstatus" in isa
    _write(tmp_path / "isa.yaml", isa)
    with pytest.raises(ValueError, match="use 'csr.mstatus'"):
        load_isa(str(tmp_path / "isa.yaml"))


def test_width_mismatch_still_rejected_at_load(tmp_path):
    isa = BASE_ISA.replace(
        "- {name: rs2, start: 20, width: 5, role: register, type: gpr}",
        "- {name: imm, start: 20, width: 12, role: immediate, type: signed}",
    ).replace('behavior: "rd = rs1 + rs2"', 'behavior: "rd = imm"')
    _write(tmp_path / "isa.yaml", isa)
    with pytest.raises(ValueError, match="Width mismatch"):
        load_isa(str(tmp_path / "isa.yaml"))


# ── uArch loading robustness ─────────────────────────────────────────────────

def test_uarch_with_extra_documents_warns_instead_of_crashing(tmp_path, caplog):
    _write(tmp_path / "isa.yaml", BASE_ISA)
    _write(tmp_path / "uarch.yaml", """\
        apiVersion: isa-archive/v1
        kind: uArch
        metadata: {name: u1}
        spec:
          isa: base-isa
          blocks: []
        ---
        apiVersion: isa-archive/v1
        kind: Constant
        metadata: {name: STRAY}
        spec: {value: 1, width: 8}
        """)
    registry = Registry()
    load_isa(str(tmp_path / "isa.yaml"), registry)
    with caplog.at_level(logging.WARNING):
        uarch = load_uarch(str(tmp_path / "uarch.yaml"), registry)
    assert uarch.name == "u1"
    assert any("ignoring document kind 'Constant'" in r.message for r in caplog.records)


def test_uarch_file_without_uarch_doc_raises(tmp_path):
    _write(tmp_path / "isa.yaml", BASE_ISA)
    _write(tmp_path / "uarch.yaml", """\
        apiVersion: isa-archive/v1
        kind: Constant
        metadata: {name: STRAY}
        spec: {value: 1, width: 8}
        """)
    registry = Registry()
    load_isa(str(tmp_path / "isa.yaml"), registry)
    with pytest.raises(ValueError, match="No uArch manifest"):
        load_uarch(str(tmp_path / "uarch.yaml"), registry)


def test_uarch_targeting_unknown_isa_raises(tmp_path):
    _write(tmp_path / "uarch.yaml", """\
        apiVersion: isa-archive/v1
        kind: uArch
        metadata: {name: u1}
        spec: {isa: no-such-isa, blocks: []}
        """)
    with pytest.raises(ValueError, match="not loaded"):
        load_uarch(str(tmp_path / "uarch.yaml"), Registry())


# ── load_directory doc scanning ──────────────────────────────────────────────

def test_load_directory_finds_isa_doc_that_is_not_first(tmp_path):
    docs = BASE_ISA.split("---\n")
    # Put the Schema doc first, the ISA doc second, instruction last.
    reordered = "---\n".join([docs[1], docs[0], docs[2]])
    _write(tmp_path / "cpu.yaml", reordered)
    registry = load_directory(str(tmp_path))
    assert "base-isa" in registry.isas


# ── ScalarType isolation between ISAs ────────────────────────────────────────

_SCALAR_ISA = textwrap.dedent("""\
    apiVersion: isa-archive/v1
    kind: ISA
    metadata: {name: %(name)s}
    spec:
      version: "1.0"
      xlen: 32
      state:
        registers:
          - {name: acc, width: 8, count: 4, type: fp8}
    """)

_SCALAR_DOC = textwrap.dedent("""\
    ---
    apiVersion: isa-archive/v1
    kind: ScalarType
    metadata: {name: fp8}
    spec: {width: 8, arith_class: ieee_float}
    """)


def test_scalar_types_do_not_leak_between_isas(tmp_path):
    _write(tmp_path / "a.yaml", _SCALAR_ISA % {"name": "isa-a"} + _SCALAR_DOC)
    _write(tmp_path / "b.yaml", _SCALAR_ISA % {"name": "isa-b"})  # no fp8 declared
    registry = Registry()
    load_isa(str(tmp_path / "a.yaml"), registry)  # declares fp8, loads fine
    with pytest.raises(ValueError, match="unknown type 'fp8'"):
        load_isa(str(tmp_path / "b.yaml"), registry)


def test_extension_inherits_base_scalar_types(tmp_path):
    _write(tmp_path / "base.yaml", _SCALAR_ISA % {"name": "s-base"} + _SCALAR_DOC)
    _write(tmp_path / "ext.yaml", """\
        apiVersion: isa-archive/v1
        kind: ISA
        metadata: {name: s-ext}
        spec:
          version: "1.0"
          extends: base.yaml
        """)
    reg = load_isa(str(tmp_path / "ext.yaml"))
    assert "fp8" in reg.scalar_types


# ── chained comparisons ──────────────────────────────────────────────────────

def test_chained_comparison_constraint_expands_pairwise():
    assert constraint_to_c("0 < rd < 5") == "(0 < rd) && (rd < 5)"


def test_single_comparison_constraint_unchanged():
    assert constraint_to_c("rd != 0") == "rd != 0"


def test_chained_comparison_in_behavior_expands_pairwise():
    ir = BehaviorIR(
        "if 0 < rs1 < 5:\n    rd = 1\nelse:\n    rd = 0",
        register_map={"rd": "gpr", "rs1": "gpr"},
        var_widths={"rd": 32, "rs1": 32},
    )
    code = QemuCBackend(ir).translate()
    assert "(0 < rs1_val) && (rs1_val < 5)" in code


# ── augmented assignment desugaring ──────────────────────────────────────────

def test_augassign_gets_zero_register_guard():
    ir = BehaviorIR(
        "rd += rs1",
        register_map={"rd": "gpr", "rs1": "gpr"},
        var_widths={"rd": 32, "rs1": 32},
    )
    code = QemuCBackend(ir).translate(zero_register_map={"rd": 0})
    assert "if (rd != 0)" in code


def test_augassign_pc_gets_mask_and_branch_tracking():
    ir = BehaviorIR("pc += 4", var_widths={"pc": 32})
    assert ir.modifies_pc and ir.is_unconditional_jump
    code = QemuCBackend(ir).translate(pc_write_tracking=True, pc_mask="0xFFFFu")
    assert "_branch_taken = 1;" in code
    assert "& 0xFFFFu" in code


# ── duplicate definitions warn ───────────────────────────────────────────────

def test_duplicate_definition_warns(tmp_path, caplog):
    _write(tmp_path / "isa.yaml",
           BASE_ISA.replace('spec:\n  version: "1.0"',
                            'spec:\n  version: "1.0"\n  includes: ["extra.yaml"]', 1))
    _write(tmp_path / "extra.yaml", """\
        apiVersion: isa-archive/v1
        kind: Instruction
        metadata: {name: ADD}
        spec:
          schema: RType
          opcode: 0x33
          behavior: "rd = rs1 + rs2"
        """)
    with caplog.at_level(logging.WARNING):
        reg = load_isa(str(tmp_path / "isa.yaml"))
    assert any("defined more than once" in r.message for r in caplog.records)
    assert "ADD" in reg.instructions
