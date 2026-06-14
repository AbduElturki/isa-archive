import logging
import pathlib
import re
from typing import Optional

from ..compiler.loader import Registry
from ..compiler.behavior import BehaviorIR
from ..compiler.backends import LLVMDagBackend
from ..compiler.backends.llvm_dag import DagPattern
from ..compiler.utils import build_reg_maps, compute_insn_width
from ..models.abi import ABI
from ..models.compiler import CompilerProfile
from ..models.enums import FieldRole
from ..models.scalar_types import of_register, resolve as resolve_scalar_type
from .base import make_jinja_env, prepare_output_dir

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

        out_asm_parts: list[str] = []
        for field in schema.spec.fields:
            if field.is_fixed_value:
                continue
            if field.role == FieldRole.REGISTER:
                reg_class = reg_class_map.get(field.type or "", "GPR")
                td_operand = f"{reg_class}:${field.name}"
                if field.name in write_vars:
                    outs_parts.append(td_operand)
                    out_asm_parts.append(f"${field.name}")
                else:
                    reg_ins_parts.append(td_operand)
                    reg_asm_parts.append(f"${field.name}")
            elif field.role == FieldRole.IMMEDIATE:
                if cimm:
                    # Schema uses split immediates → a single combined operand (added once)
                    if not imm_ins_parts:
                        imm_ins_parts.append(f"{cimm['operand_name']}:$imm")
                        imm_asm_parts.append("$imm")
                else:
                    sign = "s" if field.is_signed else "u"
                    td_operand = f"{sign}imm{field.width}:${field.name}"
                    imm_ins_parts.append(td_operand)
                    imm_asm_parts.append(f"${field.name}")

        # Registers first, then immediates (matches LLVM convention and Pat<> result order).
        # The destination register(s) lead the assembly string: "add $rd, $rs1, $rs2".
        ins_parts = reg_ins_parts + imm_ins_parts
        asm_parts = out_asm_parts + reg_asm_parts + imm_asm_parts

        # Populate fixed_fields from opcode, constant, and reserved fields
        fixed_fields: dict[str, str] = {}
        for field in schema.spec.fields:
            if field.role == FieldRole.RESERVED:
                fixed_fields[field.name] = f"0b{'0' * field.width}"
            elif field.role == FieldRole.OPCODE:
                try:
                    val = isa_reg._resolve_value(instr.spec.opcode)
                    fixed_fields[field.name] = f"0b{val:0{field.width}b}"
                except Exception:
                    pass
            elif field.role == FieldRole.CONSTANT:
                const_val = instr.spec.constants.get(field.name)
                if const_val is not None:
                    try:
                        val = isa_reg._resolve_value(const_val)
                        fixed_fields[field.name] = f"0b{val:0{field.width}b}"
                    except Exception:
                        pass

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
        # would otherwise be a hard TableGen build error. Demote to custom — the
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
            "is_terminator": dag_category in ("branch", "jump_ind", "jump_abs"),
            "is_branch": dag_category == "branch",
            "is_call": is_call,
            "is_return": is_return,
            "fixed_fields": fixed_fields,
            "branch_cond": branch_cond,
            "description": instr.metadata.description or "",
        }
        instr_defs.append((instr_name.upper(), instr_info))

    return instr_defs


def _find_sp_adjust_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic add-immediate opcode for SP adjustment (prologue/epilogue).

    Used only as a fallback when no instruction declares the `frame.sp_adjust` /
    `alu_ri.add` compiler role. Returns None if nothing plausible is found —
    callers and templates guard on None.
    """
    for name, info in instr_defs:
        if name == "ADDI":
            return name
    for name, info in instr_defs:
        dag = info.get("dag_pattern") or ""
        if info["dag_category"] == "alu_ri" and "add" in dag.lower():
            return name
    for name, info in instr_defs:
        if info["dag_category"] == "alu_ri":
            return name
    return None


def _find_lui_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic load-upper-immediate opcode (for constant/GlobalAddress lowering).

    Fallback only; returns None if no plausible candidate exists.
    """
    for name, info in instr_defs:
        if name == "LUI":
            return name
    for name, info in instr_defs:
        if info["dag_category"] in ("custom", "alu_ri") and info["outs"] and not info["ins"]:
            return name
    return None


def _compute_single_field_fixup(opcode_name: str, instr_defs: list, schemas: dict,
                                 insn_bits: int = 32) -> Optional[dict]:
    """Compute fixup encoding info for an instruction with one contiguous immediate field.

    Returns dict with bit_low, bit_high, width, keep_mask_hex, val_mask_hex, or None if not found.
    """
    for name, info in instr_defs:
        if name == opcode_name:
            schema = schemas.get(info.get("schema", ""))
            if schema is None:
                return None
            imm_fields = [f for f in schema.spec.fields if f.role == FieldRole.IMMEDIATE]
            if len(imm_fields) != 1:
                return None
            field = imm_fields[0]
            hw_low = field.start
            hw_high = field.start + field.width - 1
            width = field.width
            field_mask = ((1 << width) - 1) << hw_low
            keep_mask = ((1 << insn_bits) - 1) & ~field_mask
            return {
                "bit_low": hw_low,
                "bit_high": hw_high,
                "width": width,
                "keep_mask_hex": _mask_hex(keep_mask, insn_bits),
                "val_mask_hex": f"0x{(1 << width) - 1:X}u",
            }
    return None


def _encode_instr_as_nop(opcode_name: str, instr_defs: list, schemas: dict,
                          isa_reg, schema_len: int,
                          byte_order: str = "little") -> Optional[str]:
    """Encode a named instruction with all operands zeroed (rd=0, rs1=0, imm=0).

    Walks the schema fields, setting bits for OPCODE and CONSTANT roles from the
    instruction spec. REGISTER and IMMEDIATE fields default to 0 (x0 / imm=0).
    Returns a C string literal like '"\\x13\\x00\\x00\\x00"', or None on failure.
    """
    instr = isa_reg.instructions.get(opcode_name.lower())
    if instr is None:
        return None
    schema = schemas.get(instr.spec.schema_name)
    if schema is None:
        return None

    word = 0
    for field in schema.spec.fields:
        if field.role == FieldRole.OPCODE:
            try:
                val = int(isa_reg._resolve_value(instr.spec.opcode))
                word |= (val & ((1 << field.width) - 1)) << field.start
            except Exception:
                pass
        elif field.role == FieldRole.CONSTANT:
            const_val = instr.spec.constants.get(field.name)
            if const_val is not None:
                try:
                    val = int(isa_reg._resolve_value(const_val))
                    word |= (val & ((1 << field.width) - 1)) << field.start
                except Exception:
                    pass
        # REGISTER, IMMEDIATE, RESERVED → 0

    nbytes = schema_len // 8
    try:
        raw = word.to_bytes(nbytes, 'big' if byte_order == 'big' else 'little')
    except OverflowError:
        return None
    return '"' + ''.join(f'\\x{b:02x}' for b in raw) + '"'


