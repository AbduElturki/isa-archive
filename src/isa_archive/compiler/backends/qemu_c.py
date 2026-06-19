import ast
from typing import Optional
from ..behavior import BehaviorIR
from .base import _BackendBase
from ...models.scalar_types import ArithClass


def _indent(code: str) -> str:
    """Indent each non-blank line of a (possibly multi-line) statement block by
    four spaces, so generated `if`/`for` bodies are readable even without a
    post-generation clang-format pass."""
    return "\n".join("    " + ln if ln.strip() else ln for ln in code.split("\n"))


def build_do_interrupt_body(trap_info, csr_info, pc_mask=None,
                            env_prefix="env->", cause_var="cause") -> Optional[str]:
    """C statements for QEMU's `do_interrupt` on an ISA with a `trap:` block:
    vector through the trap CSRs exactly like a software `trap()` — save epc, set
    `cause_csr` to the C variable `cause_var` (the caller picks the value: the
    interrupt marker for an IRQ, the cause code for a synchronous exception), do
    the mie->mpie save / mie=0 shuffle, then `pc = mtvec & ~3`. This is the same
    vectoring `QemuCBackend._emit_trap` emits, so hardware interrupts and software
    traps share one path. Returns None if the ISA declares no `trap:` block (the
    CPU keeps the halt-on-exception fallback).
    """
    if not trap_info:
        return None
    p = env_prefix
    lines = [f"{p}{trap_info['epc_csr']} = {p}pc;",
             f"{p}{trap_info['cause_csr']} = {cause_var};"]
    sc = trap_info.get("status_csr")
    scf = csr_info.get(sc, {}).get("fields", {}) if sc else {}
    if sc and "mie" in scf and "mpie" in scf:
        ms, mw = scf["mie"]
        ps, pw = scf["mpie"]
        dmask = ((1 << pw) - 1) << ps          # mpie = mie
        lines.append(f"{p}{sc} = ({p}{sc} & ~{hex(dmask)}) | "
                     f"((({p}{sc} >> {ms}) & {hex((1 << mw) - 1)}) << {ps});")
        mmask = ((1 << mw) - 1) << ms          # mie = 0
        lines.append(f"{p}{sc} = {p}{sc} & ~{hex(mmask)};")
    pcv = f"{p}{trap_info['vector_csr']} & ~0x3"
    if pc_mask:
        pcv = f"({pcv}) & {pc_mask}"
    lines.append(f"{p}pc = {pcv};")
    return "\n".join(lines)


def _c_int_types(width: int) -> tuple[str, str, int]:
    """(unsigned C type, signed C type, storage bits) for a value width.

    Standard uintN_t up to 64; native __uint128_t up to 128 (available on
    every 64-bit host compiler QEMU supports). Wider has no C type.
    """
    if width <= 64:
        bits = next(b for b in (8, 16, 32, 64) if width <= b)
        return f"uint{bits}_t", f"int{bits}_t", bits
    if width <= 128:
        return "__uint128_t", "__int128_t", 128
    raise ValueError(
        f"no C integer type holds {width} bits (maximum is native 128-bit)"
    )


