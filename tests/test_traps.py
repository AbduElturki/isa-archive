"""Traps / exceptions / system instructions: the `csr.*` DSL namespace, the
trap()/trap_return() primitives, their QEMU C lowering, and loader validation."""
import types
import pathlib

import pytest

from isa_archive.models import ISA, ISASpec, ISAState, Metadata, Register, Schema, Instruction
from isa_archive.models.isa import ISACSR, Trap
from isa_archive.models.csr import CSRField
from isa_archive.models.schema import SchemaSpec, SchemaField
from isa_archive.compiler.loader import ISARegistry, Registry, load_isa
from isa_archive.compiler.behavior import BehaviorIR
from isa_archive.compiler.backends import QemuCBackend
from isa_archive.compiler.utils import build_csr_info, build_trap_info, csr_map

REPO = pathlib.Path(__file__).resolve().parent.parent


# ── shared fixtures ──────────────────────────────────────────────────────────

def _csr_objs():
    return [
        ISACSR(name="mstatus", address=0x300, width=32, fields=[
            CSRField(name="mie", start=3, end=3), CSRField(name="mpie", start=7, end=7)]),
        ISACSR(name="mtvec", address=0x305, width=32, fields=[]),
        ISACSR(name="mepc", address=0x341, width=32, fields=[]),
        ISACSR(name="mcause", address=0x342, width=32, fields=[]),
    ]


def _trap():
    return Trap(vector_csr="mtvec", epc_csr="mepc", cause_csr="mcause",
                status_csr="mstatus", causes={"ecall_m": 11, "illegal": 2})


def _ctx():
    """(csrs-dict, csr_info, trap_info) for driving the backend directly."""
    ns = types.SimpleNamespace(arch_csrs=_csr_objs(), trap=_trap())
    return csr_map(ns), build_csr_info(ns), build_trap_info(ns)


def _lower(behavior, register_map=None, var_widths=None):
    csrs, ci, ti = _ctx()
    ir = BehaviorIR(behavior, register_map=register_map or {},
                    var_widths={"pc": 32, **(var_widths or {})}, csrs=csrs)
    return ir, QemuCBackend(ir).translate(csr_info=ci, trap_info=ti)


# ── behavior-IR analysis flags ───────────────────────────────────────────────

def test_csr_read_sets_reads_flag_and_excludes_namespace_from_vars():
    ir, _ = _lower("rd = csr.mcause", register_map={"rd": "gpr"}, var_widths={"rd": 32})
    assert ir.reads_csr and not ir.writes_csr
    assert "csr" not in ir.used_vars and "mcause" not in ir.used_vars
    assert ir.csrs_used == {"mcause"}


def test_trap_sets_flags_and_excludes_cause_name():
    ir, _ = _lower("trap(ecall_m)")
    assert ir.uses_trap and ir.modifies_pc and ir.is_unconditional_jump
    assert "ecall_m" not in ir.used_vars and "trap" not in ir.used_vars
    assert ir.trap_causes_used == ["ecall_m"]


def test_uses_sys_property():
    assert _lower("trap_return()")[0].uses_sys
    assert _lower("csr.mtvec = rs1", register_map={"rs1": "gpr"}, var_widths={"rs1": 32})[0].uses_sys


# ── QEMU C lowering ──────────────────────────────────────────────────────────

def test_trap_lowers_to_epc_cause_vector_sequence():
    _, c = _lower("trap(ecall_m)")
    assert "env->mepc = env->pc;" in c
    assert "env->mcause = 11;" in c
    assert "env->pc = env->mtvec & ~0x3;" in c
    # mstatus: mpie = mie, then mie = 0
    assert "(((env->mstatus >> 3) & 0x1) << 7)" in c
    assert "env->mstatus = env->mstatus & ~0x8;" in c


def test_do_interrupt_body_vectors_like_trap():
    # Hardware interrupt delivery reuses the software-trap vectoring (epc, cause,
    # mie->mpie, mie=0, pc=mtvec&~3); the cause is a C variable the caller sets.
    from isa_archive.compiler.backends.qemu_c import build_do_interrupt_body
    _, ci, ti = _ctx()
    body = build_do_interrupt_body(ti, ci, pc_mask=None)
    assert "env->mepc = env->pc;" in body
    assert "env->mcause = cause;" in body
    assert "(((env->mstatus >> 3) & 0x1) << 7)" in body   # mpie = mie
    assert "env->mstatus = env->mstatus & ~0x8;" in body  # mie = 0
    assert "env->pc = env->mtvec & ~0x3;" in body


