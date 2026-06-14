import ast
from typing import Optional
from ..behavior import BehaviorIR
from .base import _BackendBase


def _indent(code: str) -> str:
    """Indent each non-blank line of a (possibly multi-line) statement block by
    four spaces, so generated `if`/`for` bodies are readable even without a
    post-generation clang-format pass."""
    return "\n".join("    " + ln if ln.strip() else ln for ln in code.split("\n"))


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
                  addr_mask: Optional[str] = None) -> str:
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

    def _translate_complex(self, node: ast.AST, state_prefix: Optional[str] = None) -> str:
        env_prefix = state_prefix or "env->"
        ir = self.ir

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
                if node.id in ir.write_vars or regfile in self._helper_only_regfiles:
                    return f"{env_prefix}{regfile}[{node.id}]"
                return f"{node.id}_val"
            return node.id

        raise ValueError(f"Unsupported syntax in behavior: '{ast.unparse(node)}'")
