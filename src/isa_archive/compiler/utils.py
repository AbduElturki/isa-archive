import ast as _ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .loader import ISARegistry
    from ..models import Schema, Instruction

from ..models.enums import FieldRole


def compute_insn_width(isa_reg: "ISARegistry", name: str, max_bits: int = 512,
                       limit_hint: str = "") -> dict:
    """Resolve an ISA's uniform instruction-encoding width (distinct from data `xlen`).

    All schemas must share one length, ≤ ``max_bits``. Returns a dict with
    ``insn_bits`` (int), ``insn_bytes`` (int), and ``insn_uint`` (the C unsigned
    type that holds an instruction word, or ``None`` for >64-bit / APInt encoding).
    Raises ValueError on mixed widths or over-limit widths; ``limit_hint`` is
    appended to the over-limit error to explain where the ceiling comes from.
    """
    bits = isa_reg.xlen
    schemas = getattr(isa_reg, "schemas", None)
    if schemas:
        lengths = {s.spec.length for s in schemas.values()}
        if len(lengths) > 1:
            raise ValueError(
                f"{name}: mixed instruction widths {sorted(lengths)} are not supported; "
                f"all schemas must share one uniform width."
            )
        bits = next(iter(lengths))
    if bits > max_bits:
        raise ValueError(
            f"{name}: instruction width {bits} exceeds the {max_bits}-bit limit."
            + (f" {limit_hint}" if limit_hint else "")
        )
    if bits <= 32:
        uint = "uint32_t"
    elif bits <= 64:
        uint = "uint64_t"
    else:
        uint = None
    return {"insn_bits": bits, "insn_bytes": (bits + 7) // 8, "insn_uint": uint}


def instruction_pattern(instr: "Instruction", schema: "Schema", fill: str = ".") -> str:
    """Build a bit-pattern string for an instruction given its schema.

    Fixed fields (opcode, constant) are filled with their binary values.
    Reserved fields are filled with 0s.
    All other bit positions use `fill`.
    """
    pattern = [fill] * schema.spec.length
    instr_fixed = {"opcode": instr.spec.opcode}
    instr_fixed.update(instr.spec.constants)
    for name, value in instr_fixed.items():
        field = next((f for f in schema.spec.fields if f.name == name), None)
        if field is None:
            continue
        bin_val = format(value, f'0{field.width}b')
        for i, bit in enumerate(reversed(bin_val)):
            pattern[schema.spec.length - 1 - (field.start + i)] = bit
    for field in schema.spec.fields:
        if field.role == FieldRole.RESERVED:
            for i in range(field.width):
                pattern[schema.spec.length - 1 - (field.start + i)] = "0"
    return "".join(pattern)


def constraint_to_c(expr: str, field_prefix: str = "") -> str:
    """Translate a Python-style boolean expression to C.

    field_prefix is prepended to every variable name — use "a->" for QEMU trans_ context.
    Python and C share ==, !=, <, >, <=, >=, +, -, *, /, %, &, |, ^, <<, >>.
    Only and/or/not differ and are translated here.
    """
    tree = _ast.parse(expr, mode='eval').body
    return _c_expr(tree, field_prefix)


def _c_expr(node, prefix: str) -> str:
    if isinstance(node, _ast.BoolOp):
        op = "&&" if isinstance(node.op, _ast.And) else "||"
        return f" {op} ".join(f"({_c_expr(v, prefix)})" for v in node.values)
    if isinstance(node, _ast.UnaryOp) and isinstance(node.op, _ast.Not):
        return f"!({_c_expr(node.operand, prefix)})"
    if isinstance(node, _ast.Compare):
        _cmp = {"Eq": "==", "NotEq": "!=", "Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">="}
        result = _c_expr(node.left, prefix)
        for op, comp in zip(node.ops, node.comparators):
            result += f" {_cmp[type(op).__name__]} {_c_expr(comp, prefix)}"
        return result
    if isinstance(node, _ast.BinOp):
        _bin = {"Add": "+", "Sub": "-", "Mult": "*", "Div": "/", "Mod": "%",
                "BitAnd": "&", "BitOr": "|", "BitXor": "^", "LShift": "<<", "RShift": ">>"}
        return f"{_c_expr(node.left, prefix)} {_bin[type(node.op).__name__]} {_c_expr(node.right, prefix)}"
    if isinstance(node, _ast.Name):
        return f"{prefix}{node.id}"
    if isinstance(node, _ast.Constant):
        return str(node.value)
    raise ValueError(f"Unsupported constraint expression: {_ast.dump(node)}")


def build_reg_maps(schema: "Schema | None", isa_reg: "ISARegistry") -> tuple[dict[str, str], dict[str, int]]:
    """Build (register_map, var_widths) for a given schema + ISA context.

    register_map maps schema field names to their architectural state name.
    var_widths maps every variable name (fields, registers, CSRs, pc) to its bit width.
    """
    reg_map: dict[str, str] = {}
    var_widths: dict[str, int] = {}
    reg_width_map = {r.name: r.width for r in isa_reg.registers}

    if schema:
        for field in schema.spec.fields:
            if field.maps_to_state:
                reg_map[field.name] = field.maps_to_state
                var_widths[field.name] = reg_width_map.get(field.maps_to_state, isa_reg.xlen)
            elif field.operand and field.operand in isa_reg.operands:
                op = isa_reg.operands[field.operand]
                if op.spec.maps_to_state:
                    reg_map[field.name] = op.spec.maps_to_state
                    var_widths[field.name] = reg_width_map.get(op.spec.maps_to_state, isa_reg.xlen)
                else:
                    var_widths[field.name] = field.end - field.start + 1
            else:
                var_widths[field.name] = field.end - field.start + 1

    for reg in isa_reg.registers:
        var_widths[reg.name] = reg.width
        for alias in reg.aliases:
            var_widths[alias] = reg.width
    for csr in isa_reg.arch_csrs:
        var_widths[csr.name] = csr.width
    var_widths["pc"] = isa_reg.xlen

    return reg_map, var_widths
