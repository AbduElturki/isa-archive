"""The compiler-role contract: inferring/collecting roles, the coverage report,
the set-less-than-via-branch synthesis, and constant-strategy inference."""
from typing import Optional


def _mem_role_names(xlen: int) -> list[str]:
    """Memory roles for an ISA of the given data width: sign/zero-extending pairs
    for every sub-word width, plus the full-word plain load/store."""
    roles: list[str] = []
    sub_widths = [w for w in (8, 16, 32, 64) if w < xlen]
    for w in sub_widths:
        roles += [f"mem.load{w}s", f"mem.load{w}u"]
    roles.append(f"mem.load{xlen}")
    roles += [f"mem.store{w}" for w in sub_widths] + [f"mem.store{xlen}"]
    return roles


def _role_groups(xlen: int) -> dict[str, list[str]]:
    """The role slots a backend can fill to lower C, grouped for the report."""
    return {
        "ALU rr":  [f"alu_rr.{op}" for op in ("add", "sub", "and", "or", "xor", "shl", "srl", "sra")],
        "ALU ri":  [f"alu_ri.{op}" for op in ("add", "and", "or", "xor", "shl", "srl", "sra")],
        "Const":   ["const.hi", "const.lo", "const.load"],
        "Memory":  _mem_role_names(xlen),
        "Branch":  [f"branch.{c}" for c in ("eq", "ne", "lt", "ge", "ltu", "geu")],
        "Compare": ["cmp.lt", "cmp.ltu", "cmp.lti", "cmp.ltui"],
        "Control": ["control.jump", "control.call", "control.call_indirect", "control.ret"],
        "Frame":   ["frame.sp_adjust"],
        "Global":  ["global.hi", "global.lo"],
    }


def _required_roles(xlen: int) -> set[str]:
    """Roles whose absence makes a working C backend impossible (drives --strict).
    `const` is checked separately (strategy-dependent: single_imm needs no hi/lo).
    Spill/reload uses the full-word load/store, so the requirement follows xlen."""
    return {
        "alu_rr.add", "alu_rr.sub", "alu_ri.add",
        f"mem.load{xlen}", f"mem.store{xlen}",
        "branch.eq", "branch.ne",
        "control.jump", "control.ret", "frame.sp_adjust",
    }

_BRANCH_COND_TO_ROLE: dict[str, str] = {
    "seteq": "eq", "setne": "ne", "setlt": "lt", "setge": "ge",
    "setult": "ltu", "setuge": "geu", "setle": "le", "setgt": "gt",
    "setugt": "ugt", "setule": "ule",
}


_SETCC_TO_CMP: dict[str, str] = {
    "setlt": "lt", "setult": "ltu", "seteq": "eq", "setne": "ne",
    "setle": "le", "setge": "ge", "setgt": "gt",
    "setugt": "ugt", "setule": "ule", "setuge": "uge",
}


def _infer_roles(info: dict, xlen: int = 32) -> set[str]:
    """Specific compiler roles inferred from an instruction's behavior (layer 1)."""
    roles: set[str] = set()
    cat = info["dag_category"]
    op = info.get("dag_op")
    if op in _SETCC_TO_CMP:
        # set-less-than family: SLT/SLTU → cmp.lt/ltu, SLTI/SLTIU → cmp.lti/ltui
        suffix = _SETCC_TO_CMP[op]
        roles.add(f"cmp.{suffix}i" if cat == "alu_ri" else f"cmp.{suffix}")
    elif cat == "alu_rr" and op and op != "copy":
        roles.add(f"alu_rr.{op}")
    elif cat == "alu_ri" and op and op != "copy":
        roles.add(f"alu_ri.{op}")
    elif cat == "load":
        w = info.get("dag_load_width")
        if w:
            # Sub-word loads carry a sign/zero-extension suffix; the full-word
            # load doesn't extend (suffix rule keyed on xlen, not a literal 32).
            roles.add(f"mem.load{w}" if w >= xlen
                      else f"mem.load{w}{'s' if info.get('dag_load_signed') else 'u'}")
    elif cat == "store":
        w = info.get("dag_load_width")
        if w:
            roles.add(f"mem.store{w}")
    elif cat == "branch":
        cond = _BRANCH_COND_TO_ROLE.get(info.get("branch_cond") or "")
        if cond:
            roles.add(f"branch.{cond}")
    elif cat == "jump_abs":
        roles.add("control.jump")
        if info.get("is_call"):
            roles.add("control.call")
    elif cat == "jump_ind":
        roles.add("control.ret" if info.get("is_return") else "control.call_indirect")
    return roles


