import ast
from typing import TYPE_CHECKING, Dict, Set, Optional, Tuple

if TYPE_CHECKING:
    from ..models import Operand, CSR


class BehaviorIR:
    """Parses a behavior string and extracts analysis results (variables, widths, flags)."""

    KNOWN_BUILTINS = frozenset({"sext", "signed", "zext"})

    OPERATORS = {
        ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
        ast.Mod: "%", ast.LShift: "<<", ast.RShift: ">>",
        ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^",
        ast.Invert: "~",
    }
    CMP_OPS = {
        ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
        ast.Gt: ">", ast.GtE: ">="
    }
    BOOL_OPS = {ast.And: "&&", ast.Or: "||"}
    MEM_KEYWORDS = {"mem8": 8, "mem16": 16, "mem32": 32, "mem64": 64}

    def __init__(self, behavior_str: str,
                 register_map: Optional[Dict[str, str]] = None,
                 var_widths: Optional[Dict[str, int]] = None,
                 operands: Optional[Dict[str, "Operand"]] = None,
                 csrs: Optional[Dict[str, "CSR"]] = None):
        try:
            self.tree = ast.parse(behavior_str)
        except SyntaxError:
            raise ValueError(f"Invalid Python syntax in behavior: {behavior_str}")
        self.register_map = register_map or {}
        self.var_widths = var_widths or {}
        self.operands = operands or {}
        self.csrs = csrs or {}
        self.used_vars: Set[str] = set()
        self.read_vars: Set[str] = set()
        self.write_vars: Set[str] = set()
        self.modifies_pc = False
        self.is_unconditional_jump = False
        self.temporaries: Dict[str, Tuple[int, Optional[str]]] = {}
        self._analyze()
        self.is_unconditional_jump = self._detect_unconditional_pc_write(self.tree)

    def _analyze(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                if node.id not in self.operands and node.id != "range" and node.id not in self.MEM_KEYWORDS and node.id not in self.KNOWN_BUILTINS:
                    self.used_vars.add(node.id)
                    if isinstance(node.ctx, ast.Store):
                        self.write_vars.add(node.id)
                    elif isinstance(node.ctx, ast.Load):
                        self.read_vars.add(node.id)
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                name = node.target.id
                if name not in self.register_map and name != "pc" and name not in self.var_widths and name not in self.MEM_KEYWORDS:
                    self.temporaries[name] = (32, "_inline_")
                    self.var_widths[name] = 32
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        name = target.id
                        if name == "pc":
                            self.modifies_pc = True
                        elif name not in self.register_map and name not in self.var_widths and name not in self.MEM_KEYWORDS:
                            width = self.get_width(node.value)
                            type_name = None
                            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                                if node.value.func.id in self.operands:
                                    type_name = node.value.func.id
                            self.temporaries[name] = (width, type_name)
                            self.var_widths[name] = width

    def _detect_unconditional_pc_write(self, tree) -> bool:
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "pc" for t in stmt.targets):
                    return True
            if isinstance(stmt, ast.AugAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id == "pc":
                    return True
        return False

    def _get_field_info(self, type_name: str, attr_name: str) -> Tuple[int, Optional[str]]:
        fields = []
        if type_name in self.operands:
            fields = self.operands[type_name].spec.fields or []
        elif type_name in self.csrs:
            fields = self.csrs[type_name].spec.fields or []
        for f in fields:
            if f.name == attr_name:
                w = None
                if hasattr(f, 'width') and f.width is not None:
                    w = f.width
                elif hasattr(f, 'end') and f.end is not None and hasattr(f, 'start'):
                    w = f.end - f.start + 1
                if w is None:
                    raise ValueError(f"Type '{type_name}' field '{attr_name}' has no determinable width")
                t = f.type if hasattr(f, 'type') else None
                return w, t
        raise ValueError(f"Type '{type_name}' has no field '{attr_name}'")

    def get_width(self, node: ast.AST) -> int:
        if isinstance(node, ast.Name):
            if node.id in self.MEM_KEYWORDS: return self.MEM_KEYWORDS[node.id]
            if node.id in self.var_widths: return self.var_widths[node.id]
            if node.id in self.operands: return self.operands[node.id].spec.width
            raise ValueError(
                f"Cannot determine bit-width of '{node.id}' — "
                "is it declared in the schema, register state, or operands?"
            )
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                obj = node.value.id
                if obj in self.temporaries and self.temporaries[obj][1]:
                    w, _ = self._get_field_info(self.temporaries[obj][1], node.attr)
                    return w
                if obj in self.csrs:
                    w, _ = self._get_field_info(obj, node.attr)
                    return w
            raise ValueError(
                f"Cannot determine bit-width of '{ast.unparse(node)}' — "
                "check that the type has this field"
            )
        if isinstance(node, ast.Constant): return node.value.bit_length() or 1
        if isinstance(node, ast.BinOp): return max(self.get_width(node.left), self.get_width(node.right))
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in self.MEM_KEYWORDS:
                return self.MEM_KEYWORDS[node.value.id]
            if isinstance(node.slice, ast.Slice):
                l = node.slice.lower.value if node.slice.lower else 0
                u = node.slice.upper.value if node.slice.upper else self.get_width(node.value)
                return u - l
        if isinstance(node, ast.Set):
            return sum(self.get_width(elt) for elt in node.elts)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in self.operands:
                    return self.operands[node.func.id].spec.width
                if node.func.id == "sext":
                    return self.var_widths.get("pc", 32)
                if node.func.id == "zext":
                    return self.var_widths.get("pc", 32)
                if node.func.id == "signed":
                    return self.get_width(node.args[0])
        if isinstance(node, ast.Assign): return self.get_width(node.targets[0])
        if isinstance(node, ast.Compare): return 1
        if isinstance(node, ast.BoolOp): return 1
        if isinstance(node, ast.UnaryOp): return self.get_width(node.operand)
        raise ValueError(f"Cannot determine bit-width of expression '{ast.unparse(node)}'")


