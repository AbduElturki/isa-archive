"""Generate descriptive C++ headers for an ISA — enums, a per-instruction metadata
table, and decode/field helpers a consumer adopts into their own functional or cycle
model. Mirrors the shape of nisa-archive's generated C++ (decode + metadata + latency),
not an executing simulator.
"""
import logging
import pathlib
import re

from ..compiler.loader import Registry
from ..compiler.utils import compute_insn_width
from ..models.enums import FieldRole
from ..models.scalar_types import of_register
from .base import make_jinja_env, prepare_output_dir, write_generated, CLANG_FORMAT_LLVM
from .llvm import _get_schema_combined_imm

logger = logging.getLogger("isa_archive.generators")


def _ident(name: str) -> str:
    """Sanitize an arbitrary name into a valid C++ identifier."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return "_" + s if s and s[0].isdigit() else s


def _uarch_latencies(registry: Registry, isa_name: str) -> dict[str, int]:
    """exec_type -> latency, taken from the first uArch associated with this ISA."""
    out: dict[str, int] = {}
    for uarch in registry.uarches.values():
        if uarch.isa.name != isa_name:
            continue
        for block in uarch.blocks:
            for et in block.handles:
                out.setdefault(et, block.latency)
    return out


def _instr_info(instr, isa_reg, latencies: dict[str, int]) -> dict:
    """Build the descriptive record for one instruction."""
    name = instr.metadata.name
    schema = isa_reg.schemas[instr.spec.schema_name]
    reg_classes = {r.name for r in isa_reg.registers}

    mask = 0
    match = 0
    operands: list[dict] = []
    for f in schema.spec.fields:
        if f.role in (FieldRole.OPCODE, FieldRole.CONSTANT, FieldRole.RESERVED):
            field_mask = ((1 << f.width) - 1) << f.start
            if f.role == FieldRole.RESERVED:
                val = 0
            elif f.role == FieldRole.OPCODE:
                val = isa_reg._resolve_value(instr.spec.opcode)
            else:  # CONSTANT
                cv = instr.spec.constants.get(f.name)
                if cv is None:
                    continue  # unconstrained constant field — not part of the match
                val = isa_reg._resolve_value(cv)
            mask |= field_mask
            match |= (val << f.start) & field_mask
        elif f.role == FieldRole.REGISTER:
            operands.append({"name": f.name, "kind": "Reg", "start": f.start,
                             "width": f.width, "signed": False,
                             "rc": f.type if f.type in reg_classes else "none"})
        elif f.role == FieldRole.IMMEDIATE:
            operands.append({"name": f.name, "kind": "Imm", "start": f.start,
                             "width": f.width, "signed": f.is_signed, "rc": "none"})

    # Immediate reconstruction (single field, or split via the imm_<hi>_<lo> layout).
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

    opcode_val = 0
    for f in schema.spec.fields:
        if f.role == FieldRole.OPCODE:
            opcode_val = isa_reg._resolve_value(instr.spec.opcode)
            break

    exec_type = instr.spec.exec_type or ""
    asm_ops = ", ".join(o["name"] for o in operands)
    behavior = " ".join(instr.spec.behavior.split())
    return {
        "enum": _ident(name.upper()),
        "mnemonic": name.lower(),
        "schema": instr.spec.schema_name,
        "opcode": opcode_val,
        "mask": mask,
        "match": match,
        "category": _ident(exec_type) if exec_type else "none",
        "exec_type": exec_type,
        "behavior": behavior.replace("\\", "\\\\").replace('"', '\\"'),
        "description": instr.metadata.description or "",
        "latency": latencies.get(exec_type, 1),
        "operands": operands,
        "asm_format": f"{name.lower()} {asm_ops}".rstrip(),
        "imm": imm,
    }


def generate_cpp_isa(registry: Registry, output_dir: str, clang_format: bool = False):
    env = make_jinja_env()
    root = prepare_output_dir(output_dir)
    write_generated(root / ".clang-format", CLANG_FORMAT_LLVM)

    for isa_reg in registry.isas.values():
        isa_name = isa_reg.name
        ns = _ident(isa_name)
        guard = _ident(isa_name).upper()

        try:
            insn = compute_insn_width(isa_reg, isa_name, max_bits=64,
                                      limit_hint=("The C++ model decodes one instruction word "
                                                  "into a uint64_t; encodings wider than 64 bits "
                                                  "are not supported by this target yet."))
        except ValueError as e:
            logger.warning("%s: skipping C++ model — %s", isa_name, e)
            continue

        latencies = _uarch_latencies(registry, isa_name)
        instrs = [_instr_info(i, isa_reg, latencies) for i in isa_reg.instructions.values()]
        # Decode in order of most-specific match first (more fixed bits), so an
        # instruction whose fixed bits subsume another's can't shadow it.
        decode_order = sorted(instrs, key=lambda it: bin(it["mask"]).count("1"), reverse=True)

        # Distinct exec_type categories (in first-seen order), always with `none` first.
        categories = ["none"]
        for it in instrs:
            if it["category"] not in categories:
                categories.append(it["category"])

        reg_classes = [{"name": _ident(r.name), "raw": r.name, "width": r.width,
                        "count": r.count, "is_float": r.is_float,
                        "ctype": (of_register(r).c_type or f"uint{r.width}_t")}
                       for r in isa_reg.registers]

        ctx = dict(isa_name=isa_name, ns=ns, guard=guard,
                   insn_bits=insn["insn_bits"], insn_bytes=insn["insn_bytes"],
                   instrs=instrs, decode_order=decode_order, categories=categories,
                   reg_classes=reg_classes, has_uarch=bool(latencies))

        out = root / ns
        out.mkdir(parents=True, exist_ok=True)

        def render(tpl: str, name: str):
            content = env.get_template(f"cpp_isa/{tpl}").render(**ctx)
            write_generated(out / name, content, clang_format=clang_format)

        render("enums.h.j2", f"{ns}_enums.h")
        render("info.h.j2", f"{ns}_info.h")
        render("decode.h.j2", f"{ns}_decode.h")
        render("model.h.j2", f"{ns}_model.h")
        render("example_main.cpp.j2", "example_main.cpp")
        render("INTEGRATE.md.j2", "INTEGRATE.md")

    logger.info(f"Generated C++ ISA headers in {output_dir}")
