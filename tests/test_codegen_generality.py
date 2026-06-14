"""Tests for the QEMU/LLVM generality fixes (plan: qemu-llvm-codegen-generality.md).

Covers: Q1/Q2 (register storage model), Q3 (width-aware TCG fast path),
Q4 (big-endian QEMU config), Q5 (loud per-instruction errors), Q6 (zero-guard
field match), L1 (full-width loads), L2 (pattern operand coverage),
L3 (register-class partition), L4 (xlen-parameterized required roles).
"""
import pytest

from isa_archive.models import (
    ISA, ISASpec, ISAState, Register, Metadata,
    Schema, SchemaSpec, Instruction, InstructionSpec,
)
from isa_archive.models.schema import SchemaField
from isa_archive.compiler.loader import ISARegistry, Registry
from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.backends import LLVMDagBackend, QemuCBackend, QemuTCGBackend
from isa_archive.generators.llvm import (
    generate_llvm, _build_instr_defs, _required_roles, _role_groups,
    _setcc_branch_entries,
)
from isa_archive.generators.qemu import (
    generate_qemu_isa, _regfile_storage, _validate_for_qemu,
)


def _registry(registers, schemas, instructions, name="test-isa", xlen=32,
              byte_order="little") -> Registry:
    manifest = ISA(
        metadata=Metadata(name=name),
        spec=ISASpec(name=name, version="1.0", xlen=xlen, byte_order=byte_order,
                     state=ISAState(registers=registers)),
    )
    reg = ISARegistry(manifest)
    for s in schemas:
        reg.add(s)
    for i in instructions:
        reg.add(i)
    reg.validate()
    registry = Registry()
    registry.isas[name] = reg
    return registry


def _rtype(field_type="gpr", extra_fields=()):
    return Schema(
        metadata=Metadata(name="RType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0, width=7, role="opcode"),
            SchemaField(name="rd", start=7, width=5, role="register", type=field_type),
            SchemaField(name="rs1", start=12, width=5, role="register", type=field_type),
            SchemaField(name="rs2", start=17, width=5, role="register", type=field_type),
            *extra_fields,
        ]),
    )


def _instr(name="ADD", schema="RType", behavior="rd = rs1 + rs2", opcode=0x33):
    return Instruction(
        metadata=Metadata(name=name),
        spec=InstructionSpec(**{"schema": schema, "opcode": opcode,
                                "behavior": behavior}),
    )


def _gpr(width=32, count=32, **kw):
    return Register(name="gpr", width=width, count=count, **kw)


# ── Q1/Q2: register storage model ────────────────────────────────────────────

def test_storage_xlen_width_file_gets_tcg_global():
    reg = _registry([_gpr()], [_rtype()], [_instr()])
    st = _regfile_storage(reg.isas["test-isa"])["gpr"]
    assert st["tcg"] == "i32" and st["c_type"] == "uint32_t" and st["mask"] is None


def test_storage_narrow_file_is_helper_only():
    regs = [_gpr(), Register(name="preg", width=1, count=8)]
    reg = _registry(regs, [_rtype()], [_instr()])
    st = _regfile_storage(reg.isas["test-isa"])["preg"]
    assert st["tcg"] is None
    assert st["c_type"] == "uint8_t"      # never uint1_t
    assert st["mask"] == "0x1u"


def test_storage_128bit_file_is_native_int128():
    regs = [_gpr(), Register(name="vreg", width=128, count=16)]
    reg = _registry(regs, [_rtype()], [_instr()])
    st = _regfile_storage(reg.isas["test-isa"])["vreg"]
    assert st["c_type"] == "__uint128_t" and st["tcg"] is None and st["mask"] is None


def test_storage_wide_file_is_byte_array():
    regs = [_gpr(), Register(name="vreg", width=256, count=8)]
    reg = _registry(regs, [_rtype()], [_instr()])
    st = _regfile_storage(reg.isas["test-isa"])["vreg"]
    assert st["c_type"] is None and st["bytes"] == 32 and st["tcg"] is None


