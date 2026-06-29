"""QEMU generator orchestration: the flat ISA-semantics target and the full
source-tree target (with the sub-target `components` filter)."""
import logging
import pathlib
from typing import Optional

from ...compiler.loader import Registry
from ...compiler.utils import compute_insn_width, build_trap_info, build_csr_info
from ...compiler.backends.qemu_c import build_do_interrupt_body
from ..base import prepare_output_dir, write_generated, make_renderer, CLANG_FORMAT_QEMU
from .word import _guest_word, _regfile_storage, _float_scalar_types
from .semantics import _make_qemu_env, _validate_for_qemu, _build_wide_decode_meta

# Instruction words up to this many bits. <=64-bit ISAs decode via QEMU's
# decodetree; wider ones (up to this cap) use a hand-written byte-array decoder.
_QEMU_MAX_INSN_BITS = 512
_QEMU_WIDTH_HINT = ("The QEMU backend decodes instruction words up to "
                    f"{_QEMU_MAX_INSN_BITS} bits; wider encodings are not supported.")

logger = logging.getLogger("isa_archive.generators")


def _interrupt_ctx(isa_reg, pc_mask) -> dict:
    """Hardware-interrupt-delivery context for the CPU template, derived from the
    ISA's `trap:` block. For an ISA without one, `do_interrupt_body` is None and
    the CPU keeps its halt-on-exception fallback - byte-identical to before."""
    trap_info = build_trap_info(isa_reg)
    if not trap_info:
        return {"do_interrupt_body": None, "irq_enable_expr": None,
                "irq_cause": None, "exc_cause": None}
    csr_info = build_csr_info(isa_reg)
    sc = trap_info.get("status_csr")
    scf = csr_info.get(sc, {}).get("fields", {}) if sc else {}
    # Interrupts are taken only when the status CSR's mie bit is set (if declared).
    irq_enable_expr = (f"((env->{sc} >> {scf['mie'][0]}) & 1)"
                       if sc and "mie" in scf else "1")
    cc = trap_info["cause_csr"]
    ccf = csr_info.get(cc, {}).get("fields", {})
    ibit = (ccf["interrupt"][0] if "interrupt" in ccf
            else csr_info.get(cc, {}).get("width", 32) - 1)
    return {
        "do_interrupt_body": build_do_interrupt_body(trap_info, csr_info, pc_mask),
        "irq_enable_expr": irq_enable_expr,
        "irq_cause": hex(1 << ibit),                          # mcause.interrupt = 1
        "exc_cause": trap_info["causes"].get("illegal", 0),   # synchronous exception
    }


