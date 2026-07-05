"""Expand `kind: BehaviorFunc` calls inside a behavior AST by inlining.

Run once, inside `BehaviorIR.__init__`, right after `ast.parse` and before
`_analyze`. Every backend then lowers the already-expanded tree - the function's
locals become ordinary temporaries the backends already declare, so no backend
needs to know functions exist.

v1 rules (validated in the loader too): a call must be the whole right-hand side
of an assignment (`x = f(a)`) or a bare statement (`f(a)`); a value-returning
function has exactly one tail `return`; editable args bind to caller lvalues,
operand-typed args bind to a caller variable name; recursion and nested calls
(`f(a) + 1`, `f(g(a))`) are rejected.
"""
import ast
import copy
from typing import Dict, List, Tuple


def _is_func_call(node: ast.AST, funcs: Dict) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in funcs)


def _is_lvalue(node: ast.AST) -> bool:
    # Assignable forms: a variable, `reg.attr`, or `vd[i]`.
    return isinstance(node, (ast.Name, ast.Attribute, ast.Subscript))


class _Renamer(ast.NodeTransformer):
    """Rename a set of local names to their gensym'd forms (all contexts)."""
    def __init__(self, mapping: Dict[str, str]):
        self.mapping = mapping

    def visit_Name(self, node: ast.Name):
        if node.id in self.mapping:
            return ast.Name(id=self.mapping[node.id], ctx=node.ctx)
        return node


class _Substituter(ast.NodeTransformer):
    """Replace parameter Names with their bound argument nodes (fresh copy each
    occurrence; the occurrence's context is preserved for Store targets)."""
    def __init__(self, subst: Dict[str, ast.AST]):
        self.subst = subst

    def visit_Name(self, node: ast.Name):
        repl = self.subst.get(node.id)
        if repl is None:
            return node
        new = copy.deepcopy(repl)
        if hasattr(new, "ctx"):
            new.ctx = node.ctx
        return new


