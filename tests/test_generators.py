import pytest
from isa_archive.models import (
    ISA, ISASpec, ISAState, Register, Metadata,
    Schema, SchemaSpec, Instruction, InstructionSpec,
    EnumDef, EnumDefSpec,
)
from isa_archive.models.schema import SchemaField
from isa_archive.compiler.loader import ISARegistry, Registry
from isa_archive.generators.sv import generate_verilog
from isa_archive.generators.qemu import generate_qemu_isa as generate_qemu


def _make_registry() -> Registry:
    """Minimal registry: one GPR file, one R-type schema, ADD instruction."""
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


def test_generate_verilog_creates_files(tmp_path):
    registry = _make_registry()
    generate_verilog(registry, str(tmp_path))
    assert (tmp_path / "test-isa_operands.sv").exists()


def test_generate_verilog_contains_expected_fragments(tmp_path):
    registry = _make_registry()
    generate_verilog(registry, str(tmp_path))
    # Operands file is generated even if empty; just verify it's created
    assert (tmp_path / "test-isa_operands.sv").exists()


def test_generate_qemu_creates_all_files(tmp_path):
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    assert (tmp_path / "test-isa.decode").exists()
    assert (tmp_path / "test-isa_helpers.c").exists()
    assert (tmp_path / "test-isa_helper.h").exists()
    assert (tmp_path / "test-isa_trans.c.inc").exists()
    assert (tmp_path / "test-isa_arch.h").exists()


def test_generate_qemu_decode_contains_instruction(tmp_path):
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    decode_content = (tmp_path / "test-isa.decode").read_text()
    assert "add" in decode_content.lower()


def test_generate_qemu_helpers_contains_instruction(tmp_path):
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    helpers = (tmp_path / "test-isa_helpers.c").read_text()
    assert "ADD" in helpers or "add" in helpers.lower()


def test_generate_qemu_signed_immediate_in_decode(tmp_path):
    """Signed immediate fields must emit the s-modifier in decodetree."""
    manifest = ISA(
        metadata=Metadata(name="imm-isa"),
        spec=ISASpec(
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="IType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
            SchemaField(name="imm",    start=17, width=12, role="immediate", type="signed"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="ADDI"),
        spec=InstructionSpec(**{"schema": "IType", "opcode": 0x13, "behavior": "rd = rs1 + imm"})
    ))
    reg.validate()
    registry = Registry()
    registry.isas["imm-isa"] = reg
    generate_qemu(registry, str(tmp_path))
    decode = (tmp_path / "imm-isa.decode").read_text()
    assert "%IType_imm 17:s12" in decode


def test_generate_qemu_unsigned_immediate_in_decode(tmp_path):
    """Unsigned immediate fields must NOT have the s-modifier."""
    manifest = ISA(
        metadata=Metadata(name="uimm-isa"),
        spec=ISASpec(
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="UType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="imm",    start=12, width=20, role="immediate"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="LUI"),
        spec=InstructionSpec(**{"schema": "UType", "opcode": 0x37, "behavior": "rd = imm"})
    ))
    with pytest.raises(ValueError):  # rd(32-bit) = imm(20-bit) → width mismatch
        reg.validate()


def test_generate_qemu_arch_h_created(tmp_path):
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    assert (tmp_path / "test-isa_arch.h").exists()
    content = (tmp_path / "test-isa_arch.h").read_text()
    assert "ArchState" in content
    assert "gpr[32]" in content


def test_generate_qemu_zero_register_guard_in_trans(tmp_path):
    """trans_ functions must early-return true when rd is the zero register."""
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    trans = (tmp_path / "test-isa_trans.c.inc").read_text()
    assert "a->rd == 0" in trans


def test_generate_qemu_zero_register_guard_in_helper(tmp_path):
    """HELPER functions must early-return when rd is the zero register."""
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    helpers = (tmp_path / "test-isa_helpers.c").read_text()
    assert "rd == 0" in helpers