def _write_isa_files(env, isa_reg, out_path: pathlib.Path, clang_format: bool = False):
    """Generate the ISA-semantics files into out_path (flat)."""
    _validate_for_qemu(isa_reg)
    xlen = isa_reg.xlen
    word = _guest_word(isa_reg)
    _w = compute_insn_width(isa_reg, isa_reg.name, max_bits=_QEMU_MAX_INSN_BITS,
                            limit_hint=_QEMU_WIDTH_HINT)
    wide = _w["insn_bits"] > 64  # >64-bit words: byte-array fetch + hand-written decoder
    float_types = _float_scalar_types(isa_reg)
    has_mem = any("mem" in i.spec.behavior for i in isa_reg.instructions.values())
    has_sext = any("sext" in i.spec.behavior for i in isa_reg.instructions.values())
    # Headers for non-built-in float c_types used in the u2f/f2u helpers.
    from ...models.scalar_types import format_include
    c_includes = sorted({format_include(ft["c_include"])
                         for ft in float_types if ft["c_include"]})
    byte_order = getattr(isa_reg.manifest.spec, "byte_order", "little")
    intr = _interrupt_ctx(isa_reg, word["xlen_mask"])
    ctx = dict(instructions=isa_reg.instructions, isa_reg=isa_reg, isa_name=isa_reg.name,
               xlen=xlen, tcg_type=word["tcg_type"], c_int_type=word["c_int_type"],
               tcg_bits=word["tcg_bits"], xlen_mask=word["xlen_mask"],
               page_bits=word["page_bits"], addr_bits=word["addr_bits"],
               insn_bits=_w["insn_bits"], insn_bytes=_w["insn_bytes"],
               wide=wide, wide_instrs=(_build_wide_decode_meta(isa_reg) if wide else None),
               byte_order=byte_order,
               reg_storage=_regfile_storage(isa_reg),
               float_scalar_types=float_types, c_includes=c_includes,
               has_mem=has_mem, has_float=bool(float_types), has_sext=has_sext, **intr)

    render = make_renderer(env, ctx, clang_format=clang_format)
    name = isa_reg.name
    if wide:
        # decodetree caps at 64 bits - hand-write decode-<isa>.c.inc, the file
        # <isa>_translate.c already #includes. (decodetree would produce it for
        # narrow ISAs at build time.)
        render("qemu/qemu_wide_decode.c.inc.j2", out_path / f"decode-{name}.c.inc")
    else:
        render("qemu/qemu_decode.decode.j2", out_path / f"{name}.decode")
    render("qemu/qemu_helpers.c.j2",     out_path / f"{name}_helpers.c")
    render("qemu/qemu_helper.h.j2",      out_path / f"{name}_helper.h")
    render("qemu/qemu_trans.c.inc.j2",   out_path / f"{name}_trans.c.inc")
    render("qemu/qemu_arch.h.j2",        out_path / f"{name}_arch.h")
    render("qemu/qemu_translate.c.j2",   out_path / f"{name}_translate.c")
    render("qemu/qemu_cpu.c.j2",         out_path / f"{name}_cpu.c")
    if isa_reg.operands:
        render("qemu/qemu_operands.h.j2", out_path / f"{name}_operands.h")


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
        # alias - never invent one positionally (accelerator ISAs have no stack).
        sp_reg_idx = first_reg.aliases.get("sp") if first_reg else None
        sp_reg_file = first_reg.name if first_reg else None
        _w = compute_insn_width(isa_reg, isa_reg.name, max_bits=_QEMU_MAX_INSN_BITS,
                                limit_hint=_QEMU_WIDTH_HINT)
        ctx = dict(instructions=isa_reg.instructions, isa_reg=isa_reg, isa_name=isa_reg.name,
                   xlen=xlen, tcg_type=word["tcg_type"], c_int_type=word["c_int_type"],
                   tcg_bits=word["tcg_bits"], xlen_mask=word["xlen_mask"],
                   page_bits=word["page_bits"], addr_bits=word["addr_bits"],
                   machine=machine,
                   sp_reg_idx=sp_reg_idx, sp_reg_file=sp_reg_file,
                   insn_bits=_w["insn_bits"], insn_bytes=_w["insn_bytes"],
                   wide=_w["insn_bits"] > 64,
                   reg_storage=_regfile_storage(isa_reg),
                   byte_order=getattr(isa_reg.manifest.spec, "byte_order", "little"),
                   float_scalar_types=_float_scalar_types(isa_reg))

        root = pathlib.Path(output_dir)
        isa = isa_reg.name

        # Ship a clang-format config so adopted code formats to QEMU house style.
        write_generated(root / ".clang-format", CLANG_FORMAT_QEMU)

        render_to = make_renderer(env, ctx, clang_format=clang_format)

        if want("isa"):
            # target/{isa}/ - ISA semantics + QOM boilerplate
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
            # hw/{isa}/ - machine definition
            hw_dir = root / "hw" / isa
            hw_dir.mkdir(parents=True, exist_ok=True)
            render_to("qemu/qemu_hw_virt_c.j2",  hw_dir / "virt.c")
            render_to("qemu/qemu_hw_meson.j2",   hw_dir / "meson.build")
            render_to("qemu/qemu_hw_kconfig.j2", hw_dir / "Kconfig")
            # configs/ - build system configs
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
