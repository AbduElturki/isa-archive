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
    # System / trap primitives. `csr` is a reserved namespace (`csr.mstatus`,
    # `csr.mstatus.mie`); `trap(cause)` / `trap_return()` are builtin statements.
    TRAP_BUILTINS = frozenset({"trap", "trap_return"})
    CSR_NAMESPACE = "csr"

    def reg_element_access(self, node: ast.AST):
        """If `node` is an element index into a shaped register (`vd[i]`, `vd[i][j]`),
        return (field_name, regfile, element_ScalarType, shape, [index_nodes]); else None.
        Bit-slices (`rd[lo:hi]`) and memory (`memNN[addr]`) are not element accesses."""
        indices = []
        cur = node
        while isinstance(cur, ast.Subscript) and not isinstance(cur.slice, ast.Slice):
            indices.append(cur.slice)
            cur = cur.value
        if not indices or not isinstance(cur, ast.Name):
            return None
        name = cur.id
        if name in self.MEM_KEYWORDS:
            return None
        regfile = self.register_map.get(name)
        if regfile is None or regfile not in self.regfile_shapes:
            return None
        elem_st, shape = self.regfile_shapes[regfile]
        indices.reverse()  # outermost dimension first
        return (name, regfile, elem_st, shape, indices)

    def reg_attr_access(self, node: ast.AST):
        """If `node` is `reg.attr` where `reg` is a register operand whose file
        declares attribute `attr`, return (reg_operand, regfile, attr, width); else
        None. Distinct from `csr.x` (csr isn't a register operand) and Operand-struct
        temporaries (those aren't in register_map)."""
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            return None
        name = node.value.id
        regfile = self.register_map.get(name)
        if regfile is None or regfile not in self.regfile_attrs:
            return None
        attrs = self.regfile_attrs[regfile]
        if node.attr not in attrs:
            return None
        return (name, regfile, node.attr, attrs[node.attr])

    @staticmethod
    def csr_ref(node: ast.AST):
        """If `node` is `csr.NAME` or `csr.NAME.FIELD`, return (csr_name, field|None);
        otherwise None. Shared by the IR, the loader, and every backend so the
        `csr.` namespace is recognized in exactly one place."""
        if isinstance(node, ast.Attribute):
            v = node.value
            if isinstance(v, ast.Name) and v.id == BehaviorIR.CSR_NAMESPACE:
                return (node.attr, None)
            if (isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name)
                    and v.value.id == BehaviorIR.CSR_NAMESPACE):
                return (v.attr, node.attr)
        return None

    def __init__(self, behavior_str: str,
                 register_map: Optional[Dict[str, str]] = None,
                 var_widths: Optional[Dict[str, int]] = None,
                 operands: Optional[Dict[str, "Operand"]] = None,
                 csrs: Optional[Dict[str, "CSR"]] = None,
                 regfile_shapes: Optional[Dict[str, tuple]] = None,
                 regfile_attrs: Optional[Dict[str, Dict[str, int]]] = None):
        try:
            self.tree = ast.parse(behavior_str)
        except SyntaxError:
            raise ValueError(f"Invalid Python syntax in behavior: {behavior_str}")
        self.register_map = register_map or {}
        self.var_widths = var_widths or {}
        self.operands = operands or {}
        self.csrs = csrs or {}
        # {register-file name → (element ScalarType, shape list)} for shaped files.
        self.regfile_shapes = regfile_shapes or {}
        # {register-file name → {attr name → width}} for per-register attributes.
        self.regfile_attrs = regfile_attrs or {}
        self.attr_regs: Set[str] = set()   # register operands accessed via `.attr`
        self.unknown_reg_attrs: Set[tuple] = set()  # (reg, attr) accesses with no such attr
        self.uses_shaped_elem = False      # behavior indexes a shaped register element
        self.used_vars: Set[str] = set()
        self.read_vars: Set[str] = set()
        self.write_vars: Set[str] = set()
        self.modifies_pc = False
        self.is_unconditional_jump = False
        # System / trap analysis
        self.reads_csr = False
        self.writes_csr = False
        self.uses_trap = False
        self.csrs_used: Set[str] = set()       # CSR names referenced via csr.*
        self.trap_causes_used: list = []        # cause names passed to trap(...)
        self.temporaries: Dict[str, Tuple[int, Optional[str]]] = {}
        self._analyze()
        self.is_unconditional_jump = self._detect_unconditional_pc_write(self.tree)

    @property
    def uses_sys(self) -> bool:
        """True if the behavior touches a CSR, a trap primitive, or a register
        attribute — backends that can't model these (TCG fast path, LLVM ISel, RTL)
        use this to degrade gracefully instead of emitting wrong code."""
        return (self.reads_csr or self.writes_csr or self.uses_trap
                or bool(self.attr_regs))

    @property
    def uses_structured(self) -> bool:
        """True if the behavior touches a CSR/trap/attribute OR a shaped-register
        element. The RTL skeleton can't model any of these yet."""
        return self.uses_sys or self.uses_shaped_elem

    def _analyze(self):
        # Pre-pass: classify CSR references and trap primitives, and collect the
        # cause-argument names so the main pass doesn't mistake them for variables.
        for node in ast.walk(self.tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in self.TRAP_BUILTINS):
                self.uses_trap = True
                self.modifies_pc = True
                if node.func.id == "trap" and node.args and isinstance(node.args[0], ast.Name):
                    self.trap_causes_used.append(node.args[0].id)
            if self.reg_element_access(node) is not None:
                self.uses_shaped_elem = True
            cr = self.csr_ref(node)
            if cr is not None:
                self.csrs_used.add(cr[0])
                if isinstance(getattr(node, "ctx", None), ast.Store):
                    self.writes_csr = True
                else:
                    self.reads_csr = True
            ar = self.reg_attr_access(node)
            if ar is not None:
                # the register operand is accessed by index → force index passing
                self.attr_regs.add(ar[0])
                self.used_vars.add(ar[0])
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id in self.register_map):
                # `reg.something` where `something` is not a declared attribute
                self.unknown_reg_attrs.add((node.value.id, node.attr))

        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                if (node.id not in self.operands and node.id != "range"
                        and node.id not in self.MEM_KEYWORDS and node.id not in self.KNOWN_BUILTINS
                        and node.id != self.CSR_NAMESPACE and node.id not in self.TRAP_BUILTINS
                        and node.id not in self.trap_causes_used):
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
                    # Element-indexed write to a shaped register (`vd[i] = …`): mark the
                    # base register as written (its file is helper-only in QEMU, an
                    # output operand in LLVM).
                    acc = self.reg_element_access(target)
                    if acc is not None:
                        self.write_vars.add(acc[0])
                        self.used_vars.add(acc[0])
                        continue
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
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id in self.TRAP_BUILTINS):
                return True  # a top-level trap()/trap_return() redirects control
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
            fields = self.csrs[type_name].fields or []
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
            ar = self.reg_attr_access(node)
            if ar is not None:
                return ar[3]   # attribute width
            cr = self.csr_ref(node)
            if cr is not None:
                csr_name, field = cr
                if csr_name not in self.csrs:
                    raise ValueError(f"Behavior references unknown CSR 'csr.{csr_name}'")
                if field is None:
                    return self.csrs[csr_name].width
                w, _ = self._get_field_info(csr_name, field)
                return w
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
            acc = self.reg_element_access(node)
            if acc is not None:
                name, _regfile, elem_st, shape, indices = acc
                if len(indices) != len(shape):
                    raise ValueError(
                        f"'{name}' is a shaped register {shape}; index all "
                        f"{len(shape)} dimension(s) to reach an element "
                        f"(got {len(indices)})"
                    )
                return elem_st.width
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


