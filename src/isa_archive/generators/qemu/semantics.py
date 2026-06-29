"""Per-instruction QEMU semantics: behavior → C/TCG, plus the Jinja env and the
pre-flight validation that runs every instruction through the translators."""
from ...compiler.behavior import BehaviorIR
from ...compiler.backends import QemuCBackend, QemuTCGBackend
from ...compiler.utils import (build_reg_maps, instruction_pattern, constraint_to_c,
                               csr_map, build_csr_info, build_trap_info, build_regfile_shapes,
                               build_regfile_attrs, compute_decode_fields)
from ...models.enums import FieldRole
from ...models.scalar_types import of_register
from ..base import make_jinja_env
from .word import _guest_word, _regfile_storage


def _instr_qemu_info(instr, isa_reg, storage: dict[str, dict]) -> dict:
    """Compute everything the QEMU templates need for one instruction.

    Raises ValueError (prefixed with the instruction name by the caller) for
    anything that cannot be translated, so generation fails loudly instead of
    emitting invalid C.
    """
    xlen = isa_reg.xlen
    word = _guest_word(isa_reg)
    c_int_type = word["c_int_type"]
    tcg_suffix = word["tcg_type"]

    schema = isa_reg.schemas.get(instr.spec.schema_name)
    if schema is None:
        raise ValueError(f"references unknown schema '{instr.spec.schema_name}'")
    reg_map, var_widths = build_reg_maps(schema, isa_reg)

    ir = BehaviorIR(instr.spec.behavior, register_map=reg_map, var_widths=var_widths,
                    operands=isa_reg.operands, csrs=csr_map(isa_reg),
                    regfile_shapes=build_regfile_shapes(isa_reg),
                    regfile_attrs=build_regfile_attrs(isa_reg))

    wide_files = sorted({reg_map[v] for v in ir.used_vars
                         if v in reg_map and storage[reg_map[v]]["storage_bits"] is None})
    if wide_files:
        raise ValueError(
            f"uses register file(s) {', '.join(wide_files)} stored as byte "
            f"arrays; the QEMU backend supports direct register arithmetic up "
            f"to 64 bits, plus native 128-bit (other widths >64 need per-lane "
            f"behaviors or the upcoming vector-type support)"
        )

    is_conditional_branch = ir.modifies_pc and not ir.is_unconditional_jump
    reads_pc = "pc" in ir.read_vars

    reg_objs_early = {r.name: r for r in isa_reg.registers}
    zero_reg_map = {}
    for field in schema.spec.fields:
        if field.role == FieldRole.REGISTER and field.name in ir.write_vars:
            reg_obj = reg_objs_early.get(field.maps_to_state)
            if reg_obj and reg_obj.zero_register is not None:
                zero_reg_map[field.name] = reg_obj.zero_register

    float_regs = {r.name: of_register(r) for r in isa_reg.registers if r.is_float}
    helper_only = {name for name, st in storage.items() if st["tcg"] is None}
    write_masks = {name: st["mask"] for name, st in storage.items() if st["mask"]}
    tcg_files = {name for name, st in storage.items() if st["tcg"]}

    code = QemuCBackend(ir).translate(
        pc_write_tracking=is_conditional_branch,
        zero_register_map=zero_reg_map if ir.modifies_pc else None,
        float_regs=float_regs,
        helper_only_regfiles=helper_only,
        regfile_write_masks=write_masks,
        pc_mask=word["xlen_mask"],
        addr_mask=word["xlen_mask"],
        csr_info=build_csr_info(isa_reg),
        trap_info=build_trap_info(isa_reg),
        regfile_shapes=build_regfile_shapes(isa_reg),
        regfile_attrs=build_regfile_attrs(isa_reg),
    )
    tcg_code = QemuTCGBackend(ir).translate(xlen=xlen, float_regs=float_regs,
                                            tcg_regfiles=tcg_files)

    pattern = instruction_pattern(instr, schema)
    operand_fields = [f.name for f in schema.spec.fields if not f.is_fixed_value]
    fields_str = " ".join([f"{name}=%{schema.metadata.name}_{name}" for name in operand_fields])

    sorted_vars = sorted([v for v in ir.used_vars if v not in ir.temporaries and v != "pc"])
    helper_args = []
    tcg_args = []
    for v in sorted_vars:
        if (v in ir.write_vars or v in ir.attr_regs
                or (v in reg_map and reg_map[v] in helper_only)):
            # destination register index, a helper-only file's source index, or a
            # register whose attribute is accessed: the helper gets the index and
            # reaches env-> state itself.
            helper_args.append({"name": v, "type": c_int_type})
            tcg_args.append({"tcg": f"tcg_constant_{tcg_suffix}(a->{v})", "name": v})
        elif v in reg_map:
            # register value from a TCG global (width == xlen by construction)
            helper_args.append({"name": f"{v}_val", "type": c_int_type})
            tcg_args.append({"tcg": f"arch_{reg_map[v]}[a->{v}]", "name": f"{v}_val"})
        else:
            helper_args.append({"name": v, "type": c_int_type})
            tcg_args.append({"tcg": f"tcg_constant_{tcg_suffix}(a->{v})", "name": v})

    zero_guards = [{"field": k, "index": v} for k, v in zero_reg_map.items()]
    is_unconditional_jump = ir.is_unconditional_jump if ir.modifies_pc else False

    all_constraints = list(schema.spec.constraints) + list(instr.spec.constraints)
    constraints = [
        {"c_expr": constraint_to_c(c.expr, field_prefix="a->"), "message": c.message or c.expr}
        for c in all_constraints
    ]

    return {
        "code": code,
        "tcg_code": tcg_code,
        "pattern": pattern,
        "fields_str": fields_str,
        "helper_args": helper_args,
        "tcg_args": tcg_args,
        "modifies_pc": ir.modifies_pc,
        "reads_pc": reads_pc,
        "zero_guards": zero_guards,
        "bytes_per_instr": schema.spec.length // 8,
        "is_unconditional_jump": is_unconditional_jump,
        "is_conditional_branch": is_conditional_branch,
        "constraints": constraints,
    }