class _Inliner:
    def __init__(self, funcs: Dict, operands: Dict):
        self.funcs = funcs
        self.operands = operands
        self.warnings: List[str] = []
        self._n = 0                       # gensym counter
        self._parsed: Dict[str, ast.Module] = {}

    # -- helpers --------------------------------------------------------------

    def _body_ast(self, fname: str) -> ast.Module:
        if fname not in self._parsed:
            self._parsed[fname] = ast.parse(self.funcs[fname].spec.behavior)
        return copy.deepcopy(self._parsed[fname])

    def _forbid_call(self, node: ast.AST):
        for n in ast.walk(node):
            if _is_func_call(n, self.funcs):
                raise ValueError(
                    "a BehaviorFunc call must be the whole right-hand side of an "
                    "assignment or a bare statement (nested calls are not supported)")

    def _assigned_names(self, body: List[ast.stmt]) -> set:
        names = set()
        for stmt in body:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                    names.add(n.target.id)
                elif isinstance(n, ast.For) and isinstance(n.target, ast.Name):
                    names.add(n.target.id)
        return names

    # -- inlining -------------------------------------------------------------

    def _inline_call(self, call: ast.Call, stack: Tuple[str, ...]
                     ) -> Tuple[List[ast.stmt], ast.expr]:
        fname = call.func.id
        if fname in stack:
            raise ValueError(f"BehaviorFunc '{fname}' is recursive; not supported")
        fdef = self.funcs[fname]
        params = fdef.spec.args
        if len(call.args) != len(params):
            raise ValueError(f"BehaviorFunc '{fname}' expects {len(params)} arg(s), "
                             f"got {len(call.args)}")

        assigned = self._assigned_names(self._parsed_body(fname))
        param_names = {p.name for p in params}
        self._n += 1
        uid = self._n

        subst: Dict[str, ast.AST] = {}   # param → caller node (substituted in place)
        rename: Dict[str, str] = {}      # local / by-value param → gensym'd name
        inits: List[ast.stmt] = []       # `local = arg` for a written read-only param
        for param, arg in zip(params, call.args):
            if param.editable:
                # out/inout: the caller lvalue is used for reads AND writes.
                if not _is_lvalue(arg):
                    raise ValueError(f"BehaviorFunc '{fname}' arg '{param.name}' is "
                                     f"editable and needs an lvalue (a register, "
                                     f"reg.attr, or vd[i]); got '{ast.unparse(arg)}'")
                subst[param.name] = arg
            elif param.type in self.operands:
                # operand-typed: bind Name→Name so `param.field` resolves; the
                # validator already ensured a read-only operand arg isn't written.
                if not isinstance(arg, ast.Name):
                    raise ValueError(f"BehaviorFunc '{fname}' operand-typed arg "
                                     f"'{param.name}' needs a variable, got "
                                     f"'{ast.unparse(arg)}'")
                subst[param.name] = arg
            else:
                # read-only scalar: pass-by-value. If the body reassigns it, copy
                # the arg into a fresh local (writes don't reach the caller);
                # otherwise substitute the arg expression directly.
                self._forbid_call(arg)
                if param.name in assigned:
                    local = f"_{fname}_{param.name}_{uid}"
                    rename[param.name] = local
                    inits.append(ast.Assign(targets=[ast.Name(id=local, ctx=ast.Store())],
                                            value=copy.deepcopy(arg)))
                else:
                    subst[param.name] = arg

        for loc in (assigned - param_names):     # genuine locals
            rename[loc] = f"_{fname}_{loc}_{uid}"

        body = self._body_ast(fname).body
        body = [_Renamer(rename).visit(s) for s in body]
        body = [_Substituter(subst).visit(s) for s in body]
        body = self._expand_block(body, stack + (fname,))  # nested calls

        ret_expr = None
        if fdef.spec.returns is not None:
            ret_expr = body[-1].value
            body = body[:-1]
        return inits + body, ret_expr

    def _parsed_body(self, fname: str) -> List[ast.stmt]:
        if fname not in self._parsed:
            self._parsed[fname] = ast.parse(self.funcs[fname].spec.behavior)
        return self._parsed[fname].body

    def _expand_stmt(self, stmt: ast.stmt, stack: Tuple[str, ...]) -> List[ast.stmt]:
        if isinstance(stmt, ast.If):
            stmt.body = self._expand_block(stmt.body, stack)
            stmt.orelse = self._expand_block(stmt.orelse, stack)
            self._forbid_call(stmt.test)
            return [stmt]
        if isinstance(stmt, ast.For):
            stmt.body = self._expand_block(stmt.body, stack)
            self._forbid_call(stmt.iter)
            return [stmt]
        if isinstance(stmt, ast.Expr) and _is_func_call(stmt.value, self.funcs):
            fname = stmt.value.func.id
            pre, _ = self._inline_call(stmt.value, stack)
            if self.funcs[fname].spec.returns is not None:
                self.warnings.append(
                    f"result of BehaviorFunc '{fname}' is not used")
            return pre
        if isinstance(stmt, ast.Assign) and _is_func_call(stmt.value, self.funcs):
            fname = stmt.value.func.id
            if self.funcs[fname].spec.returns is None:
                raise ValueError(f"BehaviorFunc '{fname}' returns nothing and can't be "
                                 f"used as a value")
            pre, ret = self._inline_call(stmt.value, stack)
            return pre + [ast.Assign(targets=stmt.targets, value=ret)]
        self._forbid_call(stmt)
        return [stmt]

    def _expand_block(self, stmts: List[ast.stmt], stack: Tuple[str, ...]
                      ) -> List[ast.stmt]:
        out: List[ast.stmt] = []
        for s in stmts:
            out.extend(self._expand_stmt(s, stack))
        return out


def inline_behavior_funcs(tree: ast.Module, behavior_funcs: Dict,
                          operands: Dict) -> Tuple[ast.Module, List[str]]:
    """Return (expanded tree, warnings). A no-op (same tree) when no BehaviorFunc
    is declared or none is called."""
    if not behavior_funcs:
        return tree, []
    inliner = _Inliner(behavior_funcs, operands or {})
    new_body = inliner._expand_block(tree.body, ())
    new_tree = ast.Module(body=new_body, type_ignores=[])
    ast.fix_missing_locations(new_tree)
    return new_tree, inliner.warnings
