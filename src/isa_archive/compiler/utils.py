import ast as _ast
import re as _re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .loader import ISARegistry
    from ..models import Schema, Instruction

from ..models.enums import FieldRole


def csr_map(isa_reg: "ISARegistry") -> dict:
    """{csr_name → ISACSR}, the form BehaviorIR expects for `csr.*` width inference."""
    return {c.name: c for c in (getattr(isa_reg, "arch_csrs", None) or [])}


def build_regfile_shapes(isa_reg: "ISARegistry") -> dict:
    """{register-file name → (element ScalarType, shape list)} for shaped files only.
    Lets BehaviorIR + the backends resolve element indexing `vd[i]` / `vd[i][j]`."""
    from ..models.scalar_types import of_register
    return {r.name: (of_register(r), list(r.shape))
            for r in isa_reg.registers if getattr(r, "is_shaped", False)}


def build_regfile_attrs(isa_reg: "ISARegistry") -> dict:
    """{register-file name → {attr name → width}} for files with per-register
    attributes. Lets BehaviorIR + the backends resolve `reg.attr` access."""
    return {r.name: {a.name: a.width for a in r.attributes}
            for r in isa_reg.registers if getattr(r, "attributes", None)}


def build_csr_info(isa_reg: "ISARegistry") -> dict:
    """{csr_name → {"width": int, "fields": {field → (start, width)}}} for the
    QEMU C backend's CSR read/write lowering."""
    info = {}
    for c in (getattr(isa_reg, "arch_csrs", None) or []):
        fields = {f.name: (f.start, f.end - f.start + 1) for f in (c.fields or [])}
        info[c.name] = {"width": c.width, "fields": fields}
    return info


def build_trap_info(isa_reg: "ISARegistry"):
    """Resolve the ISA's `trap:` block into the dict the QEMU C backend consumes,
    or None if the ISA declares no trap wiring."""
    trap = getattr(isa_reg, "trap", None)
    if not trap:
        return None
    return {
        "vector_csr": trap.vector_csr,
        "epc_csr": trap.epc_csr,
        "cause_csr": trap.cause_csr,
        "status_csr": trap.status_csr,
        "causes": dict(trap.causes),
    }


def compute_fixed_fields(instr: "Instruction", schema: "Schema",
                         isa_reg: "ISARegistry") -> list:
    """Resolve every fixed (OPCODE / CONSTANT / RESERVED) field of an instruction.

    Returns ``[(SchemaField, value:int), …]`` — RESERVED is 0, OPCODE comes from
    ``instr.spec.opcode``, CONSTANT from ``instr.spec.constants`` — using the
    registry's value resolver (which handles named constants and enum members).
    Fields whose value can't be resolved (or an unconstrained constant) are
    omitted. The single source of truth for the LLVM and C++ backends, which
    each format these differently (binary-string fixed fields vs mask/match).
    """
    out = []
    for f in schema.spec.fields:
        if f.role == FieldRole.RESERVED:
            out.append((f, 0))
        elif f.role == FieldRole.OPCODE:
            try:
                out.append((f, isa_reg._resolve_value(instr.spec.opcode)))
            except Exception:
                pass
        elif f.role == FieldRole.CONSTANT:
            cv = instr.spec.constants.get(f.name)
            if cv is not None:
                try:
                    out.append((f, isa_reg._resolve_value(cv)))
                except Exception:
                    pass
    return out


def compute_decode_fields(instr: "Instruction", schema: "Schema",
                          isa_reg: "ISARegistry") -> dict:
    """Per-instruction decode metadata shared by the byte-array decoders (cpp-isa,
    and QEMU's >64-bit path). Returns:

    - ``fixed``: ``[{start, width, val}]`` — the opcode/constant/reserved bits to
      match (from :func:`compute_fixed_fields`).
    - ``operands``: ``[{name, kind, start, width, signed, rc}]`` — register
      (``kind="Reg"``) and immediate (``kind="Imm"``) fields, in schema order.
    - ``imm``: the immediate reconstruction (``None`` if no immediate). Either a
      single field ``{combined: False, width, signed, start}`` or a split layout
      ``{combined: True, width, signed: True, parts: [{hw_low, hw_width, imm_low}]}``.

    The single source of truth so the assembler, cpp-isa, and QEMU agree bit-for-bit.
    """
    # Lazy import: this lives in compiler/, and generators/ already imports
    # compiler.utils — importing it at module scope would be circular.
    from ..generators.llvm import _get_schema_combined_imm

    reg_classes = {r.name for r in isa_reg.registers}

    # Fixed-field match conditions. A field wider than 64 bits is split into <=64-bit
    # chunks so the byte-array decoders (whose get_bits returns uint64_t) check every
    # bit — without this a >64-bit reserved/constant field would be matched with a
    # truncated value, silently ignoring bits >= 64. <=64-bit fields pass through
    # unchanged (one chunk == the whole field), so narrow ISAs are byte-identical.
    fixed = []
    for f, val in compute_fixed_fields(instr, schema, isa_reg):
        off = 0
        while off < f.width:
            w = min(64, f.width - off)
            fixed.append({"start": f.start + off, "width": w,
                          "val": (val >> off) & ((1 << w) - 1)})
            off += w

    operands: list[dict] = []
    for f in schema.spec.fields:
        if f.role == FieldRole.REGISTER:
            operands.append({"name": f.name, "kind": "Reg", "start": f.start,
                             "width": f.width, "signed": False,
                             "rc": f.type if f.type in reg_classes else "none"})
        elif f.role == FieldRole.IMMEDIATE:
            operands.append({"name": f.name, "kind": "Imm", "start": f.start,
                             "width": f.width, "signed": f.is_signed, "rc": "none"})

    cimm = _get_schema_combined_imm(schema)
    imm = None
    if cimm:
        parts = [{"hw_low": hw_low, "hw_width": hw_high - hw_low + 1, "imm_low": imm_low}
                 for (hw_high, hw_low, imm_high, imm_low) in cimm["hw_assignments"]]
        imm = {"combined": True, "width": cimm["width"], "signed": True, "parts": parts}
    else:
        imm_fields = [f for f in schema.spec.fields if f.role == FieldRole.IMMEDIATE]
        if imm_fields:
            f = imm_fields[0]
            imm = {"combined": False, "width": f.width, "signed": f.is_signed,
                   "start": f.start}

    return {"fixed": fixed, "operands": operands, "imm": imm}


def sanitize_ident(name: str) -> str:
    """Turn an arbitrary name into a valid C/C++/Rust identifier."""
    s = _re.sub(r"[^A-Za-z0-9_]", "_", name)
    return "_" + s if s and s[0].isdigit() else s


def isa_ident(name: str, upper: bool = True) -> str:
    """Normalize an ISA name for use as a C++ namespace / LLVM target prefix:
    hyphens and slashes become underscores (optionally upper-cased)."""
    s = name.upper() if upper else name
    return s.replace("-", "_").replace("/", "_")


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
