"""Generate descriptive C++ headers for an ISA - enums, a per-instruction metadata
table, and decode/field helpers a consumer adopts into their own functional or cycle
model. Mirrors the shape of nisa-archive's generated C++ (decode + metadata + latency),
not an executing simulator.
"""
import logging
import pathlib
import re

from ..compiler.loader import Registry
from ..compiler.utils import compute_insn_width, compute_decode_fields, sanitize_ident as _ident
from ..models.enums import FieldRole
from ..models.scalar_types import of_register
from .base import make_jinja_env, prepare_output_dir, write_generated, make_renderer, CLANG_FORMAT_LLVM

logger = logging.getLogger("isa_archive.generators")


def _pascal(name: str) -> str:
    """ISA name → PascalCase prefix for the generated header file names
    (e.g. 'npu-probe' → 'NpuProbe', 'pico32' → 'Pico32')."""
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", name) if p)


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

    # fixed fields (for wide-word decode), register/immediate operands, and the
    # immediate-reconstruction layout - shared with the QEMU >64-bit decoder.
    df = compute_decode_fields(instr, schema, isa_reg)
    fixed, operands, imm = df["fixed"], df["operands"], df["imm"]

    # mask/match for the narrow (<=64-bit) uint64 decode path.
    mask = 0
    match = 0
    for f in fixed:
        field_mask = ((1 << f["width"]) - 1) << f["start"]
        mask |= field_mask
        match |= (f["val"] << f["start"]) & field_mask

    opcode_val = 0
    for f in schema.spec.fields:
        if f.role == FieldRole.OPCODE:
            opcode_val = isa_reg._resolve_value(instr.spec.opcode)
            break

    exec_type = instr.spec.exec_type or ""
    asm_ops = ", ".join(o["name"] for o in operands)
    behavior = " ".join(instr.spec.behavior.split())
    # Encoder signature: one `unsigned` per register operand, then one `int64_t imm`
    # if the instruction has an immediate (split immediates take the single logical
    # value and are distributed across their hardware fields by the encoder).
    enc_args = [f"unsigned {o['name']}" for o in operands if o["kind"] == "Reg"]
    if imm is not None:
        enc_args.append("int64_t imm")
    # Field masks for the encoder (Jinja has no shift operator, so precompute them).
    for o in operands:
        o["mask"] = (1 << o["width"]) - 1
    if imm is not None:
        if imm["combined"]:
            for p in imm["parts"]:
                p["mask"] = (1 << p["hw_width"]) - 1
        else:
            imm["mask"] = (1 << imm["width"]) - 1
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
        "fixed": fixed,
        "enc_signature": ", ".join(enc_args),
    }


def generate_cpp_isa(registry: Registry, output_dir: str, clang_format: bool = False):
    env = make_jinja_env()
    root = prepare_output_dir(output_dir)
    write_generated(root / ".clang-format", CLANG_FORMAT_LLVM)

    for isa_reg in registry.isas.values():
        isa_name = isa_reg.name
        ns = _ident(isa_name)            # namespace (lowercase): npu_probe
        cls = _pascal(isa_name)          # header file prefix (PascalCase): NpuProbe
        guard = _ident(isa_name).upper()

        try:
            insn = compute_insn_width(isa_reg, isa_name, max_bits=512,
                                      limit_hint=("The C++ model decodes instruction words up to "
                                                  "512 bits."))
        except ValueError as e:
            logger.warning("%s: skipping C++ model - %s", isa_name, e)
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
                        "element": (r.type if r.is_shaped else None),
                        "shape": (list(r.shape) if r.is_shaped else None),
                        # element C++ type for shaped files (the total width has no C type)
                        "ctype": (of_register(r).eff_cpp_type
                                  or f"uint{r.element_width if r.is_shaped else r.width}_t")}
                       for r in isa_reg.registers]

        # Register files whose element type is a custom (library) C++ type declare a
        # `cpp_include` (defaulting to `c_include`). Surface that as a per-file element
        # typedef plus the header to include. Built-in types add nothing.
        from ..models.scalar_types import format_include
        elem_includes: list[str] = []
        elem_types: list[dict] = []
        for r in isa_reg.registers:
            st = of_register(r)
            if st.eff_cpp_include and st.eff_cpp_type:
                inc = format_include(st.eff_cpp_include)
                if inc not in elem_includes:
                    elem_includes.append(inc)
                elem_types.append({"name": _ident(r.name), "ctype": st.eff_cpp_type})

        ctx = dict(isa_name=isa_name, ns=ns, cls=cls, guard=guard,
                   insn_bits=insn["insn_bits"], insn_bytes=insn["insn_bytes"],
                   wide=insn["insn_bits"] > 64,   # >64-bit words use a byte-array Word
                   instrs=instrs, decode_order=decode_order, categories=categories,
                   reg_classes=reg_classes, has_uarch=bool(latencies),
                   elem_includes=elem_includes, elem_types=elem_types)

        out = root / cls
        out.mkdir(parents=True, exist_ok=True)

        render = make_renderer(env, ctx, clang_format=clang_format)
        render("cpp_isa/enums.h.j2", out / f"{cls}Enums.h")
        render("cpp_isa/info.h.j2", out / f"{cls}InstrInfo.h")
        render("cpp_isa/decode.h.j2", out / f"{cls}Decoder.h")
        render("cpp_isa/encoder.h.j2", out / f"{cls}Encoder.h")
        render("cpp_isa/model.h.j2", out / f"{cls}.h")
        render("cpp_isa/example_main.cpp.j2", out / "example_main.cpp")
        render("cpp_isa/INTEGRATE.md.j2", out / "INTEGRATE.md")

    logger.info(f"Generated C++ ISA headers in {output_dir}")
