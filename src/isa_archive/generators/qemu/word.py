"""QEMU guest-word and register-file storage models for an ISA's data width."""
from ...models.scalar_types import of_register


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