def _find_jal_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic direct-jump (JAL) opcode. Fallback only; None if not found."""
    for name, info in instr_defs:
        if name == "JAL":
            return name
    for name, info in instr_defs:
        if info["dag_category"] == "jump_abs":
            return name
    return None


def _find_jalr_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic register-indirect-jump (JALR) opcode. Fallback only; None if not found."""
    for name, info in instr_defs:
        if name == "JALR":
            return name
    for name, info in instr_defs:
        if info["dag_category"] == "jump_ind":
            return name
    return None


def _parse_imm_field_range(field_name: str) -> Optional[tuple[int, int]]:
    """Parse imm_N or imm_N_M field names → (high_bit, low_bit) in the logical immediate.

    Returns None if the name doesn't follow the convention.
    """
    m = re.match(r'(?:imm|off(?:set)?)_(\d+)(?:_(\d+))?$', field_name)
    if not m:
        return None
    high = int(m.group(1))
    low = int(m.group(2)) if m.group(2) is not None else high
    return (high, low)


def _get_schema_combined_imm(schema) -> Optional[dict]:
    """For schemas with split immediate fields (e.g. RISC-V B/S/J-type), return info
    for generating a single combined immediate variable in the TableGen format class.

    Returns None for schemas with ≤1 immediate field or non-parseable names.

    The returned dict contains:
      - width: int              — total bits in the combined immediate (max_bit + 1)
      - includes_lsb: bool      — True if any field covers bit 0 (→ data offset like S-type)
                                  False → branch/jump target (bit 0 always 0, like B/J-type)
      - operand_name: str       — "simm{N}" for data offsets, "brtarget"/"jaltarget" for targets
      - hw_assignments: list    — [(hw_high, hw_low, imm_high, imm_low), …] for bit routing
    """
    imm_fields = [f for f in schema.spec.fields
                  if f.role == FieldRole.IMMEDIATE and not getattr(f, 'is_fixed_value', False)]
    if len(imm_fields) <= 1:
        return None

    hw_assignments: list[tuple[int, int, int, int]] = []
    max_bit = 0
    has_lsb = False

    for f in imm_fields:
        parsed = _parse_imm_field_range(f.name)
        if parsed is None:
            return None  # Naming convention not followed — fall back to split fields
        imm_high, imm_low = parsed
        expected_width = imm_high - imm_low + 1
        if expected_width != f.width:
            return None  # Width mismatch
        hw_high = f.start + f.width - 1
        hw_low = f.start
        hw_assignments.append((hw_high, hw_low, imm_high, imm_low))
        max_bit = max(max_bit, imm_high)
        if imm_low == 0:
            has_lsb = True

    combined_width = max_bit + 1

    if has_lsb:
        # Data-offset type (e.g. S-type): bit 0 is present, use simm{width}
        operand_name = f"simm{combined_width}"
    else:
        # Branch/jump target: bit 0 is always 0 and not stored
        operand_name = "jaltarget" if combined_width > 13 else "brtarget"

    return {
        "width": combined_width,
        "includes_lsb": has_lsb,
        "operand_name": operand_name,
        "hw_assignments": hw_assignments,
    }


def _collect_imm_operands(isa_reg) -> list[dict]:
    """Return sorted list of unique immediate operand type dicts for the template.

    Each dict: {"name": "simm12", "sign": "s", "width": 12}
    Includes both individual field types AND combined logical types for split-immediate schemas.
    """
    seen: set[str] = set()
    result: list[dict] = []

    def _add(name: str, sign: str, width: int) -> None:
        if name not in seen:
            seen.add(name)
            result.append({"name": name, "sign": sign, "width": width})

    for schema in isa_reg.schemas.values():
        cimm = _get_schema_combined_imm(schema)
        if cimm and cimm["includes_lsb"]:
            # data-offset combined type (e.g. simm12 for S-type)
            _add(cimm["operand_name"], "s", cimm["width"])
        for field in schema.spec.fields:
            if field.role != FieldRole.IMMEDIATE:
                continue
            sign = "s" if field.is_signed else "u"
            _add(f"{sign}imm{field.width}", sign, field.width)

    result.sort(key=lambda d: (d["sign"], d["width"]))
    return result


def _mask_hex(value: int, insn_bits: int) -> str:
    """Format a mask literal sized for the instruction word (u for ≤32-bit, ull above)."""
    if insn_bits <= 32:
        return f"0x{value & 0xFFFFFFFF:08X}u"
    return f"0x{value & ((1 << 64) - 1):016X}ull"


def _compute_fixup_info(hw_assignments: list, insn_bits: int = 32) -> dict:
    """Compute keep_mask and encoding pieces from hw_assignments for applyFixup.

    hw_assignments: list of (hw_high, hw_low, imm_high, imm_low) from _get_schema_combined_imm.
    Returns dict with keep_mask (hex) and pieces list for the Jinja2 template.
    """
    full = (1 << insn_bits) - 1
    keep_mask = full
    pieces = []
    for hw_high, hw_low, imm_high, imm_low in hw_assignments:
        width = hw_high - hw_low + 1
        field_mask = ((1 << width) - 1) << hw_low
        keep_mask = (keep_mask & ~field_mask) & full
        pieces.append({
            "imm_low": imm_low,
            "hw_low": hw_low,
            "mask_hex": f"0x{(1 << width) - 1:X}u",
        })
    return {
        "keep_mask_hex": _mask_hex(keep_mask, insn_bits),
        "pieces": pieces,
    }


