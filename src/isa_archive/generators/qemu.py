import os
import logging
import pathlib
from typing import Optional
from ..compiler.loader import Registry
from ..compiler.behavior import BehaviorIR
from ..compiler.backends import QemuCBackend, QemuTCGBackend
from ..compiler.utils import build_reg_maps, instruction_pattern, constraint_to_c, compute_insn_width
from ..models.enums import FieldRole
from ..models.scalar_types import of_register
from .base import make_jinja_env, prepare_output_dir, write_generated, CLANG_FORMAT_QEMU

logger = logging.getLogger("isa_archive.generators")


def _float_scalar_types(isa_reg) -> list[dict]:
    """Per-width float helper descriptors {w, c_type}, deduped by width and sorted.

    Drives the u2f/f2u bit-reinterpretation helpers. ``c_type`` comes from the
    single scalar-type source of truth; a width with no native host C type
    (f16/bf16) carries ``c_type=None`` and the template skips it (softfloat TODO)."""
    by_width: dict[int, object] = {}
    for r in isa_reg.registers:
        if r.is_float:
            by_width.setdefault(r.width, of_register(r).c_type)
    return [{"w": w, "c_type": c} for w, c in sorted(by_width.items())]


def _guest_word(isa_reg) -> dict:
    """The QEMU guest-word model for an ISA's data width.

    QEMU's TCG only has 32- and 64-bit guest words (TARGET_LONG_BITS), so:

    * a narrow architectural xlen (8/16) is emulated over a 32-bit guest word
      the way QEMU's AVR target works — PC and addresses are masked to xlen,
      and xlen-wide register files live in guest-word-sized slots with masked
      writes;
    * xlen=128 runs over a 64-bit guest word: registers and arithmetic are
      native 128-bit (host ``__uint128_t``, helper-only — no TCG globals), but
      the PC and the address space are 64-bit (TCG has no 128-bit guest
      addresses; values written to the PC truncate to the address space).

      tcg_bits   : 32 or 64 — TARGET_LONG_BITS / TCG global width
      tcg_type   : "i32"/"i64"
      c_int_type : C type of helper value args ("uint32_t"/"uint64_t")
      xlen_mask  : hex mask when xlen < tcg_bits (None otherwise)
      page_bits  : TARGET_PAGE_BITS (12, or 8 for narrow address spaces)
      addr_bits  : TARGET_{PHYS,VIRT}_ADDR_SPACE_BITS (xlen capped at 64)
    """
    xlen = isa_reg.xlen
    tcg_bits = 32 if xlen <= 32 else 64
    return {
        "tcg_bits": tcg_bits,
        "tcg_type": f"i{tcg_bits}",
        "c_int_type": f"uint{tcg_bits}_t",
        "xlen_mask": f"0x{(1 << xlen) - 1:X}u" if xlen < tcg_bits else None,
        "page_bits": 12 if xlen >= 32 else 8,
        "addr_bits": min(xlen, 64),
    }


def _regfile_storage(isa_reg) -> dict[str, dict]:
    """Per-register-file QEMU storage/access model.

    Each entry describes how a register file is held in CPUArchState and how
    generated code may touch it:

      storage_bits : scalar C storage width (8/16/32/64); None for >64-bit
                     files, which are stored as byte arrays.
      c_type       : the C declarator ("uint32_t"), or None for byte arrays.
      bytes        : per-element byte count (byte-array files only).
      tcg          : "i32"/"i64" when a TCG global array is emitted. Only files
                     whose width equals xlen get globals; their storage is the
                     guest word (a global of a different width than its state
                     slot corrupts memory), with masked writes when xlen is
                     narrower than the guest word. All other files are
                     helper-only: helpers receive the register index and access
                     env-> state directly.
      mask         : hex write-mask when the architectural width is narrower
                     than the storage type, else None.
    """
    xlen = isa_reg.xlen
    word = _guest_word(isa_reg)
    storage: dict[str, dict] = {}
    for r in isa_reg.registers:
        w = r.width
        if w == 128:
            # Native 128-bit storage on the host (__uint128_t exists on every
            # 64-bit host compiler QEMU supports). Helper-only: 128-bit values
            # never cross the TCG helper boundary — helpers get the index.
            storage[r.name] = {"width": w, "storage_bits": 128,
                               "c_type": "__uint128_t", "bytes": None,
                               "tcg": None, "mask": None}
            continue
        if w > 64:
            storage[r.name] = {"width": w, "storage_bits": None, "c_type": None,
                               "bytes": (w + 7) // 8, "tcg": None, "mask": None}
            continue
        if w == xlen:
            # TCG-global file: storage must be the guest-word size.
            storage_bits = word["tcg_bits"]
            tcg = word["tcg_type"]
        else:
            storage_bits = next(b for b in (8, 16, 32, 64) if w <= b)
            tcg = None
        mask = f"0x{(1 << w) - 1:X}u" if w < storage_bits else None
        storage[r.name] = {"width": w, "storage_bits": storage_bits,
                           "c_type": f"uint{storage_bits}_t", "bytes": None,
                           "tcg": tcg, "mask": mask}
    return storage


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
                    operands=isa_reg.operands, csrs={})

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
        if v in ir.write_vars or (v in reg_map and reg_map[v] in helper_only):
            # destination register index, or a helper-only file's source index:
            # the helper accesses env-> state itself.
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
            f"PC/address space — TCG has no 128-bit guest addresses.)"
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


