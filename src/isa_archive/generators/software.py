import logging
from enum import StrEnum
from typing import Optional

from ..compiler.loader import Registry
from ..compiler.utils import constraint_to_c, isa_ident
from .base import make_jinja_env, prepare_output_dir, write_generated
from .llvm import _build_instr_defs

logger = logging.getLogger("isa_archive.generators")


class SoftwareLang(StrEnum):
    C    = "c"
    RUST = "rust"


# ── Operand → concrete language type ─────────────────────────────────────────
# Inline-asm wrappers can only carry operands that live in a single
# general-purpose register and have a portable scalar type. Anything else
# (floats, sub-/super-register-width files, vector files) returns None and the
# instruction is skipped - those need typed reg classes or vector intrinsics
# (a separate capability), not a scalar asm wrapper.
_C_INT = {8: "uint8_t", 16: "uint16_t", 32: "uint32_t", 64: "uint64_t"}
_RUST_INT = {8: "u8", 16: "u16", 32: "u32", 64: "u64"}


def _reg_type(width: int, is_float: bool, lang: SoftwareLang) -> Optional[str]:
    if is_float:
        return None  # needs an architecture-specific float reg class
    return (_C_INT if lang == SoftwareLang.C else _RUST_INT).get(width)


def _imm_type(width: int, signed: bool, lang: SoftwareLang) -> str:
    w = next((cand for cand in (8, 16, 32, 64) if width <= cand), 64)
    if lang == SoftwareLang.C:
        return f"{'int' if signed else 'uint'}{w}_t"
    return f"{'i' if signed else 'u'}{w}"


def _gather_constraints(instr, schema) -> list[dict]:
    all_constraints = list(schema.spec.constraints) + list(instr.spec.constraints)
    return [
        {"c_expr": constraint_to_c(c.expr), "message": c.message or c.expr}
        for c in all_constraints
    ]


def _intrinsic_context(instr, info, schema, lang: SoftwareLang) -> Optional[dict]:
    """Resolve one instruction's structured operands into a render context, or
    None if it can't be expressed as a scalar inline-asm wrapper."""
    name = instr.metadata.name
    out_ops = info["out_ops"]
    in_reg_ops = info["in_reg_ops"]
    in_imm_ops = info["in_imm_ops"]

    if len(out_ops) > 1:
        logger.warning("intrinsics: '%s' writes %d registers; a single-value "
                       "wrapper can't represent it - skipped",
                       name, len(out_ops))
        return None

    out = None
    if out_ops:
        t = _reg_type(out_ops[0]["width"], out_ops[0]["is_float"], lang)
        if t is None:
            logger.warning("intrinsics: '%s' output '%s' has no scalar register "
                           "type - skipped", name, out_ops[0]["name"])
            return None
        out = {"name": out_ops[0]["name"], "type": t}

    in_regs = []
    for op in in_reg_ops:
        t = _reg_type(op["width"], op["is_float"], lang)
        if t is None:
            logger.warning("intrinsics: '%s' operand '%s' has no scalar register "
                           "type - skipped", name, op["name"])
            return None
        in_regs.append({"name": op["name"], "type": t})

    in_imms = [
        {"name": op["name"], "type": _imm_type(op["width"], op["signed"], lang)}
        for op in in_imm_ops
    ]

    return {
        "name": name,
        "mnemonic": name.lower(),         # matches the generated assembler/LLVM
        "fn_name": f"isa_archive_{name.lower()}",
        "behavior": instr.spec.behavior,
        "is_custom": info["dag_category"] == "custom",
        "out": out,
        "in_regs": in_regs,
        "in_imms": in_imms,
        "constraints": _gather_constraints(instr, schema),
    }


# ── Per-language renderers ───────────────────────────────────────────────────
def _operand_order(ctx) -> list[str]:
    """Operand names in assembly-placeholder order: output, input regs, imms."""
    names = [ctx["out"]["name"]] if ctx["out"] else []
    names += [o["name"] for o in ctx["in_regs"]]
    names += [o["name"] for o in ctx["in_imms"]]
    return names


def _doc(ctx) -> str:
    note = "  (custom - no compiler codegen; inline asm only)" if ctx["is_custom"] else ""
    return f"{ctx['name']}: {ctx['behavior']}{note}"


