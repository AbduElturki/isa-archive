"""LLVM backend generator orchestration.

`generate_llvm` is a thin orchestrator over named steps:
  validate → resolve register classes → build instruction defs → collect roles →
  resolve key opcodes/flags → compute encoding (fixups/NOP/relocs) → assemble ctx
  → emit the coverage report → render the target (per `components`).
Each step is a small function below that returns the slice of template context it
owns; the orchestrator merges them.
"""
import logging
import pathlib
from typing import Optional

from ...compiler.loader import Registry
from ...compiler.utils import compute_insn_width, isa_ident
from ...models.abi import ABI
from ...models.compiler import CompilerProfile
from ...models.scalar_types import resolve as resolve_scalar_type
from ..base import (make_jinja_env, prepare_output_dir, write_generated,
                    make_renderer, CLANG_FORMAT_LLVM)
from .instr_defs import _build_instr_defs
from .encoding import (_get_schema_combined_imm, _collect_imm_operands,
                       _compute_fixup_info, _compute_single_field_fixup,
                       _encode_instr_as_nop)
from .opcodes import (_find_sp_adjust_opcode, _find_lui_opcode,
                      _find_jal_opcode, _find_jalr_opcode)
from .regclasses import _class_value_types, _resolve_reg_name
from .coverage import (_collect_compiler_roles, _build_coverage_report,
                       _setcc_branch_entries, _infer_const_strategy, _required_roles)

logger = logging.getLogger("isa_archive.generators")

_ELF_BY_TRIPLE: dict[str, int] = {"riscv32": 243, "riscv64": 243, "arm": 40, "mips": 8}


def _validate_isa(isa_reg, ISA: str, xlen: int) -> None:
    """Reject data widths and `type:` references the LLVM backend can't express."""
    if xlen not in (8, 16, 32, 64, 128):
        raise ValueError(
            f"{ISA}: data width xlen={xlen} is not a legal scalar type "
            f"(must be one of 8, 16, 32, 64, 128)."
        )
    for reg in isa_reg.registers:
        t = getattr(reg, "type", None)
        if t and resolve_scalar_type(t) is None and t not in isa_reg.operands:
            raise ValueError(
                f"{ISA}: register file '{reg.name}' has type '{t}', which is "
                f"neither a scalar (iN/fN) nor a defined Operand struct."
            )


def _resolve_reg_classes(isa_reg, xlen: int, ISA: str) -> dict:
    """Partition register files into codegen classes, resolve the ABI and the
    sp/ra/zero/fp registers. Returns the register/ABI slice of template context,
    plus a few internals (``skip_regfiles``, ``first_reg``) the caller needs.
    """
    # Only files whose element type can be a first-class LLVM value type on this
    # target become register classes — integer files of the data width, float
    # files, or files with an explicit legacy `value_types` override. Everything
    # else (1-bit predicates, >xlen accumulators/vectors, …) stays architectural
    # state: making e.g. MVT::i1 or MVT::i128 legal via addRegisterClass breaks
    # codegen globally. Instructions touching excluded files are omitted (warned).
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

    # The primary integer file (first codegen-eligible non-float file) provides
    # the GPR class, the ABI alias table, and sp/zero/ra resolution.
    first_reg = next((r for r in codegen_regs if not r.is_float), None)
    if first_reg is None and isa_reg.registers:
        raise ValueError(
            f"{ISA}: no register file is usable as an LLVM integer register "
            f"class (need an integer file of width xlen={xlen})."
        )

    abi_spec = isa_reg.manifest.spec.abi or ABI()
    abi = abi_spec.resolve(first_reg.aliases if first_reg else {})

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

    int_codegen_regs = [r for r in codegen_regs if not r.is_float]

    def _alias_to_reg(alias: str) -> str:
        # Search every register file so float ABI aliases (e.g. fa0) resolve too.
        for reg in isa_reg.registers:
            if alias in reg.aliases:
                return f"{reg.prefix}{reg.aliases[alias]}"
        return alias

    return {
        # internals (not template context)
        "skip_regfiles": skip_regfiles,
        "first_reg": first_reg,
        # template context
        "registers": codegen_regs,
        "abi": abi,
        "abi_stack_alignment": abi.stack_alignment,
        "abi_arg_regs": [_alias_to_reg(a) for a in abi.arg_registers],
        "abi_ret_regs": [_alias_to_reg(r) for r in abi.ret_registers],
        "abi_saved_regs": [_alias_to_reg(s) for s in abi.callee_saved],
        "abi_fp_arg_regs": [_alias_to_reg(a) for a in abi.fp_arg_registers],
        "abi_fp_ret_regs": [_alias_to_reg(r) for r in abi.fp_ret_registers],
        "first_reg_prefix": first_reg.prefix if first_reg else "r",
        "first_reg_class": first_reg.name.upper() if first_reg else "GPR",
        "sp_reg": _resolve_reg_name(int_codegen_regs, "sp"),
        "frame_pointer_reg": _resolve_reg_name(int_codegen_regs, abi.frame_pointer),
        "zero_reg": _resolve_reg_name(int_codegen_regs, "zero"),
        "ra_reg": _resolve_reg_name(int_codegen_regs, "ra"),
        "fp_reg_class": next((rc["class_name"] for rc in reg_classes if rc["is_float"]), None),
        "reg_classes": reg_classes,
        "reg_value_types": {rc["name"]: rc["value_types"] for rc in reg_classes},
        "int_value_types": sorted({
            vt for rc in reg_classes if not rc["is_float"] for vt in rc["value_types"]}),
        "float_value_types": sorted({
            vt for rc in reg_classes if rc["is_float"] for vt in rc["value_types"]}),
    }


