import ast
from typing import Optional
from ..behavior import BehaviorIR
from .base import _BackendBase


class RustBackend(_BackendBase):
    def translate(self) -> str:
        decls = []
        for name, (width, type_name) in self.ir.temporaries.items():
            if type_name == "_inline_": continue
            if type_name: decls.append(f"let mut {name}: {type_name};")
            else: decls.append(f"let mut {name}: u{width};")
        body = [self._translate(stmt) for stmt in self.ir.tree.body]
        return "\n".join(decls + body)

    def _translate_complex(self, node: ast.AST, state_prefix: Optional[str] = None) -> str:
        ir = self.ir

        if isinstance(node, ast.If):
            cond = self._translate(node.test)
            body = "\n".join(self._translate(s) for s in node.body)
            res = f"if {cond} {{\n{body}\n}}"
            if node.orelse:
                orelse = "\n".join(self._translate(s) for s in node.orelse)
                res += f" else {{\n{orelse}\n}}"
            return res

        if isinstance(node, ast.For):
            if not (isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Name)
                    and node.iter.func.id == "range"):
                raise ValueError("Only 'for i in range(...)' is supported")
            loop_var = node.target.id
            args = node.iter.args
            start = "0" if len(args) == 1 else self._translate(args[0])
            end = self._translate(args[0] if len(args) == 1 else args[1])
            body = "\n".join(self._translate(s) for s in node.body)
            return f"for {loop_var} in {start}..{end} {{\n{body}\n}}"

        if isinstance(node, ast.Assign):
            target_name = self._translate(node.targets[0])
            value_code = self._translate(node.value)
            return f"{target_name} = {value_code};"

        if isinstance(node, ast.AugAssign):
            target_name = self._translate(node.target)
            value_code = self._translate(node.value)
            op = BehaviorIR.OPERATORS.get(type(node.op))
            return f"{target_name} {op}= {value_code};"

        if isinstance(node, ast.Subscript):
            var = self._translate(node.value)
            if isinstance(node.slice, ast.Slice):
                l = node.slice.lower.value if node.slice.lower else 0
                u = node.slice.upper.value if node.slice.upper else ir.get_width(node.value)
                return f"(({var} >> {l}) & {hex((1 << (u - l)) - 1)})"
            return f"{var}[{self._translate(node.slice)}]"

        if isinstance(node, ast.Set):
            shift = 0
            terms = []
            for elt in reversed(node.elts):
                w = ir.get_width(elt)
                val = self._translate(elt)
                terms.append(val if shift == 0 else f"({val} << {shift})")
                shift += w
            return "(" + " | ".join(terms) + ")"

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func_id = node.func.id
            if func_id == "sext":
                inner = self._translate(node.args[0])
                n = node.args[1].value
                xlen = ir.var_widths.get("pc", 32)
                shift = xlen - n
                return f"(({inner} as i{xlen}).wrapping_shl({shift}).wrapping_shr({shift})) as u{xlen}"
            if func_id == "signed":
                inner = self._translate(node.args[0])
                xlen = ir.var_widths.get("pc", 32)
                return f"({inner} as i{xlen})"
            if func_id in ir.operands:
                args = [self._translate(arg) for arg in node.args]
                return f"{func_id}::new({', '.join(args)})"

        if isinstance(node, ast.Name):
            return node.id

        raise ValueError(f"Unsupported syntax in behavior: '{ast.unparse(node)}'")