def test_generated_arch_h_has_no_invalid_c_types(tmp_path):
    regs = [_gpr(), Register(name="preg", width=1, count=8)]
    registry = _registry(regs, [_rtype()], [_instr()])
    generate_qemu_isa(registry, str(tmp_path))
    arch_h = (tmp_path / "test-isa_arch.h").read_text()
    assert "uint1_t" not in arch_h
    assert "uint8_t preg[8];" in arch_h


def test_generated_trans_has_no_tcg_global_for_narrow_file(tmp_path):
    regs = [_gpr(), Register(name="hreg", width=16, count=4)]
    registry = _registry(regs, [_rtype()], [_instr()])
    generate_qemu_isa(registry, str(tmp_path))
    trans = (tmp_path / "test-isa_trans.c.inc").read_text()
    assert "tcg_global_mem_new" in trans            # gpr still has globals
    assert "arch_hreg[i] = tcg_global_mem_new" not in trans


# ── Q3: width-aware TCG fast path ────────────────────────────────────────────

def test_tcg_fast_path_bails_for_non_xlen_register_file():
    ir = BehaviorIR("rd = rs1 + rs2",
                    register_map={"rd": "vreg", "rs1": "vreg", "rs2": "vreg"},
                    var_widths={"rd": 128, "rs1": 128, "rs2": 128, "pc": 32})
    assert QemuTCGBackend(ir).translate(xlen=32, tcg_regfiles={"gpr"}) is None


def test_tcg_fast_path_still_fires_for_xlen_file():
    ir = BehaviorIR("rd = rs1 + rs2",
                    register_map={"rd": "gpr", "rs1": "gpr", "rs2": "gpr"},
                    var_widths={"rd": 32, "rs1": 32, "rs2": 32, "pc": 32})
    out = QemuTCGBackend(ir).translate(xlen=32, tcg_regfiles={"gpr"})
    assert out is not None and "tcg_gen_add_i32" in out


# ── Q4: big-endian QEMU config ───────────────────────────────────────────────