def _class_value_types(reg) -> list[str]:
    """LLVM value types a register file holds.

    The scalar element type (modern ``type:``, the legacy ``float`` flag, or a
    plain integer default) resolves through the single scalar-type source of truth
    (``scalar_types.of_register``). The legacy ``value_types`` field remains an
    explicit verbatim override of the LLVM value-type list.
    """
    if not getattr(reg, "type", None) and reg.value_types:
        return list(reg.value_types)        # legacy explicit override
    return [of_register(reg).llvm_mvt]


def _resolve_reg_name(registers, alias: Optional[str]) -> Optional[str]:
    """Return the canonical register name (prefix+index) for the given alias.

    Resolution is by declared alias only. There is deliberately NO positional
    fallback: silently designating e.g. register #2 as the stack pointer on an
    alias-less ISA produced wrong backends for accelerator-style targets. An ISA
    that wants the CPU conventions declares the aliases (or an explicit ABI);
    the c-baremetal profile reports unresolved sp/ra/zero as missing.
    """
    if not registers or not alias:
        return None
    first = registers[0]
    if alias in first.aliases:
        return f"{first.prefix}{first.aliases[alias]}"
    return None


# ── Compiler-role contract (Part A/C) ───────────────────────────────────────
def _mem_role_names(xlen: int) -> list[str]:
    """Memory roles for an ISA of the given data width: sign/zero-extending pairs
    for every sub-word width, plus the full-word plain load/store."""
    roles: list[str] = []
    sub_widths = [w for w in (8, 16, 32, 64) if w < xlen]
    for w in sub_widths:
        roles += [f"mem.load{w}s", f"mem.load{w}u"]
    roles.append(f"mem.load{xlen}")
    roles += [f"mem.store{w}" for w in sub_widths] + [f"mem.store{xlen}"]
    return roles


def _role_groups(xlen: int) -> dict[str, list[str]]:
    """The role slots a backend can fill to lower C, grouped for the report."""
    return {
        "ALU rr":  [f"alu_rr.{op}" for op in ("add", "sub", "and", "or", "xor", "shl", "srl", "sra")],
        "ALU ri":  [f"alu_ri.{op}" for op in ("add", "and", "or", "xor", "shl", "srl", "sra")],
        "Const":   ["const.hi", "const.lo", "const.load"],
        "Memory":  _mem_role_names(xlen),
        "Branch":  [f"branch.{c}" for c in ("eq", "ne", "lt", "ge", "ltu", "geu")],
        "Compare": ["cmp.lt", "cmp.ltu", "cmp.lti", "cmp.ltui"],
        "Control": ["control.jump", "control.call", "control.call_indirect", "control.ret"],
        "Frame":   ["frame.sp_adjust"],
        "Global":  ["global.hi", "global.lo"],
    }


def _required_roles(xlen: int) -> set[str]:
    """Roles whose absence makes a working C backend impossible (drives --strict).
    `const` is checked separately (strategy-dependent: single_imm needs no hi/lo).
    Spill/reload uses the full-word load/store, so the requirement follows xlen."""
    return {
        "alu_rr.add", "alu_rr.sub", "alu_ri.add",
        f"mem.load{xlen}", f"mem.store{xlen}",
        "branch.eq", "branch.ne",
        "control.jump", "control.ret", "frame.sp_adjust",
    }

_BRANCH_COND_TO_ROLE: dict[str, str] = {
    "seteq": "eq", "setne": "ne", "setlt": "lt", "setge": "ge",
    "setult": "ltu", "setuge": "geu", "setle": "le", "setgt": "gt",
    "setugt": "ugt", "setule": "ule",
}


_SETCC_TO_CMP: dict[str, str] = {
    "setlt": "lt", "setult": "ltu", "seteq": "eq", "setne": "ne",
    "setle": "le", "setge": "ge", "setgt": "gt",
    "setugt": "ugt", "setule": "ule", "setuge": "uge",
}


def _infer_roles(info: dict, xlen: int = 32) -> set[str]:
    """Specific compiler roles inferred from an instruction's behavior (layer 1)."""
    roles: set[str] = set()
    cat = info["dag_category"]
    op = info.get("dag_op")
    if op in _SETCC_TO_CMP:
        # set-less-than family: SLT/SLTU → cmp.lt/ltu, SLTI/SLTIU → cmp.lti/ltui
        suffix = _SETCC_TO_CMP[op]
        roles.add(f"cmp.{suffix}i" if cat == "alu_ri" else f"cmp.{suffix}")
    elif cat == "alu_rr" and op and op != "copy":
        roles.add(f"alu_rr.{op}")
    elif cat == "alu_ri" and op and op != "copy":
        roles.add(f"alu_ri.{op}")
    elif cat == "load":
        w = info.get("dag_load_width")
        if w:
            # Sub-word loads carry a sign/zero-extension suffix; the full-word
            # load doesn't extend (suffix rule keyed on xlen, not a literal 32).
            roles.add(f"mem.load{w}" if w >= xlen
                      else f"mem.load{w}{'s' if info.get('dag_load_signed') else 'u'}")
    elif cat == "store":
        w = info.get("dag_load_width")
        if w:
            roles.add(f"mem.store{w}")
    elif cat == "branch":
        cond = _BRANCH_COND_TO_ROLE.get(info.get("branch_cond") or "")
        if cond:
            roles.add(f"branch.{cond}")
    elif cat == "jump_abs":
        roles.add("control.jump")
        if info.get("is_call"):
            roles.add("control.call")
    elif cat == "jump_ind":
        roles.add("control.ret" if info.get("is_return") else "control.call_indirect")
    return roles


def _expand_role(role: str, info: dict, xlen: int = 32) -> set[str]:
    """Expand a declared role tag. Specific roles (with a dot) pass through; a bare
    shape (`alu_rr`, `branch`) is expanded using the behavior-inferred op/condition."""
    if "." in role:
        return {role}
    if role in ("alu_rr", "alu_ri"):
        op = info.get("dag_op")
        return {f"{role}.{op}"} if op and op != "copy" else set()
    if role == "branch":
        cond = _BRANCH_COND_TO_ROLE.get(info.get("branch_cond") or "")
        return {f"branch.{cond}"} if cond else set()
    # load/store/control/const/frame/global bare shapes → fall back to inference
    return _infer_roles(info, xlen)