def _make_branch_registry(name: str, behavior: str) -> Registry:
    """Registry with a minimal branch instruction using the given behavior."""
    manifest = ISA(
        metadata=Metadata(name=name),
        spec=ISASpec(
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="BType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rs1",    start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="rs2",    start=12, width=5, role="register", type="gpr"),
            SchemaField(name="imm",    start=17, width=12, role="immediate", type="signed"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="BEQ"),
        spec=InstructionSpec(**{"schema": "BType", "opcode": 0x63, "behavior": behavior})
    ))
    reg.validate()
    registry = Registry()
    registry.isas[name] = reg
    return registry


def test_generate_qemu_conditional_branch_fall_through(tmp_path):
    """Conditional branch HELPER must advance env->pc before the conditional override."""
    registry = _make_branch_registry("cond-isa", "if (rs1 == rs2):\n    pc = pc + imm")
    generate_qemu(registry, str(tmp_path))
    helpers = (tmp_path / "cond-isa_helpers.c").read_text()
    assert "if (!_branch_taken) env->pc = (env->pc + " in helpers
    trans = (tmp_path / "cond-isa_trans.c.inc").read_text()
    assert "DISAS_TARGET_0" in trans


def test_generate_qemu_unconditional_jump_noreturn(tmp_path):
    """Unconditional jump must emit DISAS_NORETURN."""
    manifest = ISA(
        metadata=Metadata(name="uj-isa"),
        spec=ISASpec(
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="JType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
            SchemaField(name="imm",    start=17, width=12, role="immediate", type="signed"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="JALR"),
        spec=InstructionSpec(**{"schema": "JType", "opcode": 0x67, "behavior": "rd = pc + 4\npc = rs1 + imm"})
    ))
    reg.validate()
    registry = Registry()
    registry.isas["uj-isa"] = reg
    generate_qemu(registry, str(tmp_path))
    trans = (tmp_path / "uj-isa_trans.c.inc").read_text()
    assert "DISAS_NORETURN" in trans
    assert "DISAS_CHAIN" not in trans


def test_schema_field_enum_attribute():
    """Fields with role='constant' and type= are secondary discriminators."""
    f = SchemaField(name="funct3", start=12, width=3, role="constant", type="enum.F3_ALU")
    assert f.type == "enum.F3_ALU"
    assert f.enum_ref == "F3_ALU"
    assert f.is_constant
    assert f.is_fixed_value
    assert not f.is_reserved