def _build_wide_decode_meta(isa_reg) -> list:
    """Per-instruction metadata for the hand-written >64-bit decoder (the path that
    replaces decodetree, which caps at 64 bits).

    For each instruction: ``fixed`` (the opcode/constant/reserved bits to match) and
    ``args`` (every non-fixed field to extract into ``arg_<fn>``). The field set and
    the sign-extension flag exactly mirror what decodetree produces for <=64-bit
    ISAs - raw per-field extraction, split immediates left for the helper to
    reassemble - so the unchanged ``trans_*`` functions (which read ``a-><field>``)
    work identically. Sorted most-specific-first (most fixed bits) so a more general
    encoding can't shadow a more specific one.
    """
    out = []
    for instr in isa_reg.instructions.values():
        schema = isa_reg.schemas[instr.spec.schema_name]
        fixed = compute_decode_fields(instr, schema, isa_reg)["fixed"]
        args = [{"name": f.name, "start": f.start, "width": f.width,
                 "signed": bool(getattr(f, "is_signed", False))}
                for f in schema.spec.fields if not f.is_fixed_value]
        out.append({
            "fn": instr.metadata.name.lower().replace("-", "_"),
            "fixed": fixed,
            "args": args,
            "fixed_bits": sum(x["width"] for x in fixed),
        })
    out.sort(key=lambda it: it["fixed_bits"], reverse=True)
    return out


def _make_qemu_env():
    env = make_jinja_env()

    def get_qemu_info(instr, isa_reg):
        try:
            return _instr_qemu_info(instr, isa_reg, _regfile_storage(isa_reg))
        except ValueError as e:
            raise ValueError(f"instruction '{instr.metadata.name}': {e}") from e

    env.filters["qemu_info"] = get_qemu_info
    return env


def _validate_for_qemu(isa_reg) -> None:
    """Pre-flight checks: fail with every problem listed before writing any file."""
    if isa_reg.xlen not in (8, 16, 32, 64, 128):
        raise ValueError(
            f"{isa_reg.name}: QEMU generation requires a power-of-two xlen of "
            f"8, 16, 32, 64, or 128; got xlen={isa_reg.xlen}. (8/16 are "
            f"emulated over a 32-bit guest word with masked PC/addresses; "
            f"xlen=128 has native 128-bit registers/arithmetic but a 64-bit "
            f"PC/address space - TCG has no 128-bit guest addresses.)"
        )
    storage = _regfile_storage(isa_reg)
    errors = []
    for instr in isa_reg.instructions.values():
        try:
            _instr_qemu_info(instr, isa_reg, storage)
        except ValueError as e:
            errors.append(f"instruction '{instr.metadata.name}': {e}")
    if errors:
        raise ValueError(
            f"{isa_reg.name}: QEMU generation failed for {len(errors)} "
            f"instruction(s):\n  - " + "\n  - ".join(errors)
        )
