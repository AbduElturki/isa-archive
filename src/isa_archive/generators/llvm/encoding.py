"""Immediate-field, split-immediate, fixup, and NOP-encoding helpers for the
LLVM backend (the bit-level encoding logic)."""
import re
from typing import Optional

from ...models.enums import FieldRole


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
      - width: int              - total bits in the combined immediate (max_bit + 1)
      - includes_lsb: bool      - True if any field covers bit 0 (→ data offset like S-type)
                                  False → branch/jump target (bit 0 always 0, like B/J-type)
      - operand_name: str       - "simm{N}" for data offsets, "brtarget"/"jaltarget" for targets
      - hw_assignments: list    - [(hw_high, hw_low, imm_high, imm_low), …] for bit routing
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
            return None  # Naming convention not followed - fall back to split fields
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
            "width": width,  # field bit-width (the wide/APInt fixup path uses this)
            "mask_hex": f"0x{(1 << width) - 1:X}u",
        })
    return {
        "keep_mask_hex": _mask_hex(keep_mask, insn_bits),
        "pieces": pieces,
    }


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