def _expand_role(role: str, info: dict, xlen: int = 32) -> set[str]:
    """Expand a declared role tag. Specific roles (with a dot) pass through; a bare
    shape (`alu_rr`, `branch`) is expanded using the behavior-inferred op/condition."""
    if "." in role:
        return {role}
    if role in ("alu_rr", "alu_ri"):
        op = info.get("dag_op")
        return {f"{role}.{op}"} if op and op != "copy" else set()
    if role == "branch":
        cond = _BRANCH_COND_TO_ROLE.get(info.get("branch_cond") or "")
        return {f"branch.{cond}"} if cond else set()
    # load/store/control/const/frame/global bare shapes → fall back to inference
    return _infer_roles(info, xlen)


def _collect_compiler_roles(instr_defs: list, xlen: int = 32) -> tuple[dict[str, str], list[tuple]]:
    """Merge the three role layers (infer → schema → instruction) into a role→opcode
    map. Returns (role_to_opcode, conflicts) where conflicts are (role, first, second)."""
    role_to_opcode: dict[str, str] = {}
    conflicts: list[tuple] = []
    for name, info in instr_defs:
        roles = set(_infer_roles(info, xlen))
        for r in info.get("schema_roles", []):
            roles |= _expand_role(r, info, xlen)
        for r in info.get("instr_roles", []):
            roles |= _expand_role(r, info, xlen)
        for r in roles:
            if r in role_to_opcode and role_to_opcode[r] != name:
                conflicts.append((r, role_to_opcode[r], name))
            else:
                role_to_opcode.setdefault(r, name)
    return role_to_opcode, conflicts


def _build_coverage_report(isa_name: str, roles: dict[str, str], conflicts: list[tuple],
                            const_strategy: str,
                            has_ordering_branches: bool = True,
                            xlen: int = 32,
                            profile: str = "c-baremetal",
                            required: Optional[set] = None,
                            missing_prereqs: Optional[list] = None,
                            custom_instrs: Optional[list] = None) -> tuple[str, list[str]]:
    """Render COMPILER_COVERAGE.md text and return (markdown, missing_required).

    ``required`` is the profile-resolved required-role set; ``missing_prereqs``
    are non-role prerequisites (sp/ra/zero aliases) the profile demands;
    ``custom_instrs`` is [(name, notes)] for custom-lowered instructions (G4).
    """
    required = _required_roles(xlen) if required is None else required
    lines = [f"# {isa_name} compiler coverage", "",
             f"Profile: `{profile}`", ""]
    missing_required: list[str] = list(missing_prereqs or [])
    for group, group_roles in _role_groups(xlen).items():
        cells = []
        for r in group_roles:
            present = r in roles
            short = r.split(".", 1)[1] if "." in r else r
            cells.append(f"{short} {'✓' if present else '✗'}")
            if r in required and not present:
                missing_required.append(r)
        lines.append(f"- **{group}**: " + "  ".join(cells))

    # Ordering comparisons must be available either as direct compare-branches
    # (branch.lt/ge/ltu/geu) or as set-less-than (cmp.lt/cmp.ltu) + branch-on-zero.
    # These supplements encode what lowering C needs, so only the c-baremetal
    # contract adds them; a custom profile requires exactly its `requires` list.
    if profile == "c-baremetal" and not has_ordering_branches:
        for r in ("cmp.lt", "cmp.ltu"):
            if r not in roles:
                missing_required.append(r)

    # Const: strategy-dependent requirement (again, a C-lowering need)
    lines.append(f"- **Const strategy**: `{const_strategy}`")
    if profile == "c-baremetal":
        if const_strategy in ("hi_lo_add", "hi_lo_or", "shift_build"):
            for r in ("const.hi", "const.lo"):
                if r not in roles:
                    missing_required.append(r)
        elif const_strategy == "single_imm":
            if "const.load" not in roles:
                missing_required.append("const.load")

    if conflicts:
        lines.append("")
        lines.append("## Conflicts (multiple instructions claim one role)")
        for role, first, second in conflicts:
            lines.append(f"- `{role}`: {first} vs {second} (using {first})")

    if custom_instrs:
        lines.append("")
        lines.append("## Custom-lowered instructions (no selectable pattern)")
        for name, notes in custom_instrs:
            why = "; ".join(notes) if notes else "behavior not expressible as a single DAG pattern"
            lines.append(f"- `{name}`: {why}")

    status = "COMPILER-COMPLETE ✓" if not missing_required else \
             "INCOMPLETE ✗ — missing: " + ", ".join(sorted(set(missing_required)))
    lines += ["", f"**STATUS: {status}** (profile `{profile}`)", ""]
    return "\n".join(lines), sorted(set(missing_required))


