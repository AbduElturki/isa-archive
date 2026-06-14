import ast
from typing import Optional
from ..behavior import BehaviorIR


class _BackendBase:
    """Shared translation for pure expression nodes identical across all targets."""

    def __init__(self, ir: BehaviorIR):
        self.ir = ir

    def _translate(self, node: ast.AST, state_prefix: Optional[str] = None) -> str:
        if isinstance(node, ast.Expr):
            return self._translate(node.value, state_prefix)
        if isinstance(node, ast.Compare):
            left = self._translate(node.left, state_prefix)
            op = BehaviorIR.CMP_OPS.get(type(node.ops[0]))
            right = self._translate(node.comparators[0], state_prefix)
            return f"({left} {op} {right})"
        if isinstance(node, ast.BoolOp):
            op = BehaviorIR.BOOL_OPS.get(type(node.op))
            values = [self._translate(v, state_prefix) for v in node.values]
            return f"({f' {op} '.join(values)})"
        if isinstance(node, ast.UnaryOp):
            val = self._translate(node.operand, state_prefix)
            if isinstance(node.op, ast.Invert): return f"(~{val})"
            if isinstance(node.op, ast.Not): return f"(!{val})"
            if isinstance(node.op, ast.USub): return f"(-{val})"
        if isinstance(node, ast.BinOp):
            left = self._translate(node.left, state_prefix)
            right = self._translate(node.right, state_prefix)
            op = BehaviorIR.OPERATORS.get(type(node.op))
            return f"({left} {op} {right})"
        if isinstance(node, ast.Attribute):
            return f"{self._translate(node.value, state_prefix)}.{node.attr}"
        if isinstance(node, ast.Constant):
            return str(node.value)
        return self._translate_complex(node, state_prefix)

    def _translate_complex(self, node: ast.AST, state_prefix: Optional[str] = None) -> str:
        raise ValueError(f"Unsupported syntax in behavior: '{ast.unparse(node)}'")