def _resolve_opcodes(instr_defs: list, compiler_roles: dict, xlen: int,
                     zero_reg: Optional[str]) -> dict:
    """Bind the key opcodes (from the role map, falling back to heuristics) and the
    control-flow capability flags. Returns the opcode/flags slice of context."""
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

    slt_opcode = compiler_roles.get("cmp.lt")
    sltu_opcode = compiler_roles.get("cmp.ltu")
    bne_opcode = compiler_roles.get("branch.ne")
    has_ordering_branches = any(
        compiler_roles.get(f"branch.{c}") for c in ("lt", "ge", "ltu", "geu"))
    # cmp-and-branch-on-zero: no direct ordering branches, but set-less-than into a
    # register and branch on (non)zero is available.
    cmp_branch_path = (not has_ordering_branches) and bool(
        slt_opcode and bne_opcode and zero_reg)
    has_select = bool(bne_opcode and zero_reg)
    # Materializing a comparison into a 0/1 register normally needs a set-less-than
    # instruction; an ISA that only branches on comparisons can still do it with a
    # branch diamond — but only if it can make a 1 (add-immediate) and a 0 (zero).
    setcc_branch_entries = (
        _setcc_branch_entries(compiler_roles) if not (slt_opcode or sltu_opcode) else [])
    setcc_via_branch = bool(setcc_branch_entries and zero_reg and addi_opcode)
    if not setcc_via_branch:
        setcc_branch_entries = []

    fp_store_opcode = next(
        (n for n, i in instr_defs if i["dag_category"] == "store" and i.get("dag_is_float")), None)
    fp_load_opcode = next(
        (n for n, i in instr_defs if i["dag_category"] == "load" and i.get("dag_is_float")), None)

    return {
        "compiler_roles": compiler_roles,
        "addi_opcode": addi_opcode,
        "lui_opcode": lui_opcode,
        "jal_opcode": jal_opcode,
        "jalr_opcode": jalr_opcode,
        "const_strategy": const_strategy,
        # The global-address path keeps lui/addi (%hi/%lo) regardless of strategy.
        "const_hi_opcode": compiler_roles.get("const.hi") or lui_opcode,
        "const_lo_opcode": compiler_roles.get("const.lo") or addi_opcode,
        "const_load_opcode": compiler_roles.get("const.load"),
        "store_opcode": compiler_roles.get(f"mem.store{xlen}"),
        "load_opcode": compiler_roles.get(f"mem.load{xlen}"),
        "fp_store_opcode": fp_store_opcode,
        "fp_load_opcode": fp_load_opcode,
        "slt_opcode": slt_opcode,
        "sltu_opcode": sltu_opcode,
        "sltiu_opcode": compiler_roles.get("cmp.ltui"),
        "xor_opcode": compiler_roles.get("alu_rr.xor"),
        "beq_opcode": compiler_roles.get("branch.eq"),
        "bne_opcode": bne_opcode,
        "has_ordering_branches": has_ordering_branches,
        "cmp_branch_path": cmp_branch_path,
        "has_select": has_select,
        "setcc_via_branch": setcc_via_branch,
        "setcc_branch_entries": setcc_branch_entries,
        "needs_custom_inserter": has_select or setcc_via_branch,
        "has_global_addr": bool(lui_opcode and addi_opcode),
    }