def _write_isa_files(env, isa_reg, out_path: pathlib.Path, clang_format: bool = False):
    """Generate the 8 ISA-semantics files into out_path (flat)."""
    _validate_for_qemu(isa_reg)
    xlen = isa_reg.xlen
    word = _guest_word(isa_reg)
    _w = compute_insn_width(isa_reg, isa_reg.name, max_bits=64,  # QEMU fetch ≤ 64-bit word
                            limit_hint=("The QEMU backend fetches one instruction word per translation step and decodetree patterns cap at 64 bits; wider encodings are currently LLVM-only (see the generality plan, G3)."))
    float_types = _float_scalar_types(isa_reg)
    has_mem = any("mem" in i.spec.behavior for i in isa_reg.instructions.values())
    has_sext = any("sext" in i.spec.behavior for i in isa_reg.instructions.values())
    ctx = dict(instructions=isa_reg.instructions, isa_reg=isa_reg, isa_name=isa_reg.name,
               xlen=xlen, tcg_type=word["tcg_type"], c_int_type=word["c_int_type"],
               tcg_bits=word["tcg_bits"], xlen_mask=word["xlen_mask"],
               page_bits=word["page_bits"], addr_bits=word["addr_bits"],
               insn_bits=_w["insn_bits"], insn_bytes=_w["insn_bytes"],
               reg_storage=_regfile_storage(isa_reg),
               float_scalar_types=float_types,
               has_mem=has_mem, has_float=bool(float_types), has_sext=has_sext)

    def render(template_name: str, out_name: str):
        content = env.get_template(template_name).render(**ctx)
        write_generated(out_path / out_name, content, clang_format=clang_format)

    render("qemu/qemu_decode.decode.j2",  f"{isa_reg.name}.decode")
    render("qemu/qemu_helpers.c.j2",      f"{isa_reg.name}_helpers.c")
    render("qemu/qemu_helper.h.j2",       f"{isa_reg.name}_helper.h")
    render("qemu/qemu_trans.c.inc.j2",    f"{isa_reg.name}_trans.c.inc")
    render("qemu/qemu_arch.h.j2",         f"{isa_reg.name}_arch.h")
    render("qemu/qemu_translate.c.j2",    f"{isa_reg.name}_translate.c")
    render("qemu/qemu_cpu.c.j2",          f"{isa_reg.name}_cpu.c")
    if isa_reg.operands:
        render("qemu/qemu_operands.h.j2", f"{isa_reg.name}_operands.h")


def generate_qemu_isa(registry: Registry, output_dir: str, clang_format: bool = False):
    """Generate ISA semantics files only (flat in output_dir). Old `qemu` target behavior."""
    env = _make_qemu_env()
    out_path = prepare_output_dir(output_dir)
    write_generated(out_path / ".clang-format", CLANG_FORMAT_QEMU)
    for isa_reg in registry.isas.values():
        _write_isa_files(env, isa_reg, out_path, clang_format=clang_format)
    logger.info(f"Generated QEMU ISA artifacts in {output_dir}")