def _setcc_branch_entries(roles: dict[str, str]) -> list[dict]:
    """How to materialize each integer comparison into a 0/1 register on an ISA
    that has conditional branches but NO set-less-than instruction.

    Each entry maps an ISD set-condition node (``seteq``, ``setlt``, …) to a
    branch opcode plus two flags: ``swap`` (compare the operands reversed) and
    ``taken_one`` (taking the branch yields 1, else 0). All ten conditions are
    synthesized from eq/ne/lt/ltu, preferring a direct ge/geu branch when the
    ISA provides one. The custom inserter turns these into a branch diamond.
    """
    b = {c: roles.get(f"branch.{c}") for c in ("eq", "ne", "lt", "ge", "ltu", "geu")}
    entries: list[dict] = []

    def add(node: str, opcode: Optional[str], swap: bool, taken_one: bool) -> None:
        if opcode:
            entries.append({"node": node, "opcode": opcode,
                            "swap": swap, "taken_one": taken_one})

    add("seteq", b["eq"], False, True)
    add("setne", b["ne"], False, True)
    if b["lt"]:
        add("setlt", b["lt"], False, True)
        add("setgt", b["lt"], True, True)                    # a>b ⟺ b<a
        # a>=b ⟺ !(a<b): branch a<b, taken→0; or a direct bge → taken→1
        add("setge", b["ge"] or b["lt"], False, bool(b["ge"]))
        add("setle", b["ge"] or b["lt"], True, bool(b["ge"]))  # a<=b ⟺ !(b<a) / b>=a
    elif b["ge"]:
        add("setge", b["ge"], False, True)
        add("setle", b["ge"], True, True)
    if b["ltu"]:
        add("setult", b["ltu"], False, True)
        add("setugt", b["ltu"], True, True)
        add("setuge", b["geu"] or b["ltu"], False, bool(b["geu"]))
        add("setule", b["geu"] or b["ltu"], True, bool(b["geu"]))
    elif b["geu"]:
        add("setuge", b["geu"], False, True)
        add("setule", b["geu"], True, True)

    for i, e in enumerate(entries):
        e["code"] = i
    return entries


def _infer_const_strategy(roles: dict[str, str], instr_defs: list) -> str:
    """Infer the constant-materialization strategy from declared roles + behavior.

    - single_imm  : a `const.load` instruction whose immediate spans the full word
    - hi_lo_or    : const.hi + const.lo, lo instruction zero-extends its immediate
    - hi_lo_add   : const.hi + const.lo, lo instruction sign-extends (RISC-V)
    - shift_build : no const.hi but shift + or available
    """
    info_by_name = {n: i for n, i in instr_defs}
    if "const.load" in roles:
        return "single_imm"
    if "const.hi" in roles and "const.lo" in roles:
        lo_info = info_by_name.get(roles["const.lo"], {})
        # zero-extended lo (e.g. MIPS ORI) → no sign-compensation needed
        return "hi_lo_or" if (lo_info.get("dag_op") in ("or", "xor")) else "hi_lo_add"
    if "const.hi" not in roles and ("alu_ri.shl" in roles and "alu_ri.or" in roles):
        return "shift_build"
    return "hi_lo_add"  # default; coverage report flags missing hi/lo