def _collect_compiler_roles(instr_defs: list, xlen: int = 32) -> tuple[dict[str, str], list[tuple]]:
    """Merge the three role layers (infer → schema → instruction) into a role→opcode
    map. Returns (role_to_opcode, conflicts) where conflicts are (role, first, second)."""
    role_to_opcode: dict[str, str] = {}
    conflicts: list[tuple] = []
    for name, info in instr_defs:
        roles = set(_infer_roles(info, xlen))
        for r in info.get("schema_roles", []):
            roles |= _expand_role(r, info, xlen)
        for r in info.get("instr_roles", []):
            roles |= _expand_role(r, info, xlen)
        for r in roles:
            if r in role_to_opcode and role_to_opcode[r] != name:
                conflicts.append((r, role_to_opcode[r], name))
            else:
                role_to_opcode.setdefault(r, name)
    return role_to_opcode, conflicts


def _build_coverage_report(isa_name: str, roles: dict[str, str], conflicts: list[tuple],
                            const_strategy: str,
                            has_ordering_branches: bool = True,
                            xlen: int = 32,
                            profile: str = "c-baremetal",
                            required: Optional[set] = None,
                            missing_prereqs: Optional[list] = None,
                            custom_instrs: Optional[list] = None) -> tuple[str, list[str]]:
    """Render COMPILER_COVERAGE.md text and return (markdown, missing_required).

    ``required`` is the profile-resolved required-role set; ``missing_prereqs``
    are non-role prerequisites (sp/ra/zero aliases) the profile demands;
    ``custom_instrs`` is [(name, notes)] for custom-lowered instructions (G4).
    """
    required = _required_roles(xlen) if required is None else required
    lines = [f"# {isa_name} compiler coverage", "",
             f"Profile: `{profile}`", ""]
    missing_required: list[str] = list(missing_prereqs or [])
    for group, group_roles in _role_groups(xlen).items():
        cells = []
        for r in group_roles:
            present = r in roles
            short = r.split(".", 1)[1] if "." in r else r
            cells.append(f"{short} {'✓' if present else '✗'}")
            if r in required and not present:
                missing_required.append(r)
        lines.append(f"- **{group}**: " + "  ".join(cells))

    # Ordering comparisons must be available either as direct compare-branches
    # (branch.lt/ge/ltu/geu) or as set-less-than (cmp.lt/cmp.ltu) + branch-on-zero.
    # These supplements encode what lowering C needs, so only the c-baremetal
    # contract adds them; a custom profile requires exactly its `requires` list.
    if profile == "c-baremetal" and not has_ordering_branches:
        for r in ("cmp.lt", "cmp.ltu"):
            if r not in roles:
                missing_required.append(r)

    # Const: strategy-dependent requirement (again, a C-lowering need)
    lines.append(f"- **Const strategy**: `{const_strategy}`")
    if profile == "c-baremetal":
        if const_strategy in ("hi_lo_add", "hi_lo_or", "shift_build"):
            for r in ("const.hi", "const.lo"):
                if r not in roles:
                    missing_required.append(r)
        elif const_strategy == "single_imm":
            if "const.load" not in roles:
                missing_required.append("const.load")

    if conflicts:
        lines.append("")
        lines.append("## Conflicts (multiple instructions claim one role)")
        for role, first, second in conflicts:
            lines.append(f"- `{role}`: {first} vs {second} (using {first})")

    if custom_instrs:
        lines.append("")
        lines.append("## Custom-lowered instructions (no selectable pattern)")
        for name, notes in custom_instrs:
            why = "; ".join(notes) if notes else "behavior not expressible as a single DAG pattern"
            lines.append(f"- `{name}`: {why}")

    status = "COMPILER-COMPLETE ✓" if not missing_required else \
             "INCOMPLETE ✗ — missing: " + ", ".join(sorted(set(missing_required)))
    lines += ["", f"**STATUS: {status}** (profile `{profile}`)", ""]
    return "\n".join(lines), sorted(set(missing_required))


def _setcc_branch_entries(roles: dict[str, str]) -> list[dict]:
    """How to materialize each integer comparison into a 0/1 register on an ISA
    that has conditional branches but NO set-less-than instruction.

    Each entry maps an ISD set-condition node (``seteq``, ``setlt``, …) to a
    branch opcode plus two flags: ``swap`` (compare the operands reversed) and
    ``taken_one`` (taking the branch yields 1, else 0). All ten conditions are
    synthesized from eq/ne/lt/ltu, preferring a direct ge/geu branch when the
    ISA provides one. The custom inserter turns these into a branch diamond.
    """
    b = {c: roles.get(f"branch.{c}") for c in ("eq", "ne", "lt", "ge", "ltu", "geu")}
    entries: list[dict] = []

    def add(node: str, opcode: Optional[str], swap: bool, taken_one: bool) -> None:
        if opcode:
            entries.append({"node": node, "opcode": opcode,
                            "swap": swap, "taken_one": taken_one})

    add("seteq", b["eq"], False, True)
    add("setne", b["ne"], False, True)
    if b["lt"]:
        add("setlt", b["lt"], False, True)
        add("setgt", b["lt"], True, True)                    # a>b ⟺ b<a
        # a>=b ⟺ !(a<b): branch a<b, taken→0; or a direct bge → taken→1
        add("setge", b["ge"] or b["lt"], False, bool(b["ge"]))
        add("setle", b["ge"] or b["lt"], True, bool(b["ge"]))  # a<=b ⟺ !(b<a) / b>=a
    elif b["ge"]:
        add("setge", b["ge"], False, True)
        add("setle", b["ge"], True, True)
    if b["ltu"]:
        add("setult", b["ltu"], False, True)
        add("setugt", b["ltu"], True, True)
        add("setuge", b["geu"] or b["ltu"], False, bool(b["geu"]))
        add("setule", b["geu"] or b["ltu"], True, bool(b["geu"]))
    elif b["geu"]:
        add("setuge", b["geu"], False, True)
        add("setule", b["geu"], True, True)

    for i, e in enumerate(entries):
        e["code"] = i
    return entries