def test_schema_field_requires_role():
    """Fields without role must be rejected by Pydantic (role is required)."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        SchemaField(name="funct3", start=12, width=3)


def test_schema_field_role_fixed_not_in_decode_fields(tmp_path):
    """Opcode and constant fields must not appear as decodetree field references."""
    registry = _make_registry()
    generate_qemu(registry, str(tmp_path))
    decode = (tmp_path / "test-isa.decode").read_text()
    assert "%RType_opcode" not in decode
    assert "%RType_funct3" not in decode
    assert "%RType_funct7" not in decode
    assert "%RType_rd" in decode
    assert "%RType_rs1" in decode
    assert "%RType_rs2" in decode


def test_loader_rejects_setting_non_fixed_field_as_fixed():
    """Instruction.constants entries must reference fields with role='opcode' or role='constant'."""
    manifest = ISA(
        metadata=Metadata(name="bad-isa2"),
        spec=ISASpec(
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="Bad"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
            SchemaField(name="rs2",    start=17, width=5, role="register", type="gpr"),
        ])
    ))
    # rd is role="register", not a fixed-value field — must be rejected
    reg.add(Instruction(
        metadata=Metadata(name="BAD2"),
        spec=InstructionSpec(**{"schema": "Bad", "opcode": 0x33, "constants": {"rd": 0}, "behavior": "rd = rs1 + rs2"})
    ))
    with pytest.raises(ValueError, match="must be a role='opcode' or role='constant' field"):
        reg.validate()


def test_loader_rejects_schema_without_fixed_field():
    """Every schema used by an instruction must have at least one role='opcode' field."""
    manifest = ISA(
        metadata=Metadata(name="no-fixed-isa"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=32)]))
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="NoFixed"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="rd",  start=0,  width=5, role="register", type="gpr"),
            SchemaField(name="rs1", start=5,  width=5, role="register", type="gpr"),
            SchemaField(name="imm", start=10, width=12, role="immediate"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="FOO"),
        spec=InstructionSpec(**{"schema": "NoFixed", "opcode": 0, "behavior": "rd = rs1 + imm"})
    ))
    with pytest.raises(ValueError, match="no field with role='opcode'"):
        reg.validate()


def test_generate_verilog_behavior_width_mismatch_raises(tmp_path):
    """Assigning a 12-bit immediate directly to a 32-bit register must raise ValueError."""
    manifest = ISA(
        metadata=Metadata(name="bad-isa"),
        spec=ISASpec(
            name="BadISA",
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32)])
        )
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="S"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
            SchemaField(name="imm",    start=17, width=12, role="immediate"),
        ])
    ))
    # rd is 32-bit (gpr), imm is 12-bit immediate → width mismatch 32 != 12
    reg.add(Instruction(
        metadata=Metadata(name="BAD"),
        spec=InstructionSpec(**{"schema": "S", "opcode": 0x13, "behavior": "rd = imm"})
    ))
    with pytest.raises(ValueError):
        reg.validate()


def _make_constrained_registry() -> Registry:
    """Registry with a schema-level and an instruction-level constraint."""
    manifest = ISA(
        metadata=Metadata(name="con-isa"),
        spec=ISASpec(
            version="1.0",
            state=ISAState(registers=[Register(name="gpr", width=32, count=32, zero_register=0)])
        )
    )
    from isa_archive.models import Constraint
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="ShiftType"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="rs1",    start=12, width=5, role="register", type="gpr"),
            SchemaField(name="shamt",  start=17, width=5, role="immediate"),
        ], constraints=[
            Constraint(expr="shamt < 32", message="shift amount must be less than 32"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="SLLI"),
        spec=InstructionSpec(**{
            "schema": "ShiftType",
            "opcode": 0x13,
            "behavior": "rd = rs1 << shamt",
            "constraints": [Constraint(expr="rs1 != 0", message="rs1 must not be zero register")],
        })
    ))
    reg.validate()
    registry = Registry()
    registry.isas["con-isa"] = reg
    return registry


def test_constraint_schema_level_in_qemu_trans(tmp_path):
    """Schema-level constraints appear as guards in the trans_ function."""
    registry = _make_constrained_registry()
    generate_qemu(registry, str(tmp_path))
    trans = (tmp_path / "con-isa_trans.c.inc").read_text()
    assert "a->shamt < 32" in trans
    assert "shift amount must be less than 32" in trans


def test_constraint_instruction_level_in_qemu_trans(tmp_path):
    """Instruction-level constraints also appear in trans_, additive to schema constraints."""
    registry = _make_constrained_registry()
    generate_qemu(registry, str(tmp_path))
    trans = (tmp_path / "con-isa_trans.c.inc").read_text()
    assert "a->rs1 != 0" in trans
    assert "rs1 must not be zero register" in trans


def test_constraint_in_c_intrinsics(tmp_path):
    """Constraints appear as assert() calls in C intrinsics."""
    from isa_archive.generators.software import generate_software
    registry = _make_constrained_registry()
    generate_software(registry, str(tmp_path), "c")
    content = (tmp_path / "con-isa_intrinsics.h").read_text()
    assert "assert(" in content
    assert "shamt < 32" in content


def test_constraint_in_rust_intrinsics(tmp_path):
    """Constraints appear as assert!() calls in Rust intrinsics."""
    from isa_archive.generators.software import generate_software
    registry = _make_constrained_registry()
    generate_software(registry, str(tmp_path), "rust")
    content = (tmp_path / "con-isa_intrinsics.rs").read_text()
    assert "assert!" in content
    assert "shamt < 32" in content


def test_constraint_invalid_syntax_raises():
    """An invalid expression in a constraint must be caught at load time."""
    from isa_archive.models import Constraint
    manifest = ISA(
        metadata=Metadata(name="bad-con"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=32)]))
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="S"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0, width=7, role="opcode"),
            SchemaField(name="rd", start=7, width=5, role="register", type="gpr"),
            SchemaField(name="rs1", start=12, width=5, role="register", type="gpr"),
        ], constraints=[Constraint(expr="rd !!= 0")])  # invalid syntax
    ))
    reg.add(Instruction(
        metadata=Metadata(name="NOP"),
        spec=InstructionSpec(**{"schema": "S", "opcode": 0x13, "behavior": "rd = rs1"})
    ))
    with pytest.raises(ValueError, match="invalid constraint"):
        reg.validate()


def test_constraint_string_shorthand():
    """A plain string in constraints is accepted as the expr with no message."""
    from isa_archive.models import Constraint
    c = Constraint.model_validate("shamt < 32")
    assert c.expr == "shamt < 32"
    assert c.message is None


def test_schema_field_role_opcode():
    """role='opcode' sets is_opcode=True and is_fixed_value=True."""
    f = SchemaField(name="op", start=0, width=8, role="opcode")
    assert f.role == "opcode"
    assert f.is_opcode
    assert f.is_fixed_value
    assert not f.is_reserved
    assert not f.is_constant


def test_schema_field_role_constant_with_type():
    """role='constant' with type= is a secondary discriminator field."""
    f = SchemaField(name="funct3", start=12, width=3, role="constant", type="enum.F3_ALU")
    assert f.is_constant
    assert f.is_fixed_value
    assert f.type == "enum.F3_ALU"
    assert f.enum_ref == "F3_ALU"
    assert not f.is_opcode


def test_loader_rejects_opcode_collision():
    """Two instructions with the same opcode pattern must be rejected."""
    manifest = ISA(
        metadata=Metadata(name="col-isa"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=32)]))
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="S"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0, width=8, role="opcode"),
            SchemaField(name="rd",     start=8, width=5, role="register", type="gpr"),
            SchemaField(name="rs1",    start=13, width=5, role="register", type="gpr"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="FOO"),
        spec=InstructionSpec(**{"schema": "S", "opcode": 0x01, "behavior": "rd = rs1"})
    ))
    reg.add(Instruction(
        metadata=Metadata(name="BAR"),
        spec=InstructionSpec(**{"schema": "S", "opcode": 0x01, "behavior": "rd = rs1"})
    ))
    with pytest.raises(ValueError, match="Decoder Collision"):
        reg.validate()


def test_schema_field_role_reserved():
    """role='reserved' has is_reserved=True and is excluded from variable fields."""
    f = SchemaField(name="res", start=5, width=2, role="reserved")
    assert f.role == "reserved"
    assert f.is_reserved
    assert f.is_fixed_value
    assert not f.is_opcode
    assert not f.is_constant


def test_loader_reserved_field_not_required_in_fixed():
    """Reserved fields must not appear in and are not required in the fixed dict."""
    manifest = ISA(
        metadata=Metadata(name="res-isa"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=32)]))
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="S"),
        spec=SchemaSpec(length=32, fields=[
            SchemaField(name="opcode", start=0,  width=7, role="opcode"),
            SchemaField(name="rd",     start=7,  width=5, role="register", type="gpr"),
            SchemaField(name="res",    start=12, width=3, role="reserved"),
            SchemaField(name="rs1",    start=15, width=5, role="register", type="gpr"),
            SchemaField(name="rs2",    start=20, width=5, role="register", type="gpr"),
            SchemaField(name="pad",    start=25, width=7, role="reserved"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="ADD"),
        spec=InstructionSpec(**{"schema": "S", "opcode": 0x33, "behavior": "rd = rs1 + rs2"})
    ))
    reg.validate()  # must not raise — reserved fields require no value


def test_loader_reserved_field_encoded_as_zeros_in_pattern(tmp_path):
    """Reserved bits appear as 0s in the QEMU decodetree pattern."""
    manifest = ISA(
        metadata=Metadata(name="res-isa2"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=16)]))
    )
    reg = ISARegistry(manifest)
    reg.add(Schema(
        metadata=Metadata(name="S"),
        spec=SchemaSpec(length=16, fields=[
            SchemaField(name="opcode", start=0, width=4, role="opcode"),
            SchemaField(name="rd",     start=4, width=4, role="register", type="gpr"),
            SchemaField(name="rs1",    start=8, width=4, role="register", type="gpr"),
            SchemaField(name="res",    start=12, width=4, role="reserved"),
        ])
    ))
    reg.add(Instruction(
        metadata=Metadata(name="OP"),
        spec=InstructionSpec(**{"schema": "S", "opcode": 0x1, "behavior": "rd = rs1"})
    ))
    reg.validate()
    registry = Registry()
    registry.isas["res-isa2"] = reg
    generate_qemu(registry, str(tmp_path))
    decode = (tmp_path / "res-isa2.decode").read_text()
    # The 4 reserved bits [15:12] must be 0000 in the pattern
    assert "0000" in decode


def _make_operand_registry() -> Registry:
    """Registry with a struct operand that has constraints."""
    from isa_archive.models import Constraint
    from isa_archive.models.operand import Operand, OperandSpec, OperandField
    manifest = ISA(
        metadata=Metadata(name="op-isa"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=32)]))
    )
    reg = ISARegistry(manifest)
    reg.add(Operand(
        metadata=Metadata(name="Vec2"),
        spec=OperandSpec(
            width=32,
            fields=[OperandField(name="lo", start=0, width=16), OperandField(name="hi", start=16, width=16)],
            constraints=[Constraint(expr="lo != hi", message="Vec2 lanes must be distinct")],
        )
    ))
    registry = Registry()
    registry.isas["op-isa"] = reg
    return registry


def test_operand_constraint_in_c_struct(tmp_path):
    """Operand constraints appear as assert() calls in the C struct constructor."""
    from isa_archive.generators.software import generate_software
    registry = _make_operand_registry()
    generate_software(registry, str(tmp_path), "c")
    content = (tmp_path / "op-isa_structs.h").read_text()
    assert "assert(" in content
    assert "lo != hi" in content
    assert "Vec2 lanes must be distinct" in content


def test_operand_constraint_in_rust_struct(tmp_path):
    """Operand constraints appear as assert!() calls in the Rust struct new()."""
    from isa_archive.generators.software import generate_software
    registry = _make_operand_registry()
    generate_software(registry, str(tmp_path), "rust")
    content = (tmp_path / "op-isa_structs.rs").read_text()
    assert "assert!" in content
    assert "lo != hi" in content
    assert "Vec2 lanes must be distinct" in content


def test_operand_constraint_invalid_syntax_raises():
    """An invalid constraint expression on an operand is caught at load time."""
    from isa_archive.models import Constraint
    from isa_archive.models.operand import Operand, OperandSpec, OperandField
    manifest = ISA(
        metadata=Metadata(name="bad-op-isa"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[]))
    )
    reg = ISARegistry(manifest)
    reg.add(Operand(
        metadata=Metadata(name="Bad"),
        spec=OperandSpec(
            width=16,
            fields=[OperandField(name="x", start=0, width=16)],
            constraints=[Constraint(expr="x !!= 0")],
        )
    ))
    with pytest.raises(ValueError, match="invalid constraint"):
        reg.validate()


def test_loader_allows_fixed_field_collision():
    """Two instructions may share an opcode but differ by constant field values (RISC-V pattern)."""
    manifest = ISA(
        metadata=Metadata(name="risc-isa"),
        spec=ISASpec(version="1.0", state=ISAState(registers=[Register(name="gpr", width=32, count=32)]))
    )
    reg = ISARegistry(manifest)
    reg.add(EnumDef(metadata=Metadata(name="F3"),    spec=EnumDefSpec(width=3, values={"ZERO": 0})))
    reg.add(EnumDef(metadata=Metadata(name="F7"),    spec=EnumDefSpec(width=7, values={"BASE": 0x00, "ALT": 0x20})))
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
        spec=InstructionSpec(**{"schema": "RType", "opcode": 0x33, "constants": {"funct3": 0, "funct7": 0x00}, "behavior": "rd = rs1 + rs2"})
    ))
    reg.add(Instruction(
        metadata=Metadata(name="SUB"),
        spec=InstructionSpec(**{"schema": "RType", "opcode": 0x33, "constants": {"funct3": 0, "funct7": 0x20}, "behavior": "rd = rs1 - rs2"})
    ))
    reg.validate()  # must not raise — distinct funct7 prevents decoder collision
