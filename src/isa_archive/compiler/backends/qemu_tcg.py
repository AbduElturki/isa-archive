import ast
from typing import Optional
from ..behavior import BehaviorIR


class QemuTCGBackend:
    def __init__(self, ir: BehaviorIR):
        self.ir = ir

    def translate(self, xlen: int = 32, float_regs: Optional[dict] = None,
                  tcg_regfiles: Optional[set] = None) -> Optional[str]:
        # The fast path emits unmasked full-width TCG ops, which is only correct
        # when the architectural xlen IS the guest word width. Narrow xlen
        # (8/16, emulated over a 32-bit guest word) needs the masking C helpers.
        if xlen not in (32, 64):
            return None
        suffix = "i64" if xlen == 64 else "i32"
        ir = self.ir
        float_regs = float_regs or {}
        if len(ir.tree.body) != 1 or not isinstance(ir.tree.body[0], ast.Assign):
            return None
        assign = ir.tree.body[0]
        if len(assign.targets) != 1 or not isinstance(assign.targets[0], ast.Name):
            return None
        target_var = assign.targets[0].id
        if target_var == "pc" or target_var not in ir.register_map:
            return None
        target_regfile = ir.register_map[target_var]
        # float_regs: {register-file name → ScalarType}. Float-destination ops are
        # not done with integer TCG; fall back to the (float-correct) C helper.
        if target_regfile in float_regs:
            return None
        # tcg_regfiles: files that have TCG globals of the xlen width. Every register
        # this statement touches must be one — otherwise the generated tcg_gen_*_iN
        # would operate at the wrong width (e.g. a 128-bit or 16-bit file).
        if tcg_regfiles is not None:
            touched_files = {ir.register_map[v] for v in ir.used_vars
                             if v in ir.register_map}
            if not touched_files <= tcg_regfiles:
                return None
        if not isinstance(assign.value, ast.BinOp):
            return None
        tcg_op = {
            ast.Add: "add", ast.Sub: "sub", ast.BitAnd: "and",
            ast.BitOr: "or", ast.BitXor: "xor", ast.LShift: "shl",
            ast.RShift: "shr"
        }.get(type(assign.value.op))
        if not tcg_op:
            return None

        def _tcg_arg(node):
            if isinstance(node, ast.Name):
                if node.id in ir.register_map:
                    return f"arch_{ir.register_map[node.id]}[a->{node.id}]"
                return f"tcg_constant_{suffix}(a->{node.id})"
            if isinstance(node, ast.Constant):
                return f"tcg_constant_{suffix}({node.value})"
            return None

        left = _tcg_arg(assign.value.left)
        right = _tcg_arg(assign.value.right)
        if left and right:
            return (f"tcg_gen_{tcg_op}_{suffix}("
                    f"arch_{target_regfile}[a->{target_var}], {left}, {right});")
        return None
