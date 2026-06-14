import pytest
from isa_archive.models import (
    ISA, ISASpec, ISAState, Register, Metadata,
    Schema, Instruction, EnumDef, EnumDefSpec,
)
from isa_archive.models.schema import SchemaField, SchemaSpec
from isa_archive.models.instruction import InstructionSpec
from isa_archive.compiler.loader import ISARegistry, Registry
from isa_archive.generators.software import generate_software


def _make_registry() -> Registry:
    manifest = ISA(
        metadata=Metadata(name="test-isa"),
        spec=ISASpec(
            name="TestISA",
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(EnumDef(metadata=Metadata(name="F3"), spec=EnumDefSpec(width=3, values={"ZERO": 0})))
    reg.add(EnumDef(metadata=Metadata(name="F7"), spec=EnumDefSpec(width=7, values={"BASE": 0})))
    reg.add(Schema(
        metadata=Metadata(name="RType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="funct3", start=12, width=3, role="constant", type="enum.F3"),
            SchemaField(name="rs1",    start=15, width=5, role="register", type="gpr"),
            SchemaField(name="rs2",    start=20, width=5, role="register", type="gpr"),
            SchemaField(name="funct7", start=25, width=7, role="constant", type="enum.F7"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="ADD"),
        spec=InstructionSpec(**{"schema": "RType", "opcode": 0x33, "constants": {"funct3": 0, "funct7": 0}, "behavior": "rd = rs1 + rs2"})
    ))
    reg.validate()
    registry = Registry()
    registry.isas["test-isa"] = reg
    return registry


def test_generate_c_creates_files(tmp_path):
    generate_software(_make_registry(), str(tmp_path), "c")
    assert (tmp_path / "test-isa_intrinsics.h").exists()
    assert (tmp_path / "test-isa_structs.h").exists()
    assert (tmp_path / "test-isa_csrs.h").exists()


def test_generate_c_intrinsics_contains_instruction(tmp_path):
    generate_software(_make_registry(), str(tmp_path), "c")
    content = (tmp_path / "test-isa_intrinsics.h").read_text()
    assert "ADD" in content


def test_generate_rust_creates_files(tmp_path):
    generate_software(_make_registry(), str(tmp_path), "rust")
    assert (tmp_path / "test-isa_intrinsics.rs").exists()
    assert (tmp_path / "test-isa_structs.rs").exists()
    assert (tmp_path / "test-isa_csrs.rs").exists()


def test_generate_rust_intrinsics_contains_instruction(tmp_path):
    generate_software(_make_registry(), str(tmp_path), "rust")
    content = (tmp_path / "test-isa_intrinsics.rs").read_text()
    assert "ADD" in content


# ── P2: register + immediate operands are actually wired ─────────────────────

def _make_imm_registry() -> Registry:
    """An R-type (ADD), an I-type with an immediate (ADDI), and a vector-register
    instruction (VADD) that scalar inline-asm cannot express."""
    manifest = ISA(
        metadata=Metadata(name="imm-isa"),
        spec=ISASpec(name="ImmISA", version="1.0", state=ISAState(registers=[
            Register(name="gpr", width=32, count=32, zero_register=0),
            Register(name="vec", width=128, count=8),
        ])),
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(metadata=Metadata(name="RType"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0,  width=7, role="opcode"),
        SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
        SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
        SchemaField(name="rs2",    start=17, width=5, role="register", type="gpr"),
    ])))
    reg.add(Schema(metadata=Metadata(name="IType"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0,  width=7, role="opcode"),
        SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
        SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
        SchemaField(name="imm",    start=17, width=12, role="immediate", type="signed"),
    ])))
    reg.add(Schema(metadata=Metadata(name="VType"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0,  width=7, role="opcode"),
        SchemaField(name="vd",     start=7,  width=5, role="register", type="vec"),
        SchemaField(name="vs1",    start=12, width=5, role="register", type="vec"),
        SchemaField(name="vs2",    start=17, width=5, role="register", type="vec"),
    ])))
    reg.add(Instruction(metadata=Metadata(name="ADD"), spec=InstructionSpec(
        **{"schema": "RType", "opcode": 0x33, "behavior": "rd = rs1 + rs2"})))
    reg.add(Instruction(metadata=Metadata(name="ADDI"), spec=InstructionSpec(
        **{"schema": "IType", "opcode": 0x13, "behavior": "rd = rs1 + imm"})))
    reg.add(Instruction(metadata=Metadata(name="VADD"), spec=InstructionSpec(
        **{"schema": "VType", "opcode": 0x57, "behavior": "vd = vs1 + vs2"})))
    reg.validate()
    registry = Registry()
    registry.isas["imm-isa"] = reg
    return registry


def test_c_register_operands_are_wired(tmp_path):
    """Regression: register source operands used to be dropped, leaving the asm
    with no inputs. They must appear as parameters and "r" constraints."""
    generate_software(_make_imm_registry(), str(tmp_path), "c")
    content = (tmp_path / "imm-isa_intrinsics.h").read_text()
    assert "isa_archive_add(uint32_t rs1, uint32_t rs2)" in content
    assert '"r"(rs1), "r"(rs2)' in content
    assert 'return rd;' in content


def test_c_immediate_instruction_is_macro(tmp_path):
    """An immediate must reach an "i" constraint as a literal → a macro."""
    generate_software(_make_imm_registry(), str(tmp_path), "c")
    content = (tmp_path / "imm-isa_intrinsics.h").read_text()
    assert "#define isa_archive_addi(rs1, imm)" in content
    assert '"i"(imm)' in content


def test_rust_immediate_is_const_generic(tmp_path):
    generate_software(_make_imm_registry(), str(tmp_path), "rust")
    content = (tmp_path / "imm-isa_intrinsics.rs").read_text()
    assert "isa_archive_addi<const imm: i16>" in content
    assert "const imm" in content


def test_non_scalar_register_instruction_skipped(tmp_path):
    """Vector-register ops can't be expressed as scalar inline-asm wrappers and
    are omitted (they need vector intrinsics, a separate capability)."""
    generate_software(_make_imm_registry(), str(tmp_path), "c")
    content = (tmp_path / "imm-isa_intrinsics.h").read_text()
    assert "isa_archive_vadd" not in content
