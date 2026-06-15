import ast
from typing import List, Optional
from ..behavior import BehaviorIR
from .base import _BackendBase


class VerilogBackend(_BackendBase):
    def translate(self) -> str:
        # CSR / trap / attribute / shaped-register semantics aren't modeled in the
        # combinational RTL skeleton yet; emit a placeholder rather than failing.
        if self.ir.uses_structured:
            return "// CSR / trap / vector / attribute semantics not modeled in the RTL skeleton yet"
        self._mem_reads: List[str] = []
        decls = []
        for name, (width, type_name) in self.ir.temporaries.items():
            if type_name == "_inline_": continue
            if type_name: decls.append(f"{type_name}_t {name};")
            else: decls.append(f"logic [{width-1}:0] {name};")
        body = [self._translate(stmt) for stmt in self.ir.tree.body]
        return "\n".join(decls + self._mem_reads + body)

    def _translate_complex(self, node: ast.AST, state_prefix: Optional[str] = None) -> str:
        ir = self.ir

        if isinstance(node, ast.If):
            cond = self._translate(node.test)
            body = "\n".join(self._translate(s) for s in node.body)
            res = f"if ({cond}) begin\n{body}\nend"
            if node.orelse:
                orelse = "\n".join(self._translate(s) for s in node.orelse)
                res += f" else begin\n{orelse}\nend"
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
            return f"for (int {loop_var} = {start}; {loop_var} < {end}; {loop_var}++) begin\n{body}\nend"

        if isinstance(node, ast.Assign):
            target_var = node.targets[0]
            if (isinstance(target_var, ast.Subscript)
                    and isinstance(target_var.value, ast.Name)
                    and target_var.value.id in BehaviorIR.MEM_KEYWORDS):
                mem_width = BehaviorIR.MEM_KEYWORDS[target_var.value.id]
                addr = self._translate(target_var.slice)
                val = self._translate(node.value)
                size = {8: "3'b000", 16: "3'b001", 32: "3'b010", 64: "3'b011"}.get(mem_width, "3'b000")
                return (f"mem_req = 1'b1;\nmem_we = 1'b1;\nmem_addr = {addr};"
                        f"\nmem_wdata = {val};\nmem_size = {size};")
            target_name = self._translate(target_var)
            value_code = self._translate(node.value)
            if target_name == "pc":
                return f"pc_we = 1'b1;\npc_next = {value_code};"
            return f"{target_name} = {value_code};"

        if isinstance(node, ast.AugAssign):
            target_name = self._translate(node.target)
            value_code = self._translate(node.value)
            op = BehaviorIR.OPERATORS.get(type(node.op))
            return f"{target_name} {op}= {value_code};"

        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in BehaviorIR.MEM_KEYWORDS:
                mem_width = BehaviorIR.MEM_KEYWORDS[node.value.id]
                addr = self._translate(node.slice)
                size = {8: "3'b000", 16: "3'b001", 32: "3'b010", 64: "3'b011"}.get(mem_width, "3'b000")
                self._mem_reads.append(
                    f"mem_req = 1'b1;\nmem_we = 1'b0;\nmem_addr = {addr};\nmem_size = {size};"
                )
                return "mem_rdata"
            var = self._translate(node.value)
            if isinstance(node.slice, ast.Slice):
                l = node.slice.lower.value if node.slice.lower else 0
                u = node.slice.upper.value if node.slice.upper else ir.get_width(node.value)
                return f"{var}[{u-1}:{l}]"

        if isinstance(node, ast.Set):
            args = [self._translate(elt) for elt in node.elts]
            return "{" + ", ".join(args) + "}"

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func_id = node.func.id
            if func_id == "sext":
                inner = self._translate(node.args[0])
                n = node.args[1].value
                return f"$signed({n}'({inner}))"
            if func_id == "signed":
                inner = self._translate(node.args[0])
                return f"$signed({inner})"
            if func_id == "zext":
                # Zero-extension is implicit in SystemVerilog width contexts;
                # $unsigned() marks the intent and neutralizes a $signed() inner.
                inner = self._translate(node.args[0])
                return f"$unsigned({inner})"
            if func_id in ir.operands:
                args = [self._translate(arg) for arg in node.args]
                return "{" + ", ".join(reversed(args)) + "}"

        if isinstance(node, ast.Name):
            if node.id in ir.register_map:
                return f"{node.id}_val"
            return node.id

        raise ValueError(f"Unsupported syntax in behavior: '{ast.unparse(node)}'")
