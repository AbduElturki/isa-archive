import pytest
from isa_archive.models import Operand, OperandSpec, Metadata

def test_primitive_operand():
    op = Operand(
        metadata=Metadata(name="Reg"),
        spec=OperandSpec(width=32)
    )
    assert op.kind == "Operand"
    assert op.spec.width == 32

def test_struct_operand():
    # Test a nested struct: Rectangle -> Point -> x,y
    data = {
        "kind": "Operand",
        "metadata": {"name": "Rectangle"},
        "spec": {
            "width": 64,
            "fields": [
                {
                    "name": "top_left",
                    "start": 0,
                    "fields": [
                        {"name": "x", "width": 16, "start": 0},
                        {"name": "y", "width": 16, "start": 16}
                    ]
                }
            ]
        }
    }
    op = Operand(**data)
    assert len(op.spec.fields) == 1
    assert op.spec.fields[0].fields[0].name == "x"
