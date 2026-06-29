import logging
from ..compiler.loader import Registry, ISARegistry, uArchRegistry
from ..compiler.behavior import BehaviorIR
from ..compiler.backends import VerilogBackend
from ..compiler.utils import build_reg_maps, instruction_pattern
from ..models.enums import AccessMode
from .base import make_jinja_env, prepare_output_dir, write_generated

logger = logging.getLogger("isa_archive.generators")

def generate_verilog(registry: Registry, output_dir: str, clang_format: bool = False):
    env = make_jinja_env()
    def get_verilog_info(instr_obj, isa_reg):
        schema = isa_reg.schemas.get(instr_obj.spec.schema_name)
        reg_map, var_widths = build_reg_maps(schema, isa_reg)
        ir = BehaviorIR(
            instr_obj.spec.behavior,
            register_map=reg_map,
            var_widths=var_widths,
            operands=isa_reg.operands,
            csrs={}
        )
        return VerilogBackend(ir).translate()

    env.filters["parse_behavior"] = get_verilog_info

    out_path = prepare_output_dir(output_dir)

    # Generate for each ISA
    for isa_reg in registry.isas.values():
        # Generate Operands
        template_op = env.get_template("sv/operands.sv.j2")
        output_op = template_op.render(operands=isa_reg.operands)
        write_generated(out_path / f"{isa_reg.name}_operands.sv", output_op)

    # Generate for each uArch
    for uarch_reg in registry.uarches.values():
        isa_reg = uarch_reg.isa

        # Generate Blocks
        template_block = env.get_template("sv/block.sv.j2")

        blocks_data = []

        for block in uarch_reg.blocks:
            handled_instrs = {
                name: instr for name, instr in isa_reg.instructions.items()
                if instr.spec.exec_type in block.handles
            }
            if not handled_instrs:
                logger.warning(
                    f"Block '{block.name}' handles {block.handles} but no instructions have a matching "
                    f"exec_type - skipping. Check exec_type on your instructions."
                )
                continue

            read_ports = {}
            write_ports = {}
            max_instr_len = isa_reg.xlen
            patterns = {}

            for instr_name, instr in handled_instrs.items():
                schema = isa_reg.schemas.get(instr.spec.schema_name)
                if schema and schema.spec.length > max_instr_len:
                    max_instr_len = schema.spec.length

                patterns[instr_name] = instruction_pattern(instr, schema, fill="?") if schema else "?" * isa_reg.xlen

                reg_map, var_widths = build_reg_maps(schema, isa_reg)
                ir = BehaviorIR(
                    instr.spec.behavior,
                    register_map=reg_map,
                    var_widths=var_widths,
                    operands=isa_reg.operands,
                    csrs={}
                )

                for var in ir.read_vars:
                    if var in reg_map:
                        read_ports[f"{var}_val"] = var_widths.get(var, isa_reg.xlen)
                    elif var == "pc":
                        read_ports["pc"] = isa_reg.xlen
                for var in ir.write_vars:
                    if var in reg_map:
                        write_ports[f"{var}_val"] = var_widths.get(var, isa_reg.xlen)
                    elif var == "pc":
                        write_ports["_pc_write"] = isa_reg.xlen

            has_pc_write = write_ports.pop("_pc_write", None) is not None
            blocks_data.append({
                "name": block.name,
                "count": block.count,
                "read_ports": read_ports,
                "write_ports": write_ports,
                "max_instr_len": max_instr_len,
                "has_pc_write": has_pc_write,
            })

            imm_fields_by_instr = {
                instr_name: [
                    f for f in (isa_reg.schemas.get(instr.spec.schema_name).spec.fields
                                if isa_reg.schemas.get(instr.spec.schema_name) else [])
                    if f.role == "immediate"
                ]
                for instr_name, instr in handled_instrs.items()
            }
            output_block = template_block.render(
                instructions=handled_instrs,
                isa_reg=isa_reg,
                uarch_name=uarch_reg.name,
                block_name=block.name,
                read_ports=read_ports,
                write_ports=write_ports,
                max_instr_len=max_instr_len,
                patterns=patterns,
                xlen=isa_reg.xlen,
                has_pc_write=has_pc_write,
                imm_fields_by_instr=imm_fields_by_instr,
            )
            write_generated(out_path / f"{uarch_reg.name}_{block.name}.sv", output_block)

        # Generate Top
        template_top = env.get_template("sv/top.sv.j2")
        output_top = template_top.render(
            blocks=blocks_data,
            uarch_name=uarch_reg.name,
            isa_name=uarch_reg.isa.name,
            xlen=uarch_reg.isa.xlen,
        )
        write_generated(out_path / f"{uarch_reg.name}_top.sv", output_top)

    logger.info(f"Generated SV artifacts in {output_dir}")