class QemuCBackend(_BackendBase):
    def translate(self, env_prefix: str = "env->",
                  pc_write_tracking: bool = False,
                  zero_register_map: Optional[dict] = None,
                  float_regs: Optional[dict] = None,
                  helper_only_regfiles: Optional[set] = None,
                  regfile_write_masks: Optional[dict] = None,
                  pc_mask: Optional[str] = None,
                  addr_mask: Optional[str] = None,
                  csr_info: Optional[dict] = None,
                  trap_info: Optional[dict] = None,
                  regfile_shapes: Optional[dict] = None,
                  regfile_attrs: Optional[dict] = None) -> str:
        # csr_info: {csr_name → {"width": int, "fields": {field → (start, width)}}}
        # trap_info: {"vector_csr","epc_csr","cause_csr","status_csr","causes"} or None
        self._csr_info = csr_info or {}
        self._trap_info = trap_info
        # Shaped (vector/tile) register files; element access uses the IR recognizer.
        self._regfile_shapes = regfile_shapes or self.ir.regfile_shapes
        # Per-register attributes ({file → {attr → width}}).
        self._regfile_attrs = regfile_attrs or self.ir.regfile_attrs
        self._pc_write_tracking = pc_write_tracking
        self._zero_register_map = zero_register_map or {}
        # {register-file name → ScalarType} for floating-point files; arithmetic on
        # these is done in float space via bit-reinterpreting u2f/f2u helpers.
        self._float_regs = float_regs or {}
        # Register files with no TCG global (width ≠ xlen): helpers receive the
        # register *index* and read env-> state directly instead of a passed value.
        self._helper_only_regfiles = helper_only_regfiles or set()
        # {register-file name → hex mask} for files whose architectural width is
        # narrower than their C storage type; every write is masked.
        self._regfile_write_masks = regfile_write_masks or {}
        # Width of the value context (assignment target / comparison operands);
        # sext/zext/signed casts use it instead of assuming xlen, so arithmetic
        # on non-xlen register files extends to the right width.
        self._cast_width = None
        # When the architectural xlen is narrower than the QEMU guest word
        # (xlen 8/16 emulated over a 32-bit target), PC writes and memory
        # addresses are masked to the xlen address space.
        self._pc_mask = pc_mask
        self._addr_mask = addr_mask
        decls = []
        for name, (width, type_name) in self.ir.temporaries.items():
            if type_name == "_inline_": continue
            if type_name: decls.append(f"{type_name}_t {name};")
            else: decls.append(f"{_c_int_types(width)[0]} {name};")
        body = [self._translate(stmt, env_prefix) for stmt in self.ir.tree.body]
        return "\n".join(decls + body)

    def _csr_field(self, csr_name: str, field: str) -> tuple[int, int, int]:
        """(start, width, field-mask) for a CSR field, from csr_info."""
        info = self._csr_info.get(csr_name)
        if not info or field not in info["fields"]:
            raise ValueError(f"CSR 'csr.{csr_name}' has no field '{field}'")
        start, width = info["fields"][field]
        return start, width, ((1 << width) - 1) << start

    def _status_update(self, env_prefix: str, src: str, dst: str) -> list[str]:
        """Copy status-CSR field `src` into `dst` then (for traps) clear `src`.
        Used for the mie/mpie save-restore dance; no-op if no status CSR or the
        fields aren't declared."""
        ti = self._trap_info or {}
        sc = ti.get("status_csr")
        if not sc or sc not in self._csr_info:
            return []
        fields = self._csr_info[sc]["fields"]
        if src not in fields or dst not in fields:
            return []
        state = f"{env_prefix}{sc}"
        ss, sw, _ = self._csr_field(sc, src)
        ds, dw, dmask = self._csr_field(sc, dst)
        return [f"{state} = ({state} & ~{hex(dmask)}) | "
                f"((({state} >> {ss}) & {hex((1 << sw) - 1)}) << {ds});"]

    def _status_clear(self, env_prefix: str, field: str) -> list[str]:
        """Clear a status-CSR field (e.g. mie on trap entry); no-op if absent."""
        ti = self._trap_info or {}
        sc = ti.get("status_csr")
        if not sc or sc not in self._csr_info or field not in self._csr_info[sc]["fields"]:
            return []
        _, _, mask = self._csr_field(sc, field)
        return [f"{env_prefix}{sc} = {env_prefix}{sc} & ~{hex(mask)};"]

    def _emit_trap(self, node: ast.Call, env_prefix: str) -> str:
        ti = self._trap_info
        if not ti:
            raise ValueError("trap() used but the ISA declares no `trap:` block")
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant):
            cause = arg.value
        elif isinstance(arg, ast.Name) and arg.id in ti["causes"]:
            cause = ti["causes"][arg.id]
        else:
            raise ValueError("trap() expects an integer or a declared cause name")
        lines = [f"{env_prefix}{ti['epc_csr']} = {env_prefix}pc;",
                 f"{env_prefix}{ti['cause_csr']} = {cause};"]
        lines += self._status_update(env_prefix, "mie", "mpie")  # mpie = mie
        lines += self._status_clear(env_prefix, "mie")           # mie = 0
        pcv = f"{env_prefix}{ti['vector_csr']} & ~0x3"
        if self._pc_mask:
            pcv = f"({pcv}) & {self._pc_mask}"
        lines.append(f"{env_prefix}pc = {pcv};")
        return "\n".join(lines)

    def _emit_trap_return(self, env_prefix: str) -> str:
        ti = self._trap_info
        if not ti:
            raise ValueError("trap_return() used but the ISA declares no `trap:` block")
        lines = self._status_update(env_prefix, "mpie", "mie")  # mie = mpie
        pcv = f"{env_prefix}{ti['epc_csr']}"
        if self._pc_mask:
            pcv = f"({pcv}) & {self._pc_mask}"
        lines.append(f"{env_prefix}pc = {pcv};")
        return "\n".join(lines)

    def _emit_subarray_copy(self, lhs_acc, rhs_node, env_prefix: str) -> str:
        """Copy a partially-indexed sub-array (`td[i] = ts[i]` on a multi-dim file)
        via nested loops over the remaining dimensions. Only register→register
        sub-array moves are supported (partial indices can't be operated on)."""
        lname, lfile, lelem, lshape, lidx = lhs_acc
        remaining = lshape[len(lidx):]
        rhs_acc = self.ir.reg_element_access(rhs_node)
        if rhs_acc is None:
            raise ValueError(
                f"partially-indexed '{lname}' can only be assigned another sub-array "
                f"of matching shape (got '{ast.unparse(rhs_node)}')")
        rname, rfile, relem, rshape, ridx = rhs_acc
        if rshape[len(ridx):] != remaining or relem.width != lelem.width:
            raise ValueError(
                f"sub-array shape/element mismatch copying into '{lname}' "
                f"(remaining {remaining} vs {rshape[len(ridx):]})")
        pv = [f"_p{k}" for k in range(len(remaining))]
        lbase = f"{env_prefix}{lfile}[{lname}]" + "".join(
            f"[{self._translate(ix, env_prefix)}]" for ix in lidx)
        rbase = f"{env_prefix}{rfile}[{rname}]" + "".join(
            f"[{self._translate(ix, env_prefix)}]" for ix in ridx)
        body = (lbase + "".join(f"[{v}]" for v in pv) + " = "
                + rbase + "".join(f"[{v}]" for v in pv) + ";")
        for k in range(len(remaining) - 1, -1, -1):
            body = (f"for (uint32_t {pv[k]} = 0; {pv[k]} < {remaining[k]}; {pv[k]}++) "
                    f"{{\n{_indent(body)}\n}}")
        return body

    def _translate_complex(self, node: ast.AST, state_prefix: Optional[str] = None) -> str:
        env_prefix = state_prefix or "env->"
        ir = self.ir

        # Register attribute read: reg.attr → env->file_attr[reg]
        attr = self.ir.reg_attr_access(node)
        if attr is not None and not isinstance(getattr(node, "ctx", None), ast.Store):
            regop, regfile, aname, _w = attr
            return f"{env_prefix}{regfile}_{aname}[{regop}]"

        # Shaped-register element read: vd[i] / vd[i][j] → env->file[vd][i][j]
        acc = self.ir.reg_element_access(node)
        if acc is not None and not isinstance(getattr(node, "ctx", None), ast.Store):
            name, regfile, elem_st, shape, indices = acc
            if len(indices) != len(shape):
                raise ValueError(
                    f"'{name}' is a shaped register {shape}; index all {len(shape)} "
                    f"dimension(s) to reach an element")
            idxs = "".join(f"[{self._translate(ix, env_prefix)}]" for ix in indices)
            return f"{env_prefix}{regfile}[{name}]{idxs}"

        # CSR read: csr.NAME → env->NAME ; csr.NAME.FIELD → masked extract
        csr_r = BehaviorIR.csr_ref(node)
        if csr_r is not None and not isinstance(getattr(node, "ctx", None), ast.Store):
            csr_name, field = csr_r
            state = f"{env_prefix}{csr_name}"
            if field is None:
                return state
            start, width, _ = self._csr_field(csr_name, field)
            return f"(({state} >> {start}) & {hex((1 << width) - 1)})"

        if isinstance(node, ast.If):
            prev_cw = self._cast_width
            reg_widths = [
                ir.var_widths[n.id] for n in ast.walk(node.test)
                if isinstance(n, ast.Name) and n.id in ir.register_map
                and n.id in ir.var_widths
            ]
            if reg_widths:
                self._cast_width = max(reg_widths)
            cond = self._translate(node.test, env_prefix)
            self._cast_width = prev_cw
            body = _indent("\n".join(self._translate(s, env_prefix) for s in node.body))
            res = f"if ({cond}) {{\n{body}\n}}"
            if node.orelse:
                orelse = _indent("\n".join(self._translate(s, env_prefix) for s in node.orelse))
                res += f" else {{\n{orelse}\n}}"
            return res

        if isinstance(node, ast.For):
            if not (isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name)
                    and node.iter.func.id == "range"):
                raise ValueError("Only 'for i in range(...)' is supported")
            loop_var = node.target.id
            args = node.iter.args
            start = "0" if len(args) == 1 else self._translate(args[0], env_prefix)
            end = self._translate(args[0] if len(args) == 1 else args[1], env_prefix)
            body = _indent("\n".join(self._translate(s, env_prefix) for s in node.body))
            return f"for (uint32_t {loop_var} = {start}; {loop_var} < {end}; {loop_var}++) {{\n{body}\n}}"

        if isinstance(node, ast.Assign):
            target_var = node.targets[0]
            # Shaped-register element write: vd[i] = v  →  env->file[vd][i] = (v) [& mask]
            # Register attribute write: reg.attr = v → env->file_attr[reg] = (v) [& mask]
            attr_w = self.ir.reg_attr_access(target_var)
            if attr_w is not None:
                regop, regfile, aname, awidth = attr_w
                val = self._translate(node.value, env_prefix)
                sb = next(b for b in (8, 16, 32, 64) if awidth <= b)
                if awidth < sb:
                    val = f"({val}) & {hex((1 << awidth) - 1)}"
                return f"{env_prefix}{regfile}_{aname}[{regop}] = {val};"
            acc_w = self.ir.reg_element_access(target_var)
            if acc_w is not None:
                name, regfile, elem_st, shape, indices = acc_w
                # Partial index (vd[i] on a multi-dim file) → sub-array copy.
                if len(indices) < len(shape):
                    return self._emit_subarray_copy(acc_w, node.value, env_prefix)
                idxs = "".join(f"[{self._translate(ix, env_prefix)}]" for ix in indices)
                lhs = f"{env_prefix}{regfile}[{name}]{idxs}"
                # Float-element arithmetic: operate in float space via u2f/f2u, like
                # the whole-register float path. Needs a native host C type.
                if elem_st.arith_class == ArithClass.IEEE_FLOAT and isinstance(node.value, ast.BinOp):
                    if elem_st.c_type is None:
                        raise ValueError(
                            f"float-element arithmetic on '{name}' needs softfloat: no "
                            f"native host C type for '{elem_st.token}'")
                    w = elem_st.width
                    op = BehaviorIR.OPERATORS.get(type(node.value.op))
                    l = self._translate(node.value.left, env_prefix)
                    r = self._translate(node.value.right, env_prefix)
                    return f"{lhs} = f2u{w}(u2f{w}({l}) {op} u2f{w}({r}));"
                val = self._translate(node.value, env_prefix)
                mask = self._regfile_write_masks.get(regfile)
                if mask:
                    val = f"({val}) & {mask}"
                return f"{lhs} = {val};"
            # CSR write: csr.NAME = v  (mask to CSR width) ; csr.NAME.FIELD = v (RMW)
            csr_w = BehaviorIR.csr_ref(target_var)
            if csr_w is not None:
                csr_name, field = csr_w
                state = f"{env_prefix}{csr_name}"
                val = self._translate(node.value, env_prefix)
                if field is None:
                    cw = self._csr_info.get(csr_name, {}).get("width")
                    mask = f" & {hex((1 << cw) - 1)}" if cw else ""
                    return f"{state} = ({val}){mask};"
                start, width, fmask = self._csr_field(csr_name, field)
                return (f"{state} = ({state} & ~{hex(fmask)}) | "
                        f"((({val}) & {hex((1 << width) - 1)}) << {start});")
            if (isinstance(target_var, ast.Subscript)
                    and isinstance(target_var.value, ast.Name)
                    and target_var.value.id in BehaviorIR.MEM_KEYWORDS):
                mem_width = BehaviorIR.MEM_KEYWORDS[target_var.value.id]
                addr = self._translate(target_var.slice, env_prefix)
                val = self._translate(node.value, env_prefix)
                vw = ir.get_width(node.value)
                if mem_width != vw and not (isinstance(node.value, ast.Constant) and vw <= mem_width):
                    raise ValueError(
                        f"Width mismatch: cannot write '{ast.unparse(node.value)}' ({vw} bits) "
                        f"into {target_var.value.id}[...] ({mem_width}-bit slot)"
                    )
                funcs = {8: "cpu_stb_data_ra", 16: "cpu_stw_data_ra",
                         32: "cpu_stl_data_ra", 64: "cpu_stq_data_ra"}
                if self._addr_mask:
                    addr = f"({addr}) & {self._addr_mask}"
                return f"{funcs[mem_width]}(env, {addr}, {val}, GETPC());"
            # Floating-point arithmetic: a binop whose destination is a float
            # register file → reinterpret operands as float, operate, store bits.
            if (isinstance(target_var, ast.Name) and target_var.id in ir.register_map
                    and ir.register_map[target_var.id] in self._float_regs
                    and isinstance(node.value, ast.BinOp)):
                st = self._float_regs[ir.register_map[target_var.id]]
                if st.c_type is None:
                    raise ValueError(
                        f"{st.width}-bit float arithmetic needs softfloat: no native "
                        f"host C type for '{st.token}' (QEMU functional model)."
                    )
                w = st.width
                op = BehaviorIR.OPERATORS.get(type(node.value.op))
                l = self._translate(node.value.left, env_prefix)
                r = self._translate(node.value.right, env_prefix)
                target_name = self._translate(target_var, env_prefix)
                return (f"{target_name} = f2u{w}(u2f{w}({l}) {op} u2f{w}({r}));")
            target_name = self._translate(target_var, env_prefix)
            tw = ir.get_width(target_var)
            prev_cw = self._cast_width
            self._cast_width = tw
            value_code = self._translate(node.value, env_prefix)
            self._cast_width = prev_cw
            vw = ir.get_width(node.value)
            # A top-level sext/zext/signed adapts to the target's width (the
            # generated cast uses it), so it can't be a width mismatch.
            adaptive = (isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name)
                        and node.value.func.id in ("sext", "zext", "signed"))
            if tw != vw and not adaptive \
                    and not (isinstance(node.value, ast.Constant) and vw <= tw):
                raise ValueError(
                    f"Width mismatch: '{ast.unparse(target_var)}' is {tw} bits but "
                    f"'{ast.unparse(node.value)}' evaluates to {vw} bits"
                )
            if target_name == f"{env_prefix}pc":
                prefix = "_branch_taken = 1;\n" if self._pc_write_tracking else ""
                if self._pc_mask:
                    value_code = f"({value_code}) & {self._pc_mask}"
                return f"{prefix}{env_prefix}pc = {value_code};"
            if isinstance(target_var, ast.Name) and target_var.id in ir.register_map:
                mask = self._regfile_write_masks.get(ir.register_map[target_var.id])
                if mask:
                    value_code = f"({value_code}) & {mask}"
                zero_idx = self._zero_register_map.get(target_var.id)
                if zero_idx is not None:
                    return (f"if ({target_var.id} != {zero_idx}) {{\n"
                            f"    {target_name} = {value_code};\n}}")
            return f"{target_name} = {value_code};"

        if isinstance(node, ast.AugAssign):
            target_name = self._translate(node.target, env_prefix)
            value_code = self._translate(node.value, env_prefix)
            op = BehaviorIR.OPERATORS.get(type(node.op))
            return f"{target_name} {op}= {value_code};"

        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in BehaviorIR.MEM_KEYWORDS:
                mem_width = BehaviorIR.MEM_KEYWORDS[node.value.id]
                addr = self._translate(node.slice, env_prefix)
                funcs = {8: "cpu_ldub_data_ra", 16: "cpu_lduw_data_ra",
                         32: "cpu_ldl_data_ra", 64: "cpu_ldq_data_ra"}
                if self._addr_mask:
                    addr = f"({addr}) & {self._addr_mask}"
                return f"{funcs[mem_width]}(env, {addr}, GETPC())"
            var = self._translate(node.value, env_prefix)
            if isinstance(node.slice, ast.Slice):
                l = node.slice.lower.value if node.slice.lower else 0
                u = node.slice.upper.value if node.slice.upper else ir.get_width(node.value)
                return f"(({var} >> {l}) & {hex((1 << (u - l)) - 1)})"

        if isinstance(node, ast.Set):
            shift = 0
            terms = []
            for elt in reversed(node.elts):
                w = ir.get_width(elt)
                val = self._translate(elt, env_prefix)
                terms.append(val if shift == 0 else f"({val} << {shift})")
                shift += w
            return "(" + " | ".join(terms) + ")"

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func_id = node.func.id
            if func_id == "trap":
                return self._emit_trap(node, env_prefix)
            if func_id == "trap_return":
                return self._emit_trap_return(env_prefix)
            if func_id in ("sext", "signed", "zext"):
                # Extend to the width of the value context (assignment target /
                # comparison operands), defaulting to the data width. Rounded up
                # to a C type that exists (uintN_t, or __uint128_t at 128).
                w = self._cast_width or ir.var_widths.get("pc", 32)
                inner = self._translate(node.args[0], env_prefix)
                if func_id == "sext":
                    n = node.args[1].value
                    if n > w:  # extending to less than n bits makes no sense
                        w = n
                    _, _, bits = _c_int_types(w)
                    # Sign-extend the low n bits via a generated isa_sextN() helper
                    # (defined in the helpers preamble) so the call site stays
                    # readable instead of a nested double-cast/shift expression.
                    return f"isa_sext{bits}({inner}, {n})"
                utype, stype, _ = _c_int_types(w)
                if func_id == "signed":
                    return f"({stype})({inner})"
                return f"({utype})({inner})"
            if func_id in ir.operands:
                args = [self._translate(arg, env_prefix) for arg in node.args]
                return f"{func_id}({', '.join(args)})"

        if isinstance(node, ast.Name):
            if node.id == "pc":
                return f"{env_prefix}pc"
            if node.id in ir.register_map:
                regfile = ir.register_map[node.id]
                if regfile in self._regfile_shapes:
                    raise ValueError(
                        f"shaped register '{node.id}' must be indexed to an element "
                        f"(e.g. {node.id}[i]); whole-register ops aren't supported")
                if node.id in ir.write_vars or regfile in self._helper_only_regfiles:
                    return f"{env_prefix}{regfile}[{node.id}]"
                return f"{node.id}_val"
            return node.id

        raise ValueError(f"Unsupported syntax in behavior: '{ast.unparse(node)}'")
