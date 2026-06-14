import logging
import pathlib
from ..compiler.loader import Registry, ISARegistry
from ..models.enums import FieldRole
from ..compiler.behavior import BehaviorIR
from ..compiler.utils import build_reg_maps
from .base import make_jinja_env, prepare_output_dir

logger = logging.getLogger("isa_archive.generators")


def _parse_imm_range(field_name: str) -> tuple[int, int] | None:
    """Parse 'imm_X_Y' or 'imm_X' → (high_bit, low_bit). Returns None if not a split field."""
    if not field_name.startswith("imm_"):
        return None
    suffix = field_name[4:]
    parts = suffix.split("_")
    try:
        if len(parts) == 1:
            b = int(parts[0])
            return (b, b)
        elif len(parts) == 2:
            return (int(parts[0]), int(parts[1]))
    except ValueError:
        pass
    return None


def _build_instr_info(instr, schema, isa_reg: ISARegistry) -> dict:
    """Build encoding descriptor for one instruction (passed to the assembler template)."""
    bytes_len = schema.spec.length // 8

    # Fixed bits: opcode + constant fields
    fixed_value = 0
    fixed_names = set()
    instr_fixed = {"opcode": instr.spec.opcode}
    instr_fixed.update(instr.spec.constants)
    for field in schema.spec.fields:
        if field.name in instr_fixed:
            val = instr_fixed[field.name]
            fixed_value |= (int(val) & ((1 << field.width) - 1)) << field.start
            fixed_names.add(field.name)
        elif field.role == FieldRole.RESERVED:
            fixed_names.add(field.name)

    # Variable fields
    reg_fields = []
    imm_subfields = []
    has_split_imm = False

    for field in schema.spec.fields:
        if field.name in fixed_names:
            continue
        if field.role == FieldRole.REGISTER:
            reg_fields.append({
                "name": field.name,
                "start": field.start,
                "width": field.width,
                "mask": (1 << field.width) - 1,
            })
        elif field.role == FieldRole.IMMEDIATE:
            if field.name == "imm":
                imm_subfields.append({
                    "name": "imm",
                    "start": field.start,
                    "high": field.width - 1,
                    "low": 0,
                    "mask": (1 << field.width) - 1,
                })
            else:
                parsed = _parse_imm_range(field.name)
                if parsed:
                    high, low = parsed
                    has_split_imm = True
                else:
                    high, low = field.width - 1, 0
                imm_subfields.append({
                    "name": field.name,
                    "start": field.start,
                    "high": high,
                    "low": low,
                    "mask": (1 << (high - low + 1)) - 1,
                })

    has_imm = bool(imm_subfields)
    imm_total_bits = 0
    if has_imm:
        imm_total_bits = max(sf["high"] for sf in imm_subfields) + 1

    is_signed = any(
        f.type == "signed"
        for f in schema.spec.fields
        if f.role == FieldRole.IMMEDIATE
    )

    # Detect PC-modification via BehaviorIR
    reg_map, var_widths = build_reg_maps(schema, isa_reg)
    try:
        ir = BehaviorIR(
            instr.spec.behavior,
            register_map=reg_map,
            var_widths=var_widths,
            operands=isa_reg.operands,
            csrs={},
        )
        modifies_pc = ir.modifies_pc
    except Exception:
        modifies_pc = False

    exec_type = instr.spec.exec_type or ""
    is_mem_load = "mem_load" in exec_type
    is_mem_store = "mem_store" in exec_type

    # PC-relative: branches and jumps with split imm encoding (BType, JType)
    # Register-relative: JALR-style (single contiguous imm)
    is_pc_relative = modifies_pc and has_split_imm

    # Build assembly operand descriptor list
    reg_names = [r["name"] for r in reg_fields]
    asm_operands = []

    if is_mem_load:
        dest = next((r for r in reg_names if r == "rd"), reg_names[0] if reg_names else "rd")
        base = next((r for r in reg_names if r in ("rs1", "base")), "rs1")
        asm_operands = [f"r:{dest}", f"m:imm:{base}"]
    elif is_mem_store:
        src = next((r for r in reg_names if r in ("rs2", "src")), reg_names[0] if reg_names else "rs2")
        base = next((r for r in reg_names if r in ("rs1", "base")), "rs1")
        asm_operands = [f"r:{src}", f"m:imm:{base}"]
    else:
        for priority in ["rd", "rs1", "rs2", "rs3"]:
            if priority in reg_names:
                asm_operands.append(f"r:{priority}")
        for r in reg_names:
            if f"r:{r}" not in asm_operands:
                asm_operands.append(f"r:{r}")
        if has_imm:
            asm_operands.append("l:imm" if is_pc_relative else "i:imm")

    # Derive Python function parameter list from asm_operands
    params = []
    for op in asm_operands:
        parts = op.split(":")
        if parts[0] in ("r", "i", "l"):
            params.append(parts[1])
        elif parts[0] == "m":
            params.append(parts[1])  # imm
            params.append(parts[2])  # base register

    # Remove params that map to the same name as a reg field that isn't in asm_operands
    # (handles cases where mem operand uses a reg field name directly)
    unique_params = list(dict.fromkeys(params))

    imm_mask = (1 << imm_total_bits) - 1 if imm_total_bits > 0 else 0

    return {
        "name": instr.metadata.name,
        "mnemonic": instr.metadata.name.lower(),
        "bytes": bytes_len,
        "fixed_hex": f"0x{fixed_value:08x}",
        "reg_fields": reg_fields,
        "imm_subfields": imm_subfields,
        "imm_total_bits": imm_total_bits,
        "imm_mask": f"0x{imm_mask:x}" if imm_total_bits > 0 else "0x0",
        "imm_signed": is_signed,
        "has_imm": has_imm,
        "has_split_imm": has_split_imm,
        "modifies_pc": modifies_pc,
        "is_pc_relative": is_pc_relative,
        "is_mem_load": is_mem_load,
        "is_mem_store": is_mem_store,
        "asm_operands": asm_operands,
        "params": unique_params,
        "param_list": ", ".join(unique_params),
    }


def generate_asm(registry: Registry, output_dir: str):
    env = make_jinja_env()
    out_path = prepare_output_dir(output_dir)

    for isa_reg in registry.isas.values():
        instr_infos = []
        for instr in isa_reg.instructions.values():
            schema = isa_reg.schemas.get(instr.spec.schema_name)
            if not schema:
                continue
            instr_infos.append(_build_instr_info(instr, schema, isa_reg))
        instr_infos.sort(key=lambda x: x["name"])

        ctx = {
            "isa_name": isa_reg.name,
            "xlen": isa_reg.xlen,
            "registers": isa_reg.registers,
            "instr_infos": instr_infos,
            "machine": isa_reg.machine,
        }

        asm_path = out_path / f"{isa_reg.name}_asm.py"
        asm_path.write_text(env.get_template("asm/asm_assembler.py.j2").render(**ctx))
        asm_path.chmod(asm_path.stat().st_mode | 0o111)

        (out_path / "linker.ld").write_text(
            env.get_template("asm/asm_linker_ld.j2").render(**ctx)
        )

    logger.info(f"Generated assembler in {output_dir}")