def test_do_interrupt_body_none_without_trap():
    from isa_archive.compiler.backends.qemu_c import build_do_interrupt_body
    assert build_do_interrupt_body(None, {}) is None


def test_trap_return_restores_pc_and_mie():
    _, c = _lower("trap_return()")
    assert "env->pc = env->mepc;" in c
    assert "(((env->mstatus >> 7) & 0x1) << 3)" in c  # mie = mpie


def test_csr_full_read_and_write():
    _, r = _lower("rd = csr.mcause", register_map={"rd": "gpr"}, var_widths={"rd": 32})
    assert "env->gpr[rd] = env->mcause;" in r
    _, w = _lower("csr.mtvec = rs1", register_map={"rs1": "gpr"}, var_widths={"rs1": 32})
    assert "env->mtvec = (rs1_val) & 0xffffffff;" in w


def test_csr_field_read_and_write_mask():
    _, r = _lower("rd = zext(csr.mstatus.mie)", register_map={"rd": "gpr"}, var_widths={"rd": 32})
    assert "((env->mstatus >> 3) & 0x1)" in r
    _, w = _lower("csr.mstatus.mie = 1", var_widths={})
    assert "env->mstatus = (env->mstatus & ~0x8) | (((1) & 0x1) << 3);" in w


def test_trap_without_block_raises():
    csrs, ci, _ = _ctx()
    ir = BehaviorIR("trap(ecall_m)", var_widths={"pc": 32}, csrs=csrs)
    with pytest.raises(ValueError, match="trap"):
        QemuCBackend(ir).translate(csr_info=ci, trap_info=None)


# ── loader validation ────────────────────────────────────────────────────────

def _sys_registry(behavior, *, with_trap=True, with_csrs=True, rd=False):
    fields = [SchemaField(name="opcode", start=0, width=7, role="opcode")]
    if rd:
        fields.append(SchemaField(name="rd", start=7, width=5, role="register", type="gpr"))
        fields.append(SchemaField(name="rest", start=12, width=20, role="reserved"))
    else:
        fields.append(SchemaField(name="rest", start=7, width=25, role="reserved"))
    schema = Schema(metadata=Metadata(name="Sys"), spec=SchemaSpec(length=32, fields=fields))
    instr = Instruction(metadata=Metadata(name="SYSI"),
                        spec=InstructionSpec_compat(behavior))
    manifest = ISA(metadata=Metadata(name="t"), spec=ISASpec(
        name="t", version="1.0", xlen=32,
        state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)],
                       csrs=_csr_objs() if with_csrs else []),
        trap=_trap() if with_trap else None,
    ))
    reg = ISARegistry(manifest)
    reg.add(schema)
    reg.add(instr)
    return reg


def InstructionSpec_compat(behavior):
    from isa_archive.models.instruction import InstructionSpec
    return InstructionSpec(**{"schema": "Sys", "opcode": 0x73, "behavior": behavior})


def test_loader_rejects_trap_without_block():
    reg = _sys_registry("trap(ecall_m)", with_trap=False)
    with pytest.raises(ValueError, match="no `trap:` block"):
        reg.validate()


def test_loader_rejects_undeclared_cause():
    reg = _sys_registry("trap(bogus)")
    with pytest.raises(ValueError, match="not declared in spec.trap.causes"):
        reg.validate()


def test_loader_rejects_unknown_csr():
    reg = _sys_registry("rd = csr.nope", rd=True)
    with pytest.raises(ValueError, match="undeclared CSR"):
        reg.validate()


def test_loader_accepts_valid_trap_and_csr():
    _sys_registry("trap(ecall_m)").validate()          # no raise
    _sys_registry("rd = csr.mcause", rd=True).validate()


# ── integration: the shipped sys example ─────────────────────────────────────

def test_sys_example_generates_trap_helpers(tmp_path):
    from isa_archive.generators.qemu import generate_qemu_isa
    sys_isa = REPO / "examples/tutorial/pico32-part4/sys/isa.yaml"
    reg = load_isa(str(sys_isa))
    registry = Registry()
    registry.isas[reg.name] = reg
    generate_qemu_isa(registry, str(tmp_path))
    helpers = next(tmp_path.glob("*_helpers.c")).read_text()
    assert "env->mepc = env->pc;" in helpers      # ECALL
    assert "env->pc = env->mepc;" in helpers       # MRET
    assert "env->gpr[rd] = env->mcause;" in helpers  # CSRR_CAUSE