def _infer_const_strategy(roles: dict[str, str], instr_defs: list) -> str:
    """Infer the constant-materialization strategy from declared roles + behavior.

    - single_imm  : a `const.load` instruction whose immediate spans the full word
    - hi_lo_or    : const.hi + const.lo, lo instruction zero-extends its immediate
    - hi_lo_add   : const.hi + const.lo, lo instruction sign-extends (RISC-V)
    - shift_build : no const.hi but shift + or available
    """
    info_by_name = {n: i for n, i in instr_defs}
    if "const.load" in roles:
        return "single_imm"
    if "const.hi" in roles and "const.lo" in roles:
        lo_info = info_by_name.get(roles["const.lo"], {})
        # zero-extended lo (e.g. MIPS ORI) → no sign-compensation needed
        return "hi_lo_or" if (lo_info.get("dag_op") in ("or", "xor")) else "hi_lo_add"
    if "const.hi" not in roles and ("alu_ri.shl" in roles and "alu_ri.or" in roles):
        return "shift_build"
    return "hi_lo_add"  # default; coverage report flags missing hi/lo


def generate_llvm(registry: Registry, output_dir: str, strict: bool = False):
    """Generate a complete LLVM backend for every ISA in the registry.

    Output mirrors the LLVM source tree layout for easy drop-in:
      llvm/lib/Target/{ISA}/   → $LLVM_SRC/llvm/lib/Target/{ISA}/
      patch_llvm.sh            → idempotent integration script
      INTEGRATE.md             → integration guide

    When ``strict`` is True, generation raises if an ISA is missing a required
    compiler role (see COMPILER_COVERAGE.md).
    """
    env = make_jinja_env()

    for isa_reg in registry.isas.values():
        xlen = isa_reg.xlen
        isa_name = isa_reg.name
        ISA = isa_name.upper().replace("-", "_").replace("/", "_")

        # The data width must be a legal LLVM scalar integer MVT; everything below
        # uses MVT::i{xlen} / i{xlen} value types.
        if xlen not in (8, 16, 32, 64, 128):
            raise ValueError(
                f"{ISA}: data width xlen={xlen} is not a legal scalar type "
                f"(must be one of 8, 16, 32, 64, 128)."
            )

        # Validate any unified `type:` references: a scalar (iN/fN) or a known Operand.
        for reg in isa_reg.registers:
            t = getattr(reg, "type", None)
            if t and resolve_scalar_type(t) is None and t not in isa_reg.operands:
                raise ValueError(
                    f"{ISA}: register file '{reg.name}' has type '{t}', which is "
                    f"neither a scalar (iN/fN) nor a defined Operand struct."
                )

        # Partition register files: only files whose element type can be a
        # first-class LLVM value type on this target become register classes —
        # integer files of the data width, float files, or files with an explicit
        # legacy `value_types` override. Everything else (1-bit predicates,
        # >xlen accumulators/vectors, …) stays architectural state: making e.g.
        # MVT::i1 or MVT::i128 a legal type via addRegisterClass breaks codegen
        # globally. Instructions touching excluded files are omitted (warned).
        def _is_codegen_class(reg) -> bool:
            if getattr(reg, "value_types", None) and not getattr(reg, "type", None):
                return True  # explicit legacy override — trust the author
            return reg.is_float or reg.width == xlen

        codegen_regs = [r for r in isa_reg.registers if _is_codegen_class(r)]
        skip_regfiles = {r.name for r in isa_reg.registers if not _is_codegen_class(r)}
        for name in sorted(skip_regfiles):
            reg = next(r for r in isa_reg.registers if r.name == name)
            logger.warning(
                "%s: register file '%s' (width %d) is not a legal LLVM register "
                "class on an xlen=%d target; kept as architectural state only",
                ISA, name, reg.width, xlen,
            )

        # Register bank properties: the primary integer file is the first
        # codegen-eligible non-float file (it provides the GPR class, the ABI
        # alias table, and sp/zero/ra resolution).
        first_reg = next((r for r in codegen_regs if not r.is_float), None)
        if first_reg is None and isa_reg.registers:
            raise ValueError(
                f"{ISA}: no register file is usable as an LLVM integer register "
                f"class (need an integer file of width xlen={xlen})."
            )
        first_reg_prefix = first_reg.prefix if first_reg else "r"
        first_reg_class = first_reg.name.upper() if first_reg else "GPR"

        # Resolve ABI
        abi_spec = isa_reg.manifest.spec.abi or ABI()
        aliases: dict = first_reg.aliases if first_reg else {}
        abi = abi_spec.resolve(aliases)

        reg_classes = [
            {
                "name": reg.name,
                "class_name": reg.name.upper(),
                "width": reg.width,
                "value_types": _class_value_types(reg),
                "is_float": reg.is_float,
                "zero_index": reg.zero_register,
            }
            for reg in codegen_regs
        ]
        # Per-file resolved value types, for the register-info template.
        reg_value_types = {rc["name"]: rc["value_types"] for rc in reg_classes}
        int_value_types = sorted({
            vt for rc in reg_classes if not rc["is_float"] for vt in rc["value_types"]
        })
        float_value_types = sorted({
            vt for rc in reg_classes if rc["is_float"] for vt in rc["value_types"]
        })

        int_codegen_regs = [r for r in codegen_regs if not r.is_float]
        sp_reg = _resolve_reg_name(int_codegen_regs, "sp")
        fp_reg = _resolve_reg_name(int_codegen_regs, abi.frame_pointer)
        zero_reg = _resolve_reg_name(int_codegen_regs, "zero")
        ra_reg = _resolve_reg_name(int_codegen_regs, "ra")

        def _alias_to_reg(alias: str) -> str:
            # Search every register file so float ABI aliases (e.g. fa0) resolve too.
            for reg in isa_reg.registers:
                if alias in reg.aliases:
                    return f"{reg.prefix}{reg.aliases[alias]}"
            return alias

        abi_arg_regs = [_alias_to_reg(a) for a in abi.arg_registers]
        abi_ret_regs = [_alias_to_reg(r) for r in abi.ret_registers]
        abi_saved_regs = [_alias_to_reg(s) for s in abi.callee_saved]
        abi_fp_arg_regs = [_alias_to_reg(a) for a in abi.fp_arg_registers]
        abi_fp_ret_regs = [_alias_to_reg(r) for r in abi.fp_ret_registers]

        # The floating-point register class (first float file), if any.
        fp_reg_class = next(
            (rc["class_name"] for rc in reg_classes if rc["is_float"]), None
        )

        # Instruction-encoding width (distinct from the data width `xlen`).
        # Uniform per ISA, any size up to a 512-bit hard cap (shared with QEMU gen).
        _w = compute_insn_width(isa_reg, ISA, max_bits=512)
        insn_bits = _w["insn_bits"]
        insn_bytes = _w["insn_bytes"]
        insn_uint = _w["insn_uint"]
        schema_len = insn_bits

        instr_defs = _build_instr_defs(isa_reg, ISA, skip_regfiles=skip_regfiles)

        # Resolve compiler roles (declared tags + behavior inference), then bind the
        # key opcodes from the role map, falling back to name/category heuristics.
        compiler_roles, role_conflicts = _collect_compiler_roles(instr_defs, xlen)

        def _role_or(role_keys: list, heuristic: Optional[str]) -> Optional[str]:
            for k in role_keys:
                if k in compiler_roles:
                    return compiler_roles[k]
            return heuristic

        addi_opcode = _role_or(["frame.sp_adjust", "const.lo", "alu_ri.add"],
                               _find_sp_adjust_opcode(instr_defs))
        lui_opcode = _role_or(["const.hi", "global.hi"], _find_lui_opcode(instr_defs))
        jal_opcode = _role_or(["control.jump", "control.call"], _find_jal_opcode(instr_defs))
        jalr_opcode = _role_or(["control.call_indirect", "control.ret"],
                               _find_jalr_opcode(instr_defs))
        const_strategy = _infer_const_strategy(compiler_roles, instr_defs)
        # Opcodes used by the integer-constant materialization path (ISelDAGToDAG).
        # The global-address path keeps using lui_opcode/addi_opcode (sign-extended
        # %hi/%lo relocation convention), independent of this strategy.
        const_hi_opcode = compiler_roles.get("const.hi") or lui_opcode
        const_lo_opcode = compiler_roles.get("const.lo") or addi_opcode
        const_load_opcode = compiler_roles.get("const.load")

        # Full-word store/load for register spill/reload (PEI calls these).
        store_opcode = compiler_roles.get(f"mem.store{xlen}")
        load_opcode = compiler_roles.get(f"mem.load{xlen}")
        # Floating-point spill/reload opcodes (full-width FP store/load), if any.
        fp_store_opcode = next(
            (n for n, i in instr_defs if i["dag_category"] == "store" and i.get("dag_is_float")),
            None,
        )
        fp_load_opcode = next(
            (n for n, i in instr_defs if i["dag_category"] == "load" and i.get("dag_is_float")),
            None,
        )

        # Control-flow opcodes (Phase 1: compare-then-branch + select)
        slt_opcode   = compiler_roles.get("cmp.lt")
        sltu_opcode  = compiler_roles.get("cmp.ltu")
        sltiu_opcode = compiler_roles.get("cmp.ltui")
        xor_opcode   = compiler_roles.get("alu_rr.xor")
        beq_opcode   = compiler_roles.get("branch.eq")
        bne_opcode   = compiler_roles.get("branch.ne")
        # Direct ordering compare-branches (RISC-V style) present?
        has_ordering_branches = any(
            compiler_roles.get(f"branch.{c}") for c in ("lt", "ge", "ltu", "geu")
        )
        # cmp-and-branch-on-zero control flow: no direct ordering branches, but the
        # ISA can set-less-than into a register and branch on (non)zero.
        cmp_branch_path = (not has_ordering_branches) and bool(
            slt_opcode and bne_opcode and zero_reg
        )
        # A custom-inserter Select pseudo needs a branch-on-(non)zero + zero register.
        has_select = bool(bne_opcode and zero_reg)
        # Materializing a comparison into a 0/1 register normally needs a
        # set-less-than instruction (RISC-V SLT etc.). An ISA that branches on
        # comparisons but has no SLT can still do it with a branch diamond — but
        # only if it can make a 1 (add-immediate) and a 0 (zero register).
        setcc_branch_entries = (
            _setcc_branch_entries(compiler_roles)
            if not (slt_opcode or sltu_opcode) else []
        )
        setcc_via_branch = bool(setcc_branch_entries and zero_reg and addi_opcode)
        if not setcc_via_branch:
            setcc_branch_entries = []
        # The custom inserter is emitted for either the Select pseudo or the
        # setcc-via-branch pseudo (or both).
        needs_custom_inserter = has_select or setcc_via_branch
        # Global-address materialization needs a hi/lo (LUI+ADDI) instruction pair.
        # Without it, the ISA simply can't reference globals (honest limitation),
        # rather than emitting patterns that reference a missing opcode.
        has_global_addr = bool(lui_opcode and addi_opcode)
        imm_operands = _collect_imm_operands(isa_reg)
        schema_combined_imm = {
            name: _get_schema_combined_imm(s)
            for name, s in isa_reg.schemas.items()
        }

        # Registers that must never be allocated (zero, sp, gp, tp, frame-pointer)
        reserved_regs: list[str] = []
        if first_reg:
            for alias in ["zero", "sp", "gp", "tp"]:
                if alias in first_reg.aliases:
                    reserved_regs.append(f"{first_reg.prefix}{first_reg.aliases[alias]}")
            if abi.frame_pointer and abi.frame_pointer in first_reg.aliases:
                fp_r = f"{first_reg.prefix}{first_reg.aliases[abi.frame_pointer]}"
                if fp_r not in reserved_regs:
                    reserved_regs.append(fp_r)

        triple_arch = isa_reg.manifest.spec.triple_arch or isa_name

        # ISA-level ELF and encoding metadata
        spec = isa_reg.manifest.spec
        _ELF_BY_TRIPLE: dict[str, int] = {"riscv32": 243, "riscv64": 243, "arm": 40, "mips": 8}
        elf_machine: int = (
            spec.elf_machine
            if spec.elf_machine is not None
            else _ELF_BY_TRIPLE.get(spec.triple_arch or "", 0)
        )
        byte_order: str = getattr(spec, "byte_order", "little")

        # Compute fixup encoding info from split-immediate schemas
        jal_fixup_info = None
        branch_fixup_info = None
        for _sname, cimm in schema_combined_imm.items():
            if cimm is None:
                continue
            if cimm["operand_name"] == "jaltarget" and jal_fixup_info is None:
                jal_fixup_info = _compute_fixup_info(cimm["hw_assignments"], insn_bits)
            elif cimm["operand_name"] == "brtarget" and branch_fixup_info is None:
                branch_fixup_info = _compute_fixup_info(cimm["hw_assignments"], insn_bits)
        # Compute fixup info for absolute-address (non-PC-relative) fixups from schemas
        lui_fixup_info = _compute_single_field_fixup(lui_opcode, instr_defs, isa_reg.schemas, insn_bits)
        addi_fixup_info = _compute_single_field_fixup(addi_opcode, instr_defs, isa_reg.schemas, insn_bits)
        num_fixup_kinds = (
            (1 if jal_fixup_info else 0)
            + (1 if branch_fixup_info else 0)
            + (1 if lui_fixup_info else 0)
            + (1 if addi_fixup_info else 0)
        )

        # NOP C-string: from YAML field, or auto-encoded from the ADDI instruction
        # schema. Bytes are emitted in the ISA's byte order.
        nop_c_str: Optional[str] = None
        if spec.nop_encoding:
            try:
                nop_val = int(spec.nop_encoding, 16)
                raw = nop_val.to_bytes(schema_len // 8,
                                       'big' if byte_order == 'big' else 'little')
                nop_c_str = '"' + ''.join(f'\\x{b:02x}' for b in raw) + '"'
            except Exception:
                pass
        if nop_c_str is None and addi_opcode:
            nop_c_str = _encode_instr_as_nop(
                addi_opcode, instr_defs, isa_reg.schemas, isa_reg, schema_len,
                byte_order=byte_order,
            )

        # ELF relocation name map: explicit YAML override → RISC-V defaults → empty (→ R_NONE)
        elf_reloc_map: dict[str, str]
        if spec.elf_relocations:
            elf_reloc_map = dict(spec.elf_relocations)
        elif elf_machine == 243:  # EM_RISCV
            elf_reloc_map = {
                "jal":    "R_RISCV_JAL",
                "branch": "R_RISCV_BRANCH",
                "hi20":   "R_RISCV_HI20",
                "lo12_i": "R_RISCV_LO12_I",
            }
        else:
            elf_reloc_map = {}  # template falls back to ELF::R_NONE

        # Immediate-width constants derived from schemas (replace magic numbers in templates)
        addi_width: int      = addi_fixup_info["width"] if addi_fixup_info else 12
        lui_width: int       = lui_fixup_info["width"]  if lui_fixup_info  else 20
        lui_compensator: int = 1 << (addi_width - 1)          # 2048 for 12-bit; was 0x800
        lui_mask: int        = (1 << lui_width) - 1            # 0xFFFFF; was hardcoded
        lo_mask: int         = (1 << addi_width) - 1           # 0xFFF;   was hardcoded

        ctx = dict(
            isa_name=isa_name,
            ISA=ISA,
            xlen=xlen,
            registers=codegen_regs,
            schemas=isa_reg.schemas,
            instructions=isa_reg.instructions,
            abi=abi,
            abi_arg_regs=abi_arg_regs,
            abi_ret_regs=abi_ret_regs,
            abi_saved_regs=abi_saved_regs,
            instr_defs=instr_defs,
            sp_reg=sp_reg,
            zero_reg=zero_reg,
            ra_reg=ra_reg,
            addi_opcode=addi_opcode,
            jal_opcode=jal_opcode,
            jalr_opcode=jalr_opcode,
            lui_opcode=lui_opcode,
            frame_pointer_reg=fp_reg,
            first_reg_class=first_reg_class,
            first_reg_prefix=first_reg_prefix,
            abi_stack_alignment=abi.stack_alignment,
            schema_len=schema_len,
            tcg_type="i64" if xlen == 64 else "i32",
            reserved_regs=reserved_regs,
            triple_arch=triple_arch,
            imm_operands=imm_operands,
            schema_combined_imm=schema_combined_imm,
            jal_fixup_info=jal_fixup_info,
            branch_fixup_info=branch_fixup_info,
            lui_fixup_info=lui_fixup_info,
            addi_fixup_info=addi_fixup_info,
            num_fixup_kinds=num_fixup_kinds,
            elf_machine=elf_machine,
            byte_order=byte_order,
            nop_c_str=nop_c_str,
            elf_reloc_map=elf_reloc_map,
            addi_width=addi_width,
            lui_width=lui_width,
            lui_compensator=lui_compensator,
            lui_mask=lui_mask,
            lo_mask=lo_mask,
            const_strategy=const_strategy,
            compiler_roles=compiler_roles,
            const_hi_opcode=const_hi_opcode,
            const_lo_opcode=const_lo_opcode,
            const_load_opcode=const_load_opcode,
            store_opcode=store_opcode,
            load_opcode=load_opcode,
            fp_store_opcode=fp_store_opcode,
            fp_load_opcode=fp_load_opcode,
            slt_opcode=slt_opcode,
            sltu_opcode=sltu_opcode,
            sltiu_opcode=sltiu_opcode,
            xor_opcode=xor_opcode,
            beq_opcode=beq_opcode,
            bne_opcode=bne_opcode,
            has_ordering_branches=has_ordering_branches,
            cmp_branch_path=cmp_branch_path,
            has_select=has_select,
            setcc_via_branch=setcc_via_branch,
            setcc_branch_entries=setcc_branch_entries,
            needs_custom_inserter=needs_custom_inserter,
            has_global_addr=has_global_addr,
            reg_classes=reg_classes,
            reg_value_types=reg_value_types,
            int_value_types=int_value_types,
            float_value_types=float_value_types,
            fp_reg_class=fp_reg_class,
            abi_fp_arg_regs=abi_fp_arg_regs,
            abi_fp_ret_regs=abi_fp_ret_regs,
            insn_bits=insn_bits,
            insn_bytes=insn_bytes,
            insn_uint=insn_uint,
        )

        root = pathlib.Path(output_dir)
        target = root / "llvm" / "lib" / "Target" / ISA
        mcdesc = target / "MCTargetDesc"
        targetinfo = target / "TargetInfo"

        # Target profile (G1): decides what "complete" means for this ISA.
        profile_spec = spec.compiler or CompilerProfile()
        profile = profile_spec.profile
        if profile == "c-baremetal":
            required = _required_roles(xlen)
        elif profile == "kernel-only":
            required = set()
        else:  # custom
            required = set(profile_spec.requires)

        # Non-role prerequisites: lowering C needs the CPU register conventions
        # declared explicitly (sp for the stack, ra for calls, zero for constant
        # materialization & branch-on-zero). These are never invented positionally.
        missing_prereqs: list[str] = []
        if profile == "c-baremetal":
            for prereq, val in (("alias:sp", sp_reg), ("alias:ra", ra_reg),
                                ("alias:zero", zero_reg)):
                if val is None:
                    missing_prereqs.append(prereq)

        custom_instrs = [
            (name, info.get("dag_notes") or [])
            for name, info in instr_defs
            if info["dag_category"] == "custom"
        ]

        # Compiler-coverage diagnostic (Part C)
        report_md, missing_required = _build_coverage_report(
            ISA, compiler_roles, role_conflicts, const_strategy,
            has_ordering_branches=has_ordering_branches,
            xlen=xlen,
            profile=profile,
            required=required,
            missing_prereqs=missing_prereqs,
            custom_instrs=custom_instrs,
        )
        target.mkdir(parents=True, exist_ok=True)
        (target / "COMPILER_COVERAGE.md").write_text(report_md)
        if missing_required:
            logger.warning(
                "%s: compiler backend INCOMPLETE for profile '%s' — missing: %s "
                "(see COMPILER_COVERAGE.md)",
                ISA, profile, ", ".join(missing_required),
            )
            if strict:
                raise ValueError(
                    f"{ISA}: profile '{profile}' is missing {missing_required}. "
                    f"Tag instructions with compiler.roles, declare the missing "
                    f"register aliases, or set spec.compiler.profile to match "
                    f"the target (kernel-only for stack-less compute ISAs)."
                )
        else:
            logger.info("%s: compiler backend COMPILER-COMPLETE for profile '%s' "
                        "(strategy=%s)", ISA, profile, const_strategy)

        def render_to(template_name: str, dest: pathlib.Path):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(env.get_template(template_name).render(**ctx))

        # TableGen files
        render_to("llvm/llvm_root.td.j2",           target / f"{ISA}.td")
        render_to("llvm/llvm_register_info.td.j2",  target / f"{ISA}RegisterInfo.td")
        render_to("llvm/llvm_instr_formats.td.j2",  target / f"{ISA}InstrFormats.td")
        render_to("llvm/llvm_instr_info.td.j2",     target / f"{ISA}InstrInfo.td")
        render_to("llvm/llvm_calling_conv.td.j2",   target / f"{ISA}CallingConv.td")
        render_to("llvm/llvm_schedule.td.j2",       target / f"{ISA}Schedule.td")

        # Top-level header
        render_to("llvm/llvm_isa_h.j2",             target / f"{ISA}.h")

        # C++ backend files
        render_to("llvm/llvm_target_machine.h.j2",    target / f"{ISA}TargetMachine.h")
        render_to("llvm/llvm_target_machine.cpp.j2",  target / f"{ISA}TargetMachine.cpp")
        render_to("llvm/llvm_subtarget.h.j2",         target / f"{ISA}Subtarget.h")
        render_to("llvm/llvm_subtarget.cpp.j2",       target / f"{ISA}Subtarget.cpp")
        render_to("llvm/llvm_register_info.h.j2",     target / f"{ISA}RegisterInfo.h")
        render_to("llvm/llvm_register_info.cpp.j2",   target / f"{ISA}RegisterInfo.cpp")
        render_to("llvm/llvm_instr_info.h.j2",        target / f"{ISA}InstrInfo.h")
        render_to("llvm/llvm_instr_info.cpp.j2",      target / f"{ISA}InstrInfo.cpp")
        render_to("llvm/llvm_isel_lowering.h.j2",     target / f"{ISA}ISelLowering.h")
        render_to("llvm/llvm_isel_lowering.cpp.j2",   target / f"{ISA}ISelLowering.cpp")
        render_to("llvm/llvm_isel_dag_to_dag.cpp.j2", target / f"{ISA}ISelDAGToDAG.cpp")
        render_to("llvm/llvm_asm_printer.cpp.j2",     target / f"{ISA}AsmPrinter.cpp")
        render_to("llvm/llvm_frame_lowering.h.j2",    target / f"{ISA}FrameLowering.h")
        render_to("llvm/llvm_frame_lowering.cpp.j2",  target / f"{ISA}FrameLowering.cpp")
        render_to("llvm/llvm_cmakelists.j2",          target / "CMakeLists.txt")

        # MCTargetDesc/
        render_to("llvm/llvm_mc_target_desc.h.j2",     mcdesc / f"{ISA}MCTargetDesc.h")
        render_to("llvm/llvm_mc_target_desc.cpp.j2",   mcdesc / f"{ISA}MCTargetDesc.cpp")
        render_to("llvm/llvm_mc_asm_info.h.j2",        mcdesc / f"{ISA}MCAsmInfo.h")
        render_to("llvm/llvm_mc_asm_info.cpp.j2",      mcdesc / f"{ISA}MCAsmInfo.cpp")
        render_to("llvm/llvm_fixup_kinds.h.j2",        mcdesc / f"{ISA}FixupKinds.h")
        render_to("llvm/llvm_mc_code_emitter.cpp.j2",  mcdesc / f"{ISA}MCCodeEmitter.cpp")
        render_to("llvm/llvm_inst_printer.h.j2",       mcdesc / f"{ISA}InstPrinter.h")
        render_to("llvm/llvm_inst_printer.cpp.j2",     mcdesc / f"{ISA}InstPrinter.cpp")
        render_to("llvm/llvm_asm_backend.cpp.j2",      mcdesc / f"{ISA}AsmBackend.cpp")
        render_to("llvm/llvm_elf_object_writer.cpp.j2", mcdesc / f"{ISA}ELFObjectWriter.cpp")
        render_to("llvm/llvm_mc_cmakelists.j2",        mcdesc / "CMakeLists.txt")

        # TargetInfo/
        render_to("llvm/llvm_target_info.h.j2",          targetinfo / f"{ISA}TargetInfo.h")
        render_to("llvm/llvm_target_info.cpp.j2",        targetinfo / f"{ISA}TargetInfo.cpp")
        render_to("llvm/llvm_targetinfo_cmakelists.j2",  targetinfo / "CMakeLists.txt")

        # Integration helpers at root
        patch_sh = root / "patch_llvm.sh"
        render_to("llvm/llvm_patch_sh.j2",    patch_sh)
        patch_sh.chmod(patch_sh.stat().st_mode | 0o111)
        render_to("llvm/llvm_integrate_md.j2", root / "INTEGRATE.md")

    logger.info(f"Generated complete LLVM target in {output_dir}")
