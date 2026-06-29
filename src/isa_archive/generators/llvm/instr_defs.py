"""Build the per-instruction definition records (operands, fixed fields, DAG
pattern, roles) the LLVM TableGen/C++ templates consume."""
import logging
import re
from typing import Optional

from ...compiler.behavior import BehaviorIR
from ...compiler.backends import LLVMDagBackend
from ...compiler.backends.llvm_dag import DagPattern
from ...compiler.utils import (build_reg_maps, compute_fixed_fields, build_regfile_shapes,
                               build_regfile_attrs)
from ...models.enums import FieldRole
from .encoding import _get_schema_combined_imm

logger = logging.getLogger("isa_archive.generators")


def _build_instr_defs(isa_reg, ISA_upper: str,
                      skip_regfiles: Optional[set] = None) -> list:
    """Return a list of (INSTR_NAME, instr_info_dict) for use in TableGen templates.

    Instructions whose schema references a register file in ``skip_regfiles``
    (files that don't form an LLVM register class) are omitted with a warning.
    """
    skip_regfiles = skip_regfiles or set()
    reg_class_map = {r.name: r.name.upper() for r in isa_reg.registers}
    reg_class_info = {
        r.name: {"class": r.name.upper(), "is_float": r.is_float}
        for r in isa_reg.registers
    }
    # Width/float of each register file, keyed by file name - used to populate
    # the language-agnostic structured operand metadata below (`out_ops`/`in_*`),
    # which downstream generators (e.g. the C/Rust intrinsics) consume.
    reg_meta = {r.name: {"width": r.width, "is_float": r.is_float}
                for r in isa_reg.registers}

    instr_defs = []
    for instr_name, instr in isa_reg.instructions.items():
        schema = isa_reg.schemas.get(instr.spec.schema_name)
        if schema is None:
            logger.warning("%s: instruction '%s' references unknown schema '%s'; "
                           "omitted from the LLVM backend",
                           ISA_upper, instr_name, instr.spec.schema_name)
            continue

        used_files = {f.maps_to_state for f in schema.spec.fields if f.maps_to_state}
        skipped = sorted(used_files & skip_regfiles)
        if skipped:
            logger.warning("%s: instruction '%s' uses register file(s) %s, which "
                           "have no LLVM register class; omitted from the LLVM "
                           "backend", ISA_upper, instr_name, ", ".join(skipped))
            continue

        reg_map, var_widths = build_reg_maps(schema, isa_reg)

        # Immediate operand types from the schema, threaded into the DAG backend
        # so patterns carry the right TableGen operand type directly (no
        # post-hoc string patching). Split-immediate schemas reference the
        # combined operand as `$imm`.
        cimm = _get_schema_combined_imm(schema)
        imm_types = {
            field.name: f"{'s' if field.is_signed else 'u'}imm{field.width}"
            for field in schema.spec.fields
            if field.role == FieldRole.IMMEDIATE
        }

        try:
            ir = BehaviorIR(
                instr.spec.behavior,
                register_map=reg_map,
                var_widths=var_widths,
                operands=isa_reg.operands,
                csrs={},
                regfile_shapes=build_regfile_shapes(isa_reg),
                regfile_attrs=build_regfile_attrs(isa_reg),
            )
            dag_backend = LLVMDagBackend(
                ir, xlen=isa_reg.xlen, reg_class_info=reg_class_info,
                imm_operand_types=imm_types,
                combined_imm_type=cimm["operand_name"] if cimm else None,
            )
            dag_result = dag_backend.translate()
        except Exception:
            ir = None
            dag_backend = None
            dag_result = DagPattern(category="custom", notes=["behavior parse error"])

        write_vars = ir.write_vars if ir else set()

        outs_parts: list[str] = []
        reg_ins_parts: list[str] = []  # input registers (build separately for ordering)
        imm_ins_parts: list[str] = []  # input immediates
        reg_asm_parts: list[str] = []
        imm_asm_parts: list[str] = []

        # Language-agnostic structured operands (name + width/signedness), in the
        # same out-then-reg-then-imm order as the assembly string. Downstream
        # generators map these to concrete C/Rust types.
        out_ops: list[dict] = []
        in_reg_ops: list[dict] = []
        in_imm_ops: list[dict] = []

        out_asm_parts: list[str] = []
        for field in schema.spec.fields:
            if field.is_fixed_value:
                continue
            if field.role == FieldRole.REGISTER:
                reg_class = reg_class_map.get(field.type or "", "GPR")
                td_operand = f"{reg_class}:${field.name}"
                meta = reg_meta.get(field.type or "", {"width": isa_reg.xlen, "is_float": False})
                op_entry = {"name": field.name, "width": meta["width"],
                            "is_float": meta["is_float"]}
                if field.name in write_vars:
                    outs_parts.append(td_operand)
                    out_asm_parts.append(f"${field.name}")
                    out_ops.append(op_entry)
                else:
                    reg_ins_parts.append(td_operand)
                    reg_asm_parts.append(f"${field.name}")
                    in_reg_ops.append(op_entry)
            elif field.role == FieldRole.IMMEDIATE:
                if cimm:
                    # Schema uses split immediates → a single combined operand (added once)
                    if not imm_ins_parts:
                        imm_ins_parts.append(f"{cimm['operand_name']}:$imm")
                        imm_asm_parts.append("$imm")
                        in_imm_ops.append({"name": "imm", "width": cimm["width"],
                                           "signed": True})
                else:
                    sign = "s" if field.is_signed else "u"
                    td_operand = f"{sign}imm{field.width}:${field.name}"
                    imm_ins_parts.append(td_operand)
                    imm_asm_parts.append(f"${field.name}")
                    in_imm_ops.append({"name": field.name, "width": field.width,
                                       "signed": field.is_signed})

        # Registers first, then immediates (matches LLVM convention and Pat<> result order).
        # The destination register(s) lead the assembly string: "add $rd, $rs1, $rs2".
        ins_parts = reg_ins_parts + imm_ins_parts
        asm_parts = out_asm_parts + reg_asm_parts + imm_asm_parts

        # Fixed fields (opcode / constant / reserved) as binary literals for TableGen.
        fixed_fields: dict[str, str] = {
            field.name: f"0b{val:0{field.width}b}"
            for field, val in compute_fixed_fields(instr, schema, isa_reg)
        }

        branch_cond = None
        if dag_result.category == "branch" and dag_backend is not None:
            branch_cond = dag_backend.get_branch_condition()

        # Patterns like (brind …) and (ret) don't produce values, so they can't
        # appear alongside output operands in a TableGen pattern. Clear the pattern
        # for jump_ind/jump_abs instructions that write a link register, letting
        # ISelLowering handle them via custom code.
        dag_pattern = dag_result.dag

        if dag_result.category in ("jump_ind", "jump_abs") and outs_parts:
            dag_pattern = None

        dag_category = dag_result.category
        dag_op = dag_result.op
        dag_load_width = dag_result.load_width
        dag_notes = list(dag_result.notes)

        # A TableGen pattern must cover every non-fixed operand; an instruction
        # with extra operands the behavior never mentions (predicate/hint fields)
        # would otherwise be a hard TableGen build error. Demote to custom - the
        # category must drop too, or role inference would bind this instruction
        # to a role (spill, sp-adjust, …) whose expected operand shape it lacks.
        if dag_pattern:
            instr_ops = set(re.findall(r"\$(\w+)", " ".join(outs_parts + ins_parts)))
            pattern_ops = set(re.findall(r"\$(\w+)", dag_pattern))
            uncovered = sorted(instr_ops - pattern_ops)
            if uncovered:
                logger.warning(
                    "%s: instruction '%s' has operand(s) %s not covered by its "
                    "inferred pattern; using custom lowering",
                    ISA_upper, instr_name, ", ".join(f"${u}" for u in uncovered),
                )
                dag_pattern = None
                dag_category = "custom"
                dag_op = None
                dag_load_width = None
                dag_notes.append(
                    "operands not covered by inferred pattern: "
                    + ", ".join(f"${u}" for u in uncovered)
                )

        # The generated brcond Pat<> assumes the canonical compare-branch shape
        # (two register sources + one target immediate, no outputs).
        if branch_cond and not (
            len(reg_ins_parts) == 2 and len(imm_ins_parts) == 1 and not outs_parts
        ):
            logger.warning(
                "%s: branch '%s' does not have the 2-register + 1-immediate "
                "shape; no brcond pattern generated",
                ISA_upper, instr_name,
            )
            branch_cond = None

        schema_roles = list(schema.spec.compiler.roles) if schema.spec.compiler else []
        instr_roles = list(instr.spec.compiler.roles) if instr.spec.compiler else []

        # Dedicated call/return instruction flags. Deliberately NOT derived from
        # control.call/control.ret roles: a role says the opcode can *serve* as
        # the call/return (the Pseudo defs carry the MI flags); these flags mark
        # instructions that are *always* a call/return, which only a dedicated
        # CALL/RET mnemonic conveys.
        lname = instr_name.lower()
        is_call = lname in ("call", "bl") or bool(re.search(r"(^|[._-])call", lname))
        is_return = lname in ("ret", "return") or bool(re.search(r"(^|[._-])ret(urn)?$", lname))

        instr_info = {
            "schema": instr.spec.schema_name,
            "dag_pattern": dag_pattern,
            "dag_category": dag_category,
            "dag_load_width": dag_load_width,
            "dag_load_signed": dag_result.load_signed,
            "dag_op": dag_op,
            "dag_is_float": dag_result.is_float,
            "dag_addr_indexed": dag_result.addr_indexed,
            "dag_notes": dag_notes,
            "schema_roles": schema_roles,
            "instr_roles": instr_roles,
            "outs": ", ".join(outs_parts),
            "ins": ", ".join(ins_parts),
            "asm_str": ", ".join(asm_parts),
            "out_ops": out_ops,
            "in_reg_ops": in_reg_ops,
            "in_imm_ops": in_imm_ops,
            "is_terminator": dag_category in ("branch", "jump_ind", "jump_abs"),
            "is_branch": dag_category == "branch",
            "is_call": is_call,
            "is_return": is_return,
            "fixed_fields": fixed_fields,
            "branch_cond": branch_cond,
            "description": instr.metadata.description or "",
            # Provenance for generated comments: the source behavior and the
            # roles (schema-level + per-instruction) that shaped this def.
            "behavior": " ".join(instr.spec.behavior.split()),
            "roles": schema_roles + instr_roles,
        }
        instr_defs.append((instr_name.upper(), instr_info))

    return instr_defs
