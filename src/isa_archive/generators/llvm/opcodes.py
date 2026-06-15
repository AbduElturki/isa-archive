"""Heuristic fallbacks for the key opcodes (SP-adjust, LUI, JAL, JALR) when no
instruction declares the corresponding compiler role. Callers/templates guard on None."""
from typing import Optional


def _find_sp_adjust_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic add-immediate opcode for SP adjustment (prologue/epilogue).

    Used only as a fallback when no instruction declares the `frame.sp_adjust` /
    `alu_ri.add` compiler role. Returns None if nothing plausible is found —
    callers and templates guard on None.
    """
    for name, info in instr_defs:
        if name == "ADDI":
            return name
    for name, info in instr_defs:
        dag = info.get("dag_pattern") or ""
        if info["dag_category"] == "alu_ri" and "add" in dag.lower():
            return name
    for name, info in instr_defs:
        if info["dag_category"] == "alu_ri":
            return name
    return None


def _find_lui_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic load-upper-immediate opcode (for constant/GlobalAddress lowering).

    Fallback only; returns None if no plausible candidate exists.
    """
    for name, info in instr_defs:
        if name == "LUI":
            return name
    for name, info in instr_defs:
        if info["dag_category"] in ("custom", "alu_ri") and info["outs"] and not info["ins"]:
            return name
    return None


def _find_jal_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic direct-jump (JAL) opcode. Fallback only; None if not found."""
    for name, info in instr_defs:
        if name == "JAL":
            return name
    for name, info in instr_defs:
        if info["dag_category"] == "jump_abs":
            return name
    return None


def _find_jalr_opcode(instr_defs: list) -> Optional[str]:
    """Heuristic register-indirect-jump (JALR) opcode. Fallback only; None if not found."""
    for name, info in instr_defs:
        if name == "JALR":
            return name
    for name, info in instr_defs:
        if info["dag_category"] == "jump_ind":
            return name
    return None
