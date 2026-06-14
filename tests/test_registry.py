import pytest
from isa_archive.models import (
    ISA, ISASpec, ISAState, Register, ISACSR, CSRField,
    Metadata, Constant, ConstantSpec, EnumDef, EnumDefSpec,
    Instruction, InstructionSpec, Schema, SchemaSpec,
)
from isa_archive.models.schema import SchemaField
from isa_archive.compiler.loader import ISARegistry

def test_constant_resolution():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1"))
    reg = ISARegistry(isa_manifest)

    reg.add(Constant(metadata=Metadata(name="MY_CONST"), spec=ConstantSpec(value=0x42, width=8)))

    val = reg._resolve_value("MY_CONST")
    assert val == 0x42

def test_enum_resolution():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1"))
    reg = ISARegistry(isa_manifest)

    reg.add(EnumDef(metadata=Metadata(name="OP"), spec=EnumDefSpec(width=4, values={"ADD": 1, "SUB": 2})))

    assert reg._resolve_value("OP.ADD") == 1
    assert reg._resolve_value("OP.SUB") == 2

def test_instruction_validation_with_resolution():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1", state=ISAState(
        registers=[Register(name="gpr", width=32, count=32)]
    )))
    reg = ISARegistry(isa_manifest)

    reg.add(Schema(metadata=Metadata(name="R"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0, width=8, role="opcode"),
        SchemaField(name="rd",  start=8,  width=5, role="register", type="gpr"),
        SchemaField(name="rs1", start=13, width=5, role="register", type="gpr"),
        SchemaField(name="rs2", start=18, width=5, role="register", type="gpr"),
    ])))

    reg.add(Constant(metadata=Metadata(name="MY_OP"), spec=ConstantSpec(value=0x33, width=8)))

    instr = Instruction(metadata=Metadata(name="ADD"), spec=InstructionSpec(
        **{"schema": "R", "opcode": "MY_OP", "behavior": "rd = rs1 + rs2"}
    ))
    reg.add(instr)

    reg.validate()

    assert instr.spec.opcode == 0x33

def test_schema_consistency_overlap():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1"))
    reg = ISARegistry(isa_manifest)
    reg.add(Schema(metadata=Metadata(name="BadSchema"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="f1", start=0, width=6, role="immediate"),
        SchemaField(name="f2", start=4, width=7, role="immediate"),  # overlaps at bits 4,5
    ])))
    with pytest.raises(ValueError, match="overlaps with another field"):
        reg.validate()

def test_schema_consistency_bounds():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1"))
    reg = ISARegistry(isa_manifest)
    reg.add(Schema(metadata=Metadata(name="BadSchema"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="f1", start=10, width=-5, role="immediate"),  # width -5 → end = 10 + (-5) - 1 = 4 < start
    ])))
    with pytest.raises(ValueError, match="invalid bounds"):
        reg.validate()

def test_register_addressing_too_narrow():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1", state=ISAState(
        registers=[Register(name="gpr", width=32, count=32)]
    )))
    reg = ISARegistry(isa_manifest)
    reg.add(Schema(metadata=Metadata(name="S"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="rd", start=0, width=4, role="register", type="gpr"),  # 4 bits can't address 32 registers
    ])))
    with pytest.raises(ValueError, match="too narrow to address all 32 registers"):
        reg.validate()

def test_decoder_collision():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1", state=ISAState(
        registers=[Register(name="gpr", width=32, count=32)]
    )))
    reg = ISARegistry(isa_manifest)
    reg.add(Schema(metadata=Metadata(name="R"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0, width=7, role="opcode"),
        SchemaField(name="rd", start=7, width=5, role="register", type="gpr"),
    ])))
    reg.add(Instruction(metadata=Metadata(name="INST1"), spec=InstructionSpec(
        **{"schema": "R", "opcode": 0x33, "behavior": "rd = 1"}
    )))
    reg.add(Instruction(metadata=Metadata(name="INST2"), spec=InstructionSpec(
        **{"schema": "R", "opcode": 0x33, "behavior": "rd = 2"}
    )))
    with pytest.raises(ValueError, match="Decoder Collision"):
        reg.validate()

def test_incomplete_opcode_definitions():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1"))
    reg = ISARegistry(isa_manifest)
    reg.add(EnumDef(metadata=Metadata(name="F3"), spec=EnumDefSpec(width=3, values={"ZERO": 0})))
    reg.add(Schema(metadata=Metadata(name="S"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0, width=7, role="opcode"),
        SchemaField(name="funct3", start=12, width=3, role="constant", type="enum.F3"),
    ])))
    reg.add(Instruction(metadata=Metadata(name="INST1"), spec=InstructionSpec(
        **{"schema": "S", "opcode": 0x33, "behavior": "opcode = 1"}
    )))  # missing funct3!
    with pytest.raises(ValueError, match="missing values for fixed fields"):
        reg.validate()

def test_csr_address_collision():
    isa_manifest = ISA(metadata=Metadata(name="test-isa"), spec=ISASpec(version="1", state=ISAState(
        csrs=[
            ISACSR(name="mstatus", address=0x300, width=32),
            ISACSR(name="mie", address=0x300, width=32),  # collision!
        ]
    )))
    reg = ISARegistry(isa_manifest)
    with pytest.raises(ValueError, match="CSR Address Collision: 'mie' and 'mstatus' both use address 0x300"):
        reg.validate()

def test_single_bit_field():
    field = SchemaField(name="imm_11", start=7, width=1, role="immediate")
    assert field.width == 1
    assert field.start == 7
    assert field.end == 7
    assert field.is_operand is True

def test_display_name_falls_back_to_metadata():
    isa_manifest = ISA(metadata=Metadata(name="my-isa"), spec=ISASpec(version="1"))
    reg = ISARegistry(isa_manifest)
    assert reg.display_name == "my-isa"

def test_display_name_uses_spec_name_when_set():
    isa_manifest = ISA(metadata=Metadata(name="my-isa"), spec=ISASpec(name="My ISA", version="1"))
    reg = ISARegistry(isa_manifest)
    assert reg.display_name == "My ISA"

def test_schema_opcode_width_mismatch_warns(caplog):
    """Schemas with different opcode field widths emit a warning."""
    import logging
    manifest = ISA(
        metadata=Metadata(name="mixed-isa"),
        spec=ISASpec(version="1", state=ISAState(registers=[
            Register(name="gpr",  width=32, count=32),
            Register(name="cgpr", width=32, count=16),
        ]))
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(metadata=Metadata(name="Wide"), spec=SchemaSpec(length=32, fields=[
        SchemaField(name="opcode", start=0, width=7, role="opcode"),
        SchemaField(name="rd",     start=7, width=5, role="register", type="gpr"),
    ])))
    reg.add(Schema(metadata=Metadata(name="Narrow"), spec=SchemaSpec(length=16, fields=[
        SchemaField(name="opcode", start=0, width=4, role="opcode"),
        SchemaField(name="rd",     start=4, width=4, role="register", type="cgpr"),
    ])))
    reg.add(Instruction(metadata=Metadata(name="W1"), spec=InstructionSpec(
        **{"schema": "Wide", "opcode": 0x33, "behavior": "rd = 1"}
    )))
    reg.add(Instruction(metadata=Metadata(name="N1"), spec=InstructionSpec(
        **{"schema": "Narrow", "opcode": 0x1, "behavior": "rd = 2"}
    )))
    with caplog.at_level(logging.WARNING, logger="isa_archive.validator"):
        reg.validate()
    assert any("inconsistent opcode field widths" in r.message for r in caplog.records)