def test_qemu_configs_mark_big_endian(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    registry = _registry([_gpr()], [_rtype()], [_instr()], byte_order="big")
    generate_qemu(registry, str(tmp_path))
    mak = (tmp_path / "configs" / "targets" / "test-isa-softmmu.mak").read_text()
    assert "TARGET_BIG_ENDIAN=y" in mak


def test_qemu_configs_little_endian_default(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    registry = _registry([_gpr()], [_rtype()], [_instr()])
    generate_qemu(registry, str(tmp_path))
    mak = (tmp_path / "configs" / "targets" / "test-isa-softmmu.mak").read_text()
    assert "TARGET_BIG_ENDIAN" not in mak


# ── Q5: loud, aggregated per-instruction errors ──────────────────────────────

def test_qemu_unknown_schema_names_instruction():
    # The loader's validate() catches this first in normal flow; the generator
    # guard is defense-in-depth for callers that skip validate().
    from isa_archive.generators.qemu import _instr_qemu_info
    manifest = ISA(metadata=Metadata(name="t"),
                   spec=ISASpec(name="t", version="1.0",
                                state=ISAState(registers=[_gpr()])))
    reg = ISARegistry(manifest)
    reg.add(_rtype())
    bad = _instr(name="BAD", schema="NoSuch", opcode=0x44)
    reg.add(bad)
    with pytest.raises(ValueError, match="NoSuch"):
        _instr_qemu_info(bad, reg, _regfile_storage(reg))


def test_qemu_128bit_register_arithmetic_generates(tmp_path):
    # Exactly-128-bit files compute natively via __uint128_t (helper-only).
    regs = [_gpr(), Register(name="vreg", width=128, count=16)]
    vtype = _rtype(field_type="vreg")
    vtype.metadata.name = "VType"
    vadd = _instr(name="VADD", schema="VType", opcode=0x10)
    registry = _registry(regs, [_rtype(), vtype], [_instr(), vadd])
    generate_qemu_isa(registry, str(tmp_path))
    helpers = (tmp_path / "test-isa_helpers.c").read_text()
    assert "env->vreg[rd] = (env->vreg[rs1] + env->vreg[rs2]);" in helpers
    arch_h = (tmp_path / "test-isa_arch.h").read_text()
    assert "__uint128_t vreg[16];" in arch_h


def test_qemu_wide_register_arithmetic_fails_loudly(tmp_path):
    # >64-bit widths OTHER than 128 stay byte arrays with no arithmetic.
    regs = [_gpr(), Register(name="vreg", width=256, count=8)]
    vtype = _rtype(field_type="vreg")
    vtype.metadata.name = "VType"
    vadd = _instr(name="VADD", schema="VType", opcode=0x10)
    registry = _registry(regs, [_rtype(), vtype], [_instr(), vadd])
    with pytest.raises(ValueError) as e:
        generate_qemu_isa(registry, str(tmp_path))
    msg = str(e.value)
    assert "VADD" in msg and "vreg" in msg and "byte" in msg


def test_qemu_rejects_unsupported_xlen(tmp_path):
    registry = _registry([_gpr(width=256)], [_rtype(field_type="gpr")],
                         [_instr()], xlen=256)
    with pytest.raises(ValueError) as e:
        generate_qemu_isa(registry, str(tmp_path))
    assert "power-of-two xlen" in str(e.value)


def test_xlen128_generates_with_64bit_address_space(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    registry = _registry([_gpr(width=128)], [_rtype(field_type="gpr")],
                         [_instr()], xlen=128)
    generate_qemu(registry, str(tmp_path))
    arch_h = (tmp_path / "target" / "test-isa" / "test-isa_arch.h").read_text()
    assert "uint64_t pc;" in arch_h               # 64-bit PC over a 64-bit guest word
    assert "__uint128_t gpr[32];" in arch_h       # native 128-bit registers
    params = (tmp_path / "target" / "test-isa" / "cpu-param.h").read_text()
    assert "#define TARGET_LONG_BITS              64" in params
    assert "TARGET_VIRT_ADDR_SPACE_BITS   64" in params
    helpers = (tmp_path / "target" / "test-isa" / "test-isa_helpers.c").read_text()
    assert "env->gpr[rd] = (env->gpr[rs1] + env->gpr[rs2]);" in helpers
    trans = (tmp_path / "target" / "test-isa" / "test-isa_trans.c.inc").read_text()
    # 128-bit files are helper-only: index args, no TCG globals
    assert "arch_gpr[i] = tcg_global_mem_new" not in trans


# ── Q6: zero-register guard targets the right field ──────────────────────────

def test_zero_guard_only_on_mapped_field():
    # Two register writes; only rd has a zero-register mapping. The guard must
    # be on rd's assignment only, and must test rd (not some other field).
    ir = BehaviorIR("rd = rs1 + 1\nrs1 = rs1 + 2",
                    register_map={"rd": "gpr", "rs1": "gpr"},
                    var_widths={"rd": 32, "rs1": 32, "pc": 32})
    code = QemuCBackend(ir).translate(zero_register_map={"rd": 0})
    lines = code.splitlines()
    assert any("if (rd != 0)" in l for l in lines)
    assert not any("if (rd != 0)" in l and "gpr[rs1]" in l for l in lines)
    assert "if (rs1 != 0)" not in code


# ── L1: full-width loads select as plain loads ───────────────────────────────

def _dag(behavior, xlen=32, reg_map=None, widths=None):
    reg_map = reg_map or {"rd": "gpr", "rs1": "gpr", "rs2": "gpr"}
    widths = widths or {"rd": xlen, "rs1": xlen, "rs2": xlen, "pc": xlen}
    ir = BehaviorIR(behavior, register_map=reg_map, var_widths=widths)
    return LLVMDagBackend(ir, xlen=xlen,
                          reg_class_info={"gpr": {"class": "GPR", "is_float": False}}
                          ).translate()


def test_full_width_load_is_plain_load():
    p = _dag("rd = mem32[rs1 + imm]")
    assert p.category == "load" and "(load " in p.dag and "zextloadi32" not in p.dag


def test_subword_load_keeps_extension():
    assert "zextloadi8" in _dag("rd = mem8[rs1 + imm]").dag
    assert "sextloadi8" in _dag("rd = sext(mem8[rs1 + imm], 8)").dag


def test_overwide_load_and_store_are_custom():
    assert _dag("rd = mem64[rs1 + imm]").category == "custom"
    assert _dag("mem64[rs1 + imm] = rs2").category == "custom"


# ── L2: pattern operand coverage ─────────────────────────────────────────────

def test_uncovered_operand_demotes_to_custom():
    extra = (SchemaField(name="hint", start=22, width=5, role="register", type="gpr"),)
    registry = _registry([_gpr()], [_rtype(extra_fields=extra)],
                         [_instr(behavior="rd = rs1 + rs2")])
    isa_reg = registry.isas["test-isa"]
    defs = dict(_build_instr_defs(isa_reg, "TEST"))
    info = defs["ADD"]
    assert info["dag_pattern"] is None
    assert info["dag_category"] == "custom"
    assert info["dag_op"] is None


def test_covered_operands_keep_pattern():
    registry = _registry([_gpr()], [_rtype()], [_instr()])
    isa_reg = registry.isas["test-isa"]
    info = dict(_build_instr_defs(isa_reg, "TEST"))["ADD"]
    assert info["dag_pattern"] is not None and info["dag_category"] == "alu_rr"


# ── L3: register-class partition ─────────────────────────────────────────────

def test_non_xlen_register_files_excluded_from_llvm(tmp_path):
    regs = [_gpr(),
            Register(name="preg", width=1, count=8),
            Register(name="vreg", width=128, count=16)]
    vtype = _rtype(field_type="vreg")
    vtype.metadata.name = "VType"
    vadd = _instr(name="VADD", schema="VType", opcode=0x10)
    registry = _registry(regs, [_rtype(), vtype], [_instr(), vadd])
    generate_llvm(registry, str(tmp_path))
    target = tmp_path / "llvm" / "lib" / "Target" / "TEST_ISA"
    reg_td = (target / "TEST_ISARegisterInfo.td").read_text()
    assert "[i1]" not in reg_td and "[i128]" not in reg_td
    assert "def GPR" in reg_td and "def PREG" not in reg_td and "def VREG" not in reg_td
    instr_td = (target / "TEST_ISAInstrInfo.td").read_text()
    assert "def VADD" not in instr_td and "def ADD" in instr_td


# ── L4: required roles follow xlen ───────────────────────────────────────────

def test_required_roles_track_xlen():
    assert "mem.load32" in _required_roles(32)
    assert "mem.load64" in _required_roles(64)
    assert "mem.load32" not in _required_roles(64)
    assert "mem.load16" in _required_roles(16)


def test_role_groups_memory_row_tracks_xlen():
    mem64 = _role_groups(64)["Memory"]
    assert "mem.load32s" in mem64 and "mem.load64" in mem64 and "mem.store64" in mem64
    mem32 = _role_groups(32)["Memory"]
    assert "mem.load32" in mem32 and "mem.load64" not in mem32


# ── G1: compiler profile ─────────────────────────────────────────────────────

def _profile_registry(profile=None, aliases=None, **kw):
    regs = [Register(name="gpr", width=32, count=16, aliases=aliases or {})]
    manifest = ISA(
        metadata=Metadata(name="prof-isa"),
        spec=ISASpec(name="prof-isa", version="1.0", xlen=32,
                     compiler=profile,
                     state=ISAState(registers=regs)),
    )
    reg = ISARegistry(manifest)
    reg.add(_rtype())
    reg.add(_instr())
    reg.validate()
    registry = Registry()
    registry.isas["prof-isa"] = reg
    return registry


def test_kernel_only_profile_is_complete_without_stack(tmp_path):
    from isa_archive.models.compiler import CompilerProfile
    registry = _profile_registry(CompilerProfile(profile="kernel-only"))
    # strict must NOT raise: nothing is required for a compute-only target
    generate_llvm(registry, str(tmp_path), strict=True)
    report = (tmp_path / "llvm" / "lib" / "Target" / "PROF_ISA"
              / "COMPILER_COVERAGE.md").read_text()
    assert "COMPILER-COMPLETE" in report and "kernel-only" in report


def test_c_baremetal_requires_declared_aliases(tmp_path):
    # Alias-less ISA under the default profile: sp/ra/zero are NOT invented
    # positionally any more — they show up as missing prerequisites.
    with pytest.raises(ValueError) as e:
        generate_llvm(_profile_registry(), str(tmp_path), strict=True)
    msg = str(e.value)
    assert "alias:sp" in msg and "profile" in msg


def test_custom_profile_requires_only_listed_roles(tmp_path):
    from isa_archive.models.compiler import CompilerProfile
    registry = _profile_registry(
        CompilerProfile(profile="custom", requires=["alu_rr.add"]))
    generate_llvm(registry, str(tmp_path), strict=True)  # ADD exists → complete


def test_custom_profile_missing_role_fails_strict(tmp_path):
    from isa_archive.models.compiler import CompilerProfile
    registry = _profile_registry(
        CompilerProfile(profile="custom", requires=["alu_rr.xor"]))
    with pytest.raises(ValueError, match="alu_rr.xor"):
        generate_llvm(registry, str(tmp_path), strict=True)


def test_requires_only_valid_with_custom_profile():
    from isa_archive.models.compiler import CompilerProfile
    with pytest.raises(ValueError, match="custom"):
        CompilerProfile(profile="kernel-only", requires=["alu_rr.add"])


# ── L5: no positional sp/ra/zero invention ───────────────────────────────────

def test_alias_less_isa_gets_no_invented_stack_pointer(tmp_path):
    registry = _profile_registry()  # no aliases at all
    generate_llvm(registry, str(tmp_path))  # non-strict: warns, still generates
    fl = (tmp_path / "llvm" / "lib" / "Target" / "PROF_ISA"
          / "PROF_ISAFrameLowering.cpp").read_text()
    assert "::g2" not in fl and "::g1" not in fl and "::g0" not in fl


def test_qemu_virt_board_skips_sp_init_without_alias(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    registry = _registry([_gpr()], [_rtype()], [_instr()])  # no aliases
    generate_qemu(registry, str(tmp_path))
    virt = (tmp_path / "hw" / "test-isa" / "virt.c").read_text()
    assert "= VIRT_RAM_BASE + VIRT_RAM_SIZE" not in virt


def test_qemu_virt_board_inits_sp_from_alias(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    regs = [Register(name="gpr", width=32, count=32, aliases={"sp": 14})]
    registry = _registry(regs, [_rtype()], [_instr()])
    generate_qemu(registry, str(tmp_path))
    virt = (tmp_path / "hw" / "test-isa" / "virt.c").read_text()
    assert "gpr[14] = VIRT_RAM_BASE + VIRT_RAM_SIZE" in virt


# ── G5: unknown YAML keys are rejected ───────────────────────────────────────

def test_models_forbid_unknown_keys():
    with pytest.raises(Exception, match="byte_oder"):
        ISASpec(version="1.0", byte_oder="big")
    with pytest.raises(Exception, match="widht"):
        Register(name="gpr", widht=32, width=32, count=4)
    with pytest.raises(Exception):
        ISASpec(version="1.0", byte_order="middle")  # invalid enum value


# ── L7: immediate operand types are threaded, not string-patched ─────────────

def test_split_immediate_non_12bit_gets_correct_operand_type():
    # A 9-bit split immediate (imm_8_5 + imm_4_0): the old string-replacement
    # only worked when the combined width happened to be 12 (RISC-V S-type).
    schema = Schema(
        metadata=Metadata(name="S9"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0, width=7, role="opcode"),
            SchemaField(name="imm_4_0", start=7, width=5, role="immediate", type="signed"),
            SchemaField(name="rs1", start=12, width=5, role="register", type="gpr"),
            SchemaField(name="rs2", start=17, width=5, role="register", type="gpr"),
            SchemaField(name="imm_8_5", start=22, width=4, role="immediate", type="signed"),
        ]),
    )
    store = Instruction(
        metadata=Metadata(name="SW9"),
        spec=InstructionSpec(**{"schema": "S9", "opcode": 0x23,
                                "behavior": "mem32[rs1 + {imm_8_5, imm_4_0}] = rs2"}),
    )
    registry = _registry([_gpr()], [schema], [store])
    info = dict(_build_instr_defs(registry.isas["test-isa"], "TEST"))["SW9"]
    assert "simm9:$imm" in info["dag_pattern"]
    assert "simm12" not in info["dag_pattern"]
    assert "simm9:$imm" in info["ins"]


# ── G2: extension casts follow the target register width ─────────────────────

def test_sext_uses_target_register_width():
    ir = BehaviorIR("ad = sext(rs1, 16)",
                    register_map={"ad": "acc", "rs1": "gpr"},
                    var_widths={"ad": 64, "rs1": 32, "pc": 32})
    code = QemuCBackend(ir).translate(helper_only_regfiles={"acc"})
    # sext lowers to the width-specific isa_sextN helper (64-bit target → sext64).
    assert "isa_sext64(" in code and "isa_sext32(" not in code


def test_sext_default_width_unchanged_for_xlen_targets():
    ir = BehaviorIR("rd = sext(imm, 12)",
                    register_map={"rd": "gpr"},
                    var_widths={"rd": 32, "imm": 12, "pc": 32})
    code = QemuCBackend(ir).translate()
    assert "isa_sext32(" in code


# ── Flexible xlen: narrow data widths emulated over a 32-bit guest word ──────

def _xlen16_registry():
    """A 16-bit-data ISA: 16-bit registers/PC/addresses, 16-bit insn words."""
    schema = Schema(
        metadata=Metadata(name="R16"),
        spec=SchemaSpec(length=16, fields=[
            SchemaField(name="opcode", start=0, width=4, role="opcode"),
            SchemaField(name="rd", start=4, width=4, role="register", type="gpr"),
            SchemaField(name="rs1", start=8, width=4, role="register", type="gpr"),
            SchemaField(name="rs2", start=12, width=4, role="register", type="gpr"),
        ]),
    )
    branch = Schema(
        metadata=Metadata(name="B16"),
        spec=SchemaSpec(length=16, fields=[
            SchemaField(name="opcode", start=0, width=4, role="opcode"),
            SchemaField(name="rs1", start=4, width=4, role="register", type="gpr"),
            SchemaField(name="rs2", start=8, width=4, role="register", type="gpr"),
            SchemaField(name="imm", start=12, width=4, role="immediate", type="signed"),
        ]),
    )
    instrs = [
        _instr(name="ADD", schema="R16", opcode=0x1),
        Instruction(metadata=Metadata(name="BEQ"),
                    spec=InstructionSpec(**{
                        "schema": "B16", "opcode": 0x2,
                        "behavior": "if rs1 == rs2:\n    pc = pc + sext(imm, 4)"})),
        Instruction(metadata=Metadata(name="LW"),
                    spec=InstructionSpec(**{
                        "schema": "B16", "opcode": 0x3,
                        "behavior": "rs1 = mem16[rs2 + imm]"})),
    ]
    return _registry([Register(name="gpr", width=16, count=16)],
                     [schema, branch], instrs, xlen=16)


def test_xlen16_storage_uses_guest_word_with_mask():
    reg = _xlen16_registry().isas["test-isa"]
    st = _regfile_storage(reg)["gpr"]
    # xlen-wide file: guest-word storage + i32 TCG global + masked writes
    assert st["tcg"] == "i32" and st["c_type"] == "uint32_t" and st["mask"] == "0xFFFFu"


def test_xlen16_generates_valid_qemu(tmp_path):
    generate_qemu_isa(_xlen16_registry(), str(tmp_path))
    arch_h = (tmp_path / "test-isa_arch.h").read_text()
    assert "uint32_t pc;" in arch_h          # guest-word PC storage
    assert "uint16_t" not in arch_h          # gpr widened to the guest word
    helpers = (tmp_path / "test-isa_helpers.c").read_text()
    assert "& 0xFFFFu" in helpers            # masked register/pc writes
    # branch fall-through and taken-path PC writes both masked to 16 bits
    assert "env->pc = ((env->pc + " in helpers or "env->pc = (env->pc + " in helpers
    translate = (tmp_path / "test-isa_translate.c").read_text()
    assert "translator_lduw" in translate    # 16-bit instruction fetch


def test_xlen16_memory_addresses_are_masked():
    ir = BehaviorIR("rd = mem16[rs1 + imm]",
                    register_map={"rd": "gpr", "rs1": "gpr"},
                    var_widths={"rd": 16, "rs1": 16, "imm": 4, "pc": 16})
    code = QemuCBackend(ir).translate(helper_only_regfiles=set(),
                                      regfile_write_masks={"gpr": "0xFFFFu"},
                                      addr_mask="0xFFFFu")
    assert "& 0xFFFFu, GETPC())" in code     # load address clamped to 64K


def test_xlen16_tcg_fast_path_disabled():
    ir = BehaviorIR("rd = rs1 + rs2",
                    register_map={"rd": "gpr", "rs1": "gpr", "rs2": "gpr"},
                    var_widths={"rd": 16, "rs1": 16, "rs2": 16, "pc": 16})
    # Unmasked 32-bit TCG adds would be wrong for 16-bit registers.
    assert QemuTCGBackend(ir).translate(xlen=16, tcg_regfiles={"gpr"}) is None


def test_xlen16_cpu_param_targets_32bit_guest(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    from isa_archive.models.machine import MachineLayout
    registry = _xlen16_registry()
    registry.isas["test-isa"].manifest.spec.machine = MachineLayout(
        ram_base=0x8000, ram_size=0x4000)
    registry.isas["test-isa"].machine = registry.isas["test-isa"].manifest.spec.machine
    generate_qemu(registry, str(tmp_path))
    params = (tmp_path / "target" / "test-isa" / "cpu-param.h").read_text()
    assert "#define TARGET_LONG_BITS              32" in params
    assert "TARGET_VIRT_ADDR_SPACE_BITS   16" in params
    assert "TARGET_PAGE_BITS              8" in params


def test_xlen16_machine_layout_must_fit_address_space(tmp_path):
    from isa_archive.generators.qemu import generate_qemu
    registry = _xlen16_registry()  # default machine: ram_base=0x80000000
    from isa_archive.models.machine import MachineLayout
    registry.isas["test-isa"].manifest.spec.machine = MachineLayout()
    registry.isas["test-isa"].machine = registry.isas["test-isa"].manifest.spec.machine
    with pytest.raises(ValueError, match="address space"):
        generate_qemu(registry, str(tmp_path))


# ── G3: wide-instruction ceiling has an explanatory error ────────────────────

def test_qemu_wide_insn_error_explains_ceiling(tmp_path):
    wide = Schema(
        metadata=Metadata(name="W128"),
        spec=SchemaSpec(length=128, fields=[
            SchemaField(name="opcode", start=0, width=8, role="opcode"),
            SchemaField(name="rd", start=8, width=4, role="register", type="gpr"),
            SchemaField(name="rs1", start=12, width=4, role="register", type="gpr"),
            SchemaField(name="rs2", start=16, width=4, role="register", type="gpr"),
            SchemaField(name="pad", start=20, width=108, role="reserved"),
        ]),
    )
    registry = _registry([_gpr(count=16)], [wide], [_instr(schema="W128")])
    with pytest.raises(ValueError, match="LLVM-only"):
        generate_qemu_isa(registry, str(tmp_path))


# ── examples/npu-probe: the generality contract, end to end ──────────────────

def test_npu_probe_example_generates_on_both_paths(tmp_path):
    import pathlib
    from isa_archive.compiler.loader import load_isa
    from isa_archive.generators.qemu import generate_qemu

    isa_path = (pathlib.Path(__file__).parent.parent
                / "examples" / "npu-probe" / "isa.yaml")
    registry = Registry()
    load_isa(str(isa_path), registry)

    generate_qemu(registry, str(tmp_path / "qemu"))
    arch_h = (tmp_path / "qemu" / "target" / "npu-probe" / "npu-probe_arch.h").read_text()
    assert "uint1_t" not in arch_h and " uint128_t" not in arch_h
    assert "__uint128_t vreg[16];" in arch_h
    helpers = (tmp_path / "qemu" / "target" / "npu-probe" / "npu-probe_helpers.c").read_text()
    assert "env->vreg[vd] = (env->vreg[vs1] + env->vreg[vs2]);" in helpers
    mak = (tmp_path / "qemu" / "configs" / "targets" / "npu-probe-softmmu.mak").read_text()
    assert "TARGET_BIG_ENDIAN=y" in mak

    generate_llvm(registry, str(tmp_path / "llvm"))
    target = tmp_path / "llvm" / "llvm" / "lib" / "Target" / "NPU_PROBE"
    reg_td = (target / "NPU_PROBERegisterInfo.td").read_text()
    assert "[i1]" not in reg_td and "[i128]" not in reg_td
    instr_td = (target / "NPU_PROBEInstrInfo.td").read_text()
    assert "def ADD" in instr_td
    assert "def PSET_LT" not in instr_td and "def VADD" not in instr_td


# ── SETCC materialization (comparison-as-value without a set-less-than instr) ──

def test_setcc_branch_entries_cover_all_conditions():
    # An ISA with eq/ne/lt/ltu branches can synthesize all ten ISD conditions.
    roles = {f"branch.{c}": f"B{c.upper()}" for c in ("eq", "ne", "lt", "ltu")}
    entries = _setcc_branch_entries(roles)
    nodes = {e["node"] for e in entries}
    assert nodes == {"seteq", "setne", "setlt", "setgt", "setge", "setle",
                     "setult", "setugt", "setuge", "setule"}
    # codes are dense and unique
    assert sorted(e["code"] for e in entries) == list(range(len(entries)))
    # greater-than reuses less-than with swapped operands
    gt = next(e for e in entries if e["node"] == "setgt")
    assert gt["opcode"] == "BLT" and gt["swap"] and gt["taken_one"]
    # >= with only lt: branch on (a<b), taken yields 0
    ge = next(e for e in entries if e["node"] == "setge")
    assert ge["opcode"] == "BLT" and not ge["swap"] and not ge["taken_one"]


def test_setcc_via_branch_emitted_only_without_set_less_than(tmp_path):
    import pathlib
    from isa_archive.compiler.loader import load_isa
    ex = pathlib.Path(__file__).parent.parent / "examples"

    # pico32 (tutorial part 3): ordering branches, no SLT → branch-diamond setcc
    reg = Registry(); load_isa(str(ex / "tutorial/pico32-part3/isa.yaml"), reg)
    generate_llvm(reg, str(tmp_path / "pico32"), strict=True)
    t = tmp_path / "pico32" / "llvm" / "lib" / "Target" / "PICO32"
    instr = (t / "PICO32InstrInfo.td").read_text()
    lower = (t / "PICO32ISelLowering.cpp").read_text()
    assert "def PseudoSetCC" in instr
    assert instr.count("(PseudoSetCC ") >= 10          # a Pat per condition
    assert "ZeroOrOneBooleanContent" in lower
    assert "case PICO32::PseudoSetCC:" in lower

    # cmpisa fixture (has SLT): comparisons use the SLT instruction, no diamond
    fixtures = pathlib.Path(__file__).parent / "fixtures"
    reg2 = Registry(); load_isa(str(fixtures / "cmpisa.yaml"), reg2)
    generate_llvm(reg2, str(tmp_path / "cmpisa"))
    t2 = tmp_path / "cmpisa" / "llvm" / "lib" / "Target" / "CMPISA"
    assert "def PseudoSetCC" not in (t2 / "CMPISAInstrInfo.td").read_text()
