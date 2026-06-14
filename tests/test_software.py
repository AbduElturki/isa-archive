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