def generate_qemu(registry: Registry, output_dir: str, clang_format: bool = False,
                  components: Optional[set] = None):
    """Generate complete QEMU target: ISA semantics + QOM boilerplate + machine + build system.

    Output mirrors the QEMU source tree so files can be dropped in directly:
      target/{isa}/   → $QEMU/target/{isa}/
      hw/{isa}/       → $QEMU/hw/{isa}/
      configs/        → $QEMU/configs/
      patch_qemu.sh   → run once to apply minor QEMU source patches
      INTEGRATE.md    → integration instructions

    ``components`` (None = everything) selects a subset for the sub-targets:
      "isa"     → target/{isa}/ (semantics + QOM)
      "machine" → hw/{isa}/ + configs/
      "build"   → patch_qemu.sh + INTEGRATE.md
    """
    want = (lambda g: True) if components is None else (lambda g: g in components)
    env = _make_qemu_env()

    for isa_reg in registry.isas.values():
        _validate_for_qemu(isa_reg)
        xlen = isa_reg.xlen
        word = _guest_word(isa_reg)
        machine = isa_reg.machine

        # A narrow xlen means a narrow physical address space: the machine
        # layout must fit in it (the defaults target 32-bit systems).
        if machine is not None and xlen < 32:
            limit = 1 << xlen
            top = machine.ram_base + machine.ram_size
            if machine.ram_base >= limit or top > limit:
                raise ValueError(
                    f"{isa_reg.name}: machine layout (ram_base=0x{machine.ram_base:X}, "
                    f"ram_size=0x{machine.ram_size:X}) does not fit the {xlen}-bit "
                    f"address space (max 0x{limit:X}). Set spec.machine.ram_base/"
                    f"ram_size for a narrow-xlen target."
                )
            if machine.effective_reset_vector() >= limit:
                raise ValueError(
                    f"{isa_reg.name}: reset_vector 0x{machine.effective_reset_vector():X} "
                    f"is outside the {xlen}-bit address space."
                )

        first_reg = isa_reg.registers[0] if isa_reg.registers else None
        # Initial-SP setup in the virt board only when the ISA declares an sp
        # alias — never invent one positionally (accelerator ISAs have no stack).
        sp_reg_idx = first_reg.aliases.get("sp") if first_reg else None
        sp_reg_file = first_reg.name if first_reg else None
        _w = compute_insn_width(isa_reg, isa_reg.name, max_bits=64,
                                limit_hint=("The QEMU backend fetches one instruction word per translation step and decodetree patterns cap at 64 bits; wider encodings are currently LLVM-only (see the generality plan, G3)."))
        ctx = dict(instructions=isa_reg.instructions, isa_reg=isa_reg, isa_name=isa_reg.name,
                   xlen=xlen, tcg_type=word["tcg_type"], c_int_type=word["c_int_type"],
                   tcg_bits=word["tcg_bits"], xlen_mask=word["xlen_mask"],
                   page_bits=word["page_bits"], addr_bits=word["addr_bits"],
                   machine=machine,
                   sp_reg_idx=sp_reg_idx, sp_reg_file=sp_reg_file,
                   insn_bits=_w["insn_bits"], insn_bytes=_w["insn_bytes"],
                   reg_storage=_regfile_storage(isa_reg),
                   byte_order=getattr(isa_reg.manifest.spec, "byte_order", "little"),
                   float_scalar_types=_float_scalar_types(isa_reg))

        root = pathlib.Path(output_dir)
        isa = isa_reg.name

        # Ship a clang-format config so adopted code formats to QEMU house style.
        write_generated(root / ".clang-format", CLANG_FORMAT_QEMU)

        def render_to(template_name: str, dest: pathlib.Path):
            content = env.get_template(template_name).render(**ctx)
            write_generated(dest, content, clang_format=clang_format)

        if want("isa"):
            # target/{isa}/ — ISA semantics + QOM boilerplate
            target_dir = root / "target" / isa
            target_dir.mkdir(parents=True, exist_ok=True)
            _write_isa_files(env, isa_reg, target_dir, clang_format=clang_format)
            render_to("qemu/qemu_cpu_h.j2",            target_dir / "cpu.h")
            render_to("qemu/qemu_cpu_qom_h.j2",        target_dir / "cpu-qom.h")
            render_to("qemu/qemu_cpu_param_h.j2",       target_dir / "cpu-param.h")
            render_to("qemu/qemu_helper_wrapper_h.j2",  target_dir / "helper.h")
            render_to("qemu/qemu_target_meson.j2",      target_dir / "meson.build")
            render_to("qemu/qemu_target_kconfig.j2",    target_dir / "Kconfig")

        if want("machine"):
            # hw/{isa}/ — machine definition
            hw_dir = root / "hw" / isa
            hw_dir.mkdir(parents=True, exist_ok=True)
            render_to("qemu/qemu_hw_virt_c.j2",  hw_dir / "virt.c")
            render_to("qemu/qemu_hw_meson.j2",   hw_dir / "meson.build")
            render_to("qemu/qemu_hw_kconfig.j2", hw_dir / "Kconfig")
            # configs/ — build system configs
            render_to("qemu/qemu_configs_target_mak.j2",
                      root / "configs" / "targets" / f"{isa}-softmmu.mak")
            render_to("qemu/qemu_configs_default_mak.j2",
                      root / "configs" / "devices" / f"{isa}-softmmu" / "default.mak")

        if want("build"):
            # Integration helpers at root
            render_to("qemu/qemu_integrate_md.j2", root / "INTEGRATE.md")
            patch_sh = root / "patch_qemu.sh"
            render_to("qemu/qemu_patch_sh.j2", patch_sh)
            patch_sh.chmod(patch_sh.stat().st_mode | 0o111)  # make executable

    logger.info(f"Generated QEMU target ({output_dir})")
