import logging
from enum import StrEnum
from ..compiler.loader import Registry
from ..compiler.utils import build_reg_maps, constraint_to_c
from ..models.enums import FieldRole
from .base import make_jinja_env, prepare_output_dir

logger = logging.getLogger("isa_archive.generators")


class SoftwareLang(StrEnum):
    C    = "c"
    RUST = "rust"


def generate_software(registry: Registry, output_dir: str, lang: SoftwareLang):
    env = make_jinja_env()

    def get_instr_register_map(instr, registry_isa):
        schema = registry_isa.schemas.get(instr.spec.schema_name)
        if not schema: return {}
        reg_map, _ = build_reg_maps(schema, registry_isa)
        return reg_map

    def get_instr_info(instr, registry_isa):
        schema = registry_isa.schemas.get(instr.spec.schema_name)
        if not schema: return {"sorted_ops": [], "asm_str": ""}

        op_details = []
        for field in schema.spec.fields:
            if field.is_fixed_value:
                continue

            constraint = "r"
            if field.role == FieldRole.IMMEDIATE:
                constraint = "i"

                op_details.append({
                    "name": field.name,
                    "constraint": constraint
                })

        def sort_key(op):
            order = {"rd": 0, "rs1": 1, "rs2": 2, "imm": 4}
            return order.get(op["name"], 10)

        sorted_ops = sorted(op_details, key=sort_key)

        if lang == SoftwareLang.C:
            asm_ops = [f"%{i}" for i in range(len(sorted_ops))]
            asm_str = f"{instr.metadata.name} {', '.join(asm_ops)}"
        else:
            asm_ops = [f"{{{i}}}" for i in range(len(sorted_ops))]
            asm_str = f"{instr.metadata.name} {', '.join(asm_ops)}"

        all_constraints = list(schema.spec.constraints) + list(instr.spec.constraints)
        constraints = [
            {"c_expr": constraint_to_c(c.expr), "message": c.message or c.expr}
            for c in all_constraints
        ]

        return {
            "sorted_ops": sorted_ops,
            "asm_str": asm_str,
            "constraints": constraints,
        }

    def calculate_mask(start: int, end: int) -> str:
        mask = ((1 << (end - start + 1)) - 1) << start
        return hex(mask)

    env.filters["calculate_mask"] = calculate_mask
    env.filters["instr_info"] = get_instr_info
    env.filters["constraint_to_c"] = constraint_to_c

    out_path = prepare_output_dir(output_dir)

    extension = "h" if lang == SoftwareLang.C else "rs"
    template_intrin = env.get_template(f"sw/intrinsics.{extension}.j2")
    template_struct = env.get_template(f"sw/structs.{extension}.j2")
    template_csr = env.get_template(f"sw/csrs.{extension}.j2")

    for isa_reg in registry.isas.values():
        output_intrin = template_intrin.render(instructions=isa_reg.instructions, isa_name=isa_reg.name, isa_reg=isa_reg)
        output_struct = template_struct.render(operands=isa_reg.operands, isa_name=isa_reg.name)
        output_csr = template_csr.render(csrs={}, isa_name=isa_reg.name, hex=hex)
        with open(out_path / f"{isa_reg.name}_intrinsics.{extension}", "w") as f: f.write(output_intrin)
        with open(out_path / f"{isa_reg.name}_structs.{extension}", "w") as f: f.write(output_struct)
        with open(out_path / f"{isa_reg.name}_csrs.{extension}", "w") as f: f.write(output_csr)

    logger.info(f"Generated {lang.upper()} software artifacts in {output_dir}")