def _compute_encoding(isa_reg, instr_defs: list, insn_bits: int, schema_len: int,
                      lui_opcode: Optional[str], addi_opcode: Optional[str]) -> dict:
    """Compute the fixup/NOP/relocation/immediate-width slice of context."""
    spec = isa_reg.manifest.spec
    byte_order: str = getattr(spec, "byte_order", "little")
    elf_machine: int = (
        spec.elf_machine if spec.elf_machine is not None
        else _ELF_BY_TRIPLE.get(spec.triple_arch or "", 0))

    schema_combined_imm = {name: _get_schema_combined_imm(s)
                           for name, s in isa_reg.schemas.items()}

    jal_fixup_info = branch_fixup_info = None
    for cimm in schema_combined_imm.values():
        if cimm is None:
            continue
        if cimm["operand_name"] == "jaltarget" and jal_fixup_info is None:
            jal_fixup_info = _compute_fixup_info(cimm["hw_assignments"], insn_bits)
        elif cimm["operand_name"] == "brtarget" and branch_fixup_info is None:
            branch_fixup_info = _compute_fixup_info(cimm["hw_assignments"], insn_bits)
    lui_fixup_info = _compute_single_field_fixup(lui_opcode, instr_defs, isa_reg.schemas, insn_bits)
    addi_fixup_info = _compute_single_field_fixup(addi_opcode, instr_defs, isa_reg.schemas, insn_bits)

    # NOP C-string: from YAML field, or auto-encoded from the ADDI schema.
    nop_c_str: Optional[str] = None
    if spec.nop_encoding:
        try:
            nop_val = int(spec.nop_encoding, 16)
            raw = nop_val.to_bytes(schema_len // 8, 'big' if byte_order == 'big' else 'little')
            nop_c_str = '"' + ''.join(f'\\x{b:02x}' for b in raw) + '"'
        except Exception:
            pass
    if nop_c_str is None and addi_opcode:
        nop_c_str = _encode_instr_as_nop(addi_opcode, instr_defs, isa_reg.schemas,
                                         isa_reg, schema_len, byte_order=byte_order)

    # ELF relocation names: explicit override → RISC-V defaults → empty (→ R_NONE)
    if spec.elf_relocations:
        elf_reloc_map = dict(spec.elf_relocations)
    elif elf_machine == 243:  # EM_RISCV
        elf_reloc_map = {"jal": "R_RISCV_JAL", "branch": "R_RISCV_BRANCH",
                         "hi20": "R_RISCV_HI20", "lo12_i": "R_RISCV_LO12_I"}
    else:
        elf_reloc_map = {}

    addi_width = addi_fixup_info["width"] if addi_fixup_info else 12
    lui_width = lui_fixup_info["width"] if lui_fixup_info else 20
    return {
        "byte_order": byte_order,
        "elf_machine": elf_machine,
        "triple_arch": spec.triple_arch or isa_reg.name,
        "schema_combined_imm": schema_combined_imm,
        "imm_operands": _collect_imm_operands(isa_reg),
        "jal_fixup_info": jal_fixup_info,
        "branch_fixup_info": branch_fixup_info,
        "lui_fixup_info": lui_fixup_info,
        "addi_fixup_info": addi_fixup_info,
        "num_fixup_kinds": sum(1 for f in (jal_fixup_info, branch_fixup_info,
                                           lui_fixup_info, addi_fixup_info) if f),
        "nop_c_str": nop_c_str,
        "elf_reloc_map": elf_reloc_map,
        "addi_width": addi_width,
        "lui_width": lui_width,
        "lui_compensator": 1 << (addi_width - 1),
        "lui_mask": (1 << lui_width) - 1,
        "lo_mask": (1 << addi_width) - 1,
    }


def _reserved_regs(first_reg, abi) -> list[dict]:
    """Registers that must never be allocated (zero/sp/gp/tp/frame-pointer), each
    tagged with its ABI alias so the generated code self-documents *why*."""
    reserved: list[dict] = []
    seen: set[str] = set()
    if first_reg:
        for alias in ["zero", "sp", "gp", "tp"]:
            if alias in first_reg.aliases:
                reg = f"{first_reg.prefix}{first_reg.aliases[alias]}"
                reserved.append({"reg": reg, "alias": alias})
                seen.add(reg)
        if abi.frame_pointer and abi.frame_pointer in first_reg.aliases:
            fp_r = f"{first_reg.prefix}{first_reg.aliases[abi.frame_pointer]}"
            if fp_r not in seen:
                reserved.append({"reg": fp_r, "alias": abi.frame_pointer})
    return reserved


def _render_target(env, ctx: dict, output_dir: str, ISA: str,
                   want, clang_format: bool) -> None:
    """Render the LLVM target tree for one ISA, honoring the `components` filter."""
    root = pathlib.Path(output_dir)
    target = root / "llvm" / "lib" / "Target" / ISA
    mcdesc = target / "MCTargetDesc"
    targetinfo = target / "TargetInfo"
    render_to = make_renderer(env, ctx, clang_format=clang_format)

    if want("tablegen"):
        render_to("llvm/llvm_root.td.j2",           target / f"{ISA}.td")
        render_to("llvm/llvm_register_info.td.j2",  target / f"{ISA}RegisterInfo.td")
        render_to("llvm/llvm_instr_formats.td.j2",  target / f"{ISA}InstrFormats.td")
        render_to("llvm/llvm_instr_info.td.j2",     target / f"{ISA}InstrInfo.td")
        render_to("llvm/llvm_calling_conv.td.j2",   target / f"{ISA}CallingConv.td")
        render_to("llvm/llvm_schedule.td.j2",       target / f"{ISA}Schedule.td")

    if want("backend"):
        render_to("llvm/llvm_isa_h.j2",               target / f"{ISA}.h")
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

    if want("mc"):
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
        render_to("llvm/llvm_target_info.h.j2",          targetinfo / f"{ISA}TargetInfo.h")
        render_to("llvm/llvm_target_info.cpp.j2",        targetinfo / f"{ISA}TargetInfo.cpp")
        render_to("llvm/llvm_targetinfo_cmakelists.j2",  targetinfo / "CMakeLists.txt")

    if want("backend"):
        patch_sh = root / "patch_llvm.sh"
        render_to("llvm/llvm_patch_sh.j2",    patch_sh)
        patch_sh.chmod(patch_sh.stat().st_mode | 0o111)
        render_to("llvm/llvm_integrate_md.j2", root / "INTEGRATE.md")


def _emit_coverage(isa_reg, ISA: str, ctx: dict, role_conflicts: list,
                   strict: bool, output_dir: str) -> None:
    """Write COMPILER_COVERAGE.md + .clang-format, warn/raise on missing roles."""
    spec = isa_reg.manifest.spec
    xlen = isa_reg.xlen
    profile_spec = spec.compiler or CompilerProfile()
    profile = profile_spec.profile
    if profile == "c-baremetal":
        required = _required_roles(xlen)
    elif profile == "kernel-only":
        required = set()
    else:  # custom
        required = set(profile_spec.requires)

    # Non-role prerequisites: lowering C needs the CPU register conventions
    # declared explicitly (sp, ra, zero) — never invented positionally.
    missing_prereqs: list[str] = []
    if profile == "c-baremetal":
        for prereq, val in (("alias:sp", ctx["sp_reg"]), ("alias:ra", ctx["ra_reg"]),
                            ("alias:zero", ctx["zero_reg"])):
            if val is None:
                missing_prereqs.append(prereq)

    custom_instrs = [(name, info.get("dag_notes") or [])
                     for name, info in ctx["instr_defs"]
                     if info["dag_category"] == "custom"]

    report_md, missing_required = _build_coverage_report(
        ISA, ctx["compiler_roles"], role_conflicts, ctx["const_strategy"],
        has_ordering_branches=ctx["has_ordering_branches"], xlen=xlen, profile=profile,
        required=required, missing_prereqs=missing_prereqs, custom_instrs=custom_instrs)

    target = pathlib.Path(output_dir) / "llvm" / "lib" / "Target" / ISA
    target.mkdir(parents=True, exist_ok=True)
    write_generated(target / "COMPILER_COVERAGE.md", report_md)
    write_generated(target / ".clang-format", CLANG_FORMAT_LLVM)
    if missing_required:
        logger.warning(
            "%s: compiler backend INCOMPLETE for profile '%s' — missing: %s "
            "(see COMPILER_COVERAGE.md)", ISA, profile, ", ".join(missing_required))
        if strict:
            raise ValueError(
                f"{ISA}: profile '{profile}' is missing {missing_required}. "
                f"Tag instructions with compiler.roles, declare the missing "
                f"register aliases, or set spec.compiler.profile to match "
                f"the target (kernel-only for stack-less compute ISAs).")
    else:
        logger.info("%s: compiler backend COMPILER-COMPLETE for profile '%s' "
                    "(strategy=%s)", ISA, profile, ctx["const_strategy"])


def generate_llvm(registry: Registry, output_dir: str, strict: bool = False,
                  clang_format: bool = False, components: Optional[set] = None):
    """Generate a complete LLVM backend for every ISA in the registry.

    ``components`` (None = everything) selects a subset for the sub-targets:
      "tablegen" → the *.td files
      "backend"  → C++ backend + CMake + patch_llvm.sh + INTEGRATE.md
      "mc"       → MCTargetDesc/ + TargetInfo/

    Output mirrors the LLVM source tree (llvm/lib/Target/{ISA}/) plus patch_llvm.sh
    and INTEGRATE.md. With ``strict``, raises if an ISA is missing a required role.
    """
    want = (lambda g: True) if components is None else (lambda g: g in components)
    env = make_jinja_env()

    for isa_reg in registry.isas.values():
        xlen = isa_reg.xlen
        isa_name = isa_reg.name
        ISA = isa_ident(isa_name)
        _validate_isa(isa_reg, ISA, xlen)

        rc = _resolve_reg_classes(isa_reg, xlen, ISA)

        # Instruction-encoding width (distinct from the data width `xlen`); uniform
        # per ISA, up to a 512-bit hard cap (shared with QEMU generation).
        _w = compute_insn_width(isa_reg, ISA, max_bits=512)
        insn_bits, insn_bytes, insn_uint = _w["insn_bits"], _w["insn_bytes"], _w["insn_uint"]

        instr_defs = _build_instr_defs(isa_reg, ISA, skip_regfiles=rc["skip_regfiles"])
        compiler_roles, role_conflicts = _collect_compiler_roles(instr_defs, xlen)
        opc = _resolve_opcodes(instr_defs, compiler_roles, xlen, rc["zero_reg"])
        enc = _compute_encoding(isa_reg, instr_defs, insn_bits, insn_bits,
                                opc["lui_opcode"], opc["addi_opcode"])

        ctx = dict(
            isa_name=isa_name, ISA=ISA, xlen=xlen,
            schemas=isa_reg.schemas, instructions=isa_reg.instructions,
            instr_defs=instr_defs,
            schema_len=insn_bits,
            tcg_type="i64" if xlen == 64 else "i32",
            reserved_regs=_reserved_regs(rc["first_reg"], rc["abi"]),
            insn_bits=insn_bits, insn_bytes=insn_bytes, insn_uint=insn_uint,
            **{k: v for k, v in rc.items() if k not in ("skip_regfiles", "first_reg")},
            **opc,
            **enc,
        )

        _emit_coverage(isa_reg, ISA, ctx, role_conflicts, strict, output_dir)
        _render_target(env, ctx, output_dir, ISA, want, clang_format)

    logger.info(f"Generated LLVM target ({output_dir})")
