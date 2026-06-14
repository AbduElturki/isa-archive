import pytest
import pathlib
import yaml
from isa_archive.compiler.loader import load_isa, Registry

def test_isa_inheritance(tmp_path):
    # Create a base ISA file
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    
    base_isa_content = {
        "apiVersion": "isa-archive/v1",
        "kind": "ISA",
        "metadata": {"name": "base-isa"},
        "spec": {
            "name": "Base",
            "version": "1.0",
            "includes": ["ops.yaml"]
        }
    }
    base_ops_content = [
        {
            "apiVersion": "isa-archive/v1",
            "kind": "Operand",
            "metadata": {"name": "GPR"},
            "spec": {"width": 32}
        }
    ]
    
    with open(base_dir / "isa.yaml", "w") as f:
        yaml.dump(base_isa_content, f)
    with open(base_dir / "ops.yaml", "w") as f:
        yaml.dump_all(base_ops_content, f)
        
    # Create an extending ISA file
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    
    ext_isa_content = {
        "apiVersion": "isa-archive/v1",
        "kind": "ISA",
        "metadata": {"name": "ext-isa"},
        "spec": {
            "name": "Extension",
            "version": "1.0",
            "extends": "../base/isa.yaml",
            "includes": ["instr.yaml"]
        }
    }
    
    with open(ext_dir / "isa.yaml", "w") as f:
        yaml.dump(ext_isa_content, f)
    with open(ext_dir / "instr.yaml", "w") as f:
        yaml.dump_all([], f) # Empty instr file
        
    registry = Registry()
    ext_reg = load_isa(str(ext_dir / "isa.yaml"), registry)
    
    # Check if GPR from base ISA is present in the extending ISA's registry
    assert "GPR" in ext_reg.operands
    assert ext_reg.operands["GPR"].spec.width == 32
    assert ext_reg.name == "ext-isa"