def _render_c(ctx) -> str:
    placeholders = ", ".join(f"%{i}" for i in range(len(_operand_order(ctx))))
    asm = f"{ctx['mnemonic']} {placeholders}".rstrip()
    out_clause = f'"=r"({{rd}})' if ctx["out"] else ""
    in_clause = ", ".join(
        [f'"r"({o["name"]})' for o in ctx["in_regs"]]
        + [f'"i"({o["name"]})' for o in ctx["in_imms"]]
    )
    asserts = [f'assert(({c["c_expr"]}) && "{c["message"]}");'
               for c in ctx["constraints"]]
    ret_type = ctx["out"]["type"] if ctx["out"] else "void"
    doc = f"/**\n * {_doc(ctx)}\n */"

    if ctx["in_imms"]:
        # Immediates must be assembled as literals → a statement-expression macro
        # so the value reaches the "i" constraint as a constant.
        params = ", ".join(o["name"] for o in ctx["in_regs"] + ctx["in_imms"])
        oc = out_clause.format(rd="_rd")
        lines = [f"#define {ctx['fn_name']}({params}) __extension__({{ \\"]
        for a in asserts:
            lines.append(f"    {a} \\")
        if ctx["out"]:
            lines.append(f"    {ctx['out']['type']} _rd; \\")
        lines.append("    __asm__ volatile( \\")
        lines.append(f'        "{asm}" \\')
        lines.append(f"        : {oc} \\")
        lines.append(f"        : {in_clause} \\")
        lines.append("    ); \\")
        lines.append(f"    {'_rd' if ctx['out'] else '(void)0'}; \\")
        lines.append("})")
        return f"{doc}\n" + "\n".join(lines)

    params = ", ".join(f'{o["type"]} {o["name"]}' for o in ctx["in_regs"]) or "void"
    oc = out_clause.format(rd=ctx["out"]["name"]) if ctx["out"] else ""
    body = [f"static inline {ret_type} {ctx['fn_name']}({params}) {{"]
    for a in asserts:
        body.append(f"    {a}")
    if ctx["out"]:
        body.append(f"    {ctx['out']['type']} {ctx['out']['name']};")
    body.append("    __asm__ volatile(")
    body.append(f'        "{asm}"')
    body.append(f"        : {oc}")
    body.append(f"        : {in_clause}")
    body.append("    );")
    if ctx["out"]:
        body.append(f"    return {ctx['out']['name']};")
    body.append("}")
    return f"{doc}\n" + "\n".join(body)


def _render_rust(ctx) -> str:
    placeholders = ", ".join(f"{{{i}}}" for i in range(len(_operand_order(ctx))))
    asm = f"{ctx['mnemonic']} {placeholders}".rstrip()
    operands = []
    if ctx["out"]:
        operands.append(f"out(reg) {ctx['out']['name']}")
    operands += [f"in(reg) {o['name']}" for o in ctx["in_regs"]]
    operands += [f"const {o['name']}" for o in ctx["in_imms"]]
    asm_args = ", ".join([f'"{asm}"'] + operands)

    asserts = [f'assert!(({c["c_expr"]}), "{c["message"]}");'
               for c in ctx["constraints"]]
    ret = f" -> {ctx['out']['type']}" if ctx["out"] else ""
    # Immediates become const generic parameters so they assemble as literals.
    generics = ""
    attrs = ["#[inline(always)]"]
    if ctx["in_imms"]:
        generics = "<" + ", ".join(
            f"const {o['name']}: {o['type']}" for o in ctx["in_imms"]
        ) + ">"
        attrs.append("#[allow(non_upper_case_globals)]")
    params = ", ".join(f'{o["name"]}: {o["type"]}' for o in ctx["in_regs"])

    lines = [f"/// {_doc(ctx)}"]
    lines += attrs
    lines.append(f"pub unsafe fn {ctx['fn_name']}{generics}({params}){ret} {{")
    for a in asserts:
        lines.append(f"    {a}")
    if ctx["out"]:
        lines.append(f"    let {ctx['out']['name']}: {ctx['out']['type']};")
    lines.append(f"    asm!({asm_args});")
    if ctx["out"]:
        lines.append(f"    {ctx['out']['name']}")
    lines.append("}")
    return "\n".join(lines)


def _calculate_mask(start: int, end: int) -> str:
    return hex(((1 << (end - start + 1)) - 1) << start)


def generate_software(registry: Registry, output_dir: str, lang: SoftwareLang,
                      clang_format: bool = False):
    env = make_jinja_env()
    env.filters["constraint_to_c"] = constraint_to_c
    env.filters["calculate_mask"] = _calculate_mask
    out_path = prepare_output_dir(output_dir)
    extension = "h" if lang == SoftwareLang.C else "rs"
    render = _render_c if lang == SoftwareLang.C else _render_rust

    template_intrin = env.get_template(f"sw/intrinsics.{extension}.j2")
    template_struct = env.get_template(f"sw/structs.{extension}.j2")
    template_csr = env.get_template(f"sw/csrs.{extension}.j2")

    for isa_reg in registry.isas.values():
        ISA_upper = isa_ident(isa_reg.name)
        instr_defs = dict(_build_instr_defs(isa_reg, ISA_upper))

        rendered = []
        for instr_name, instr in isa_reg.instructions.items():
            info = instr_defs.get(instr_name.upper())
            if info is None:
                continue  # omitted from the backend (unknown schema, etc.)
            schema = isa_reg.schemas.get(instr.spec.schema_name)
            ctx = _intrinsic_context(instr, info, schema, lang)
            if ctx is not None:
                rendered.append(render(ctx))

        output_intrin = template_intrin.render(intrinsics=rendered, isa_name=isa_reg.name)
        output_struct = template_struct.render(operands=isa_reg.operands, isa_name=isa_reg.name)
        output_csr = template_csr.render(csrs={}, isa_name=isa_reg.name, hex=hex)
        # Rust (.rs) isn't C/C++, so clang_format is a no-op there by extension.
        cf = clang_format and lang == SoftwareLang.C
        write_generated(out_path / f"{isa_reg.name}_intrinsics.{extension}", output_intrin, clang_format=cf)
        write_generated(out_path / f"{isa_reg.name}_structs.{extension}", output_struct, clang_format=cf)
        write_generated(out_path / f"{isa_reg.name}_csrs.{extension}", output_csr, clang_format=cf)

    logger.info(f"Generated {lang.upper()} software artifacts in {output_dir}")
