"""
LLVMDagBackend: translates BehaviorIR to LLVM SelectionDAG TableGen patterns.

Each instruction falls into one of four categories:
  - alu_rr   : rd = rs1 OP rs2          → (set GPR:$rd, (op GPR:$rs1, GPR:$rs2))
  - alu_ri   : rd = rs1 OP imm          → (set GPR:$rd, (op GPR:$rs1, simm12:$imm))
  - load     : rd = mem[rs1+imm]        → (set GPR:$rd, (loadX (add GPR:$rs1, simm12:$imm)))
  - store    : mem[rs1+imm] = rs2       → (store GPR:$rs2, (add GPR:$rs1, simm12:$imm))
  - branch   : if cond: pc = pc+imm     → BRCOND pattern (handled via ISelLowering)
  - jump_ind : pc = rs1+imm             → (brind (add GPR:$rs1, imm))
  - jump_abs : pc = imm                 → (br bb:$imm)
  - custom   : everything else          → usesCustomInserter hint

Returns None for the pattern string when custom lowering is needed.
"""
import ast
from dataclasses import dataclass, field
from typing import Optional
from ..behavior import BehaviorIR
from ...models.scalar_types import ArithClass


# Binary-operator → SelectionDAG node, keyed on (arithmetic class, AST op). This
# is the single dispatch table: the same `+` selects `add` for an integer
# destination and `fadd` for an IEEE-float destination, driven by the register
# file's arithmetic class instead of a separate float-only map.
_ARITH_BINOP_TO_DAG = {
    (ArithClass.INT, ast.Add):      "add",
    (ArithClass.INT, ast.Sub):      "sub",
    (ArithClass.INT, ast.BitAnd):   "and",
    (ArithClass.INT, ast.BitOr):    "or",
    (ArithClass.INT, ast.BitXor):   "xor",
    (ArithClass.INT, ast.LShift):   "shl",
    (ArithClass.INT, ast.RShift):   "srl",   # logical; signed right-shift mapped separately
    (ArithClass.INT, ast.Mult):     "mul",
    (ArithClass.INT, ast.FloorDiv): "sdiv",
    (ArithClass.INT, ast.Mod):      "srem",
    (ArithClass.IEEE_FLOAT, ast.Add):  "fadd",
    (ArithClass.IEEE_FLOAT, ast.Sub):  "fsub",
    (ArithClass.IEEE_FLOAT, ast.Mult): "fmul",
    (ArithClass.IEEE_FLOAT, ast.Div):  "fdiv",
}


@dataclass
class DagPattern:
    category: str                    # "alu_rr", "alu_ri", "load", "store", "branch",
                                     # "jump_ind", "jump_abs", "custom"
    dag: Optional[str] = None        # TableGen DAG string (None → custom)
    load_width: Optional[int] = None # for load/store: bit width
    load_signed: bool = False        # for load: sign-extend?
    op: Optional[str] = None         # for alu_rr/alu_ri: DAG op name (add, sub, and, …)
    is_float: bool = False           # operands/result are in a floating-point class
    addr_indexed: bool = False       # load/store uses base+register (indexed) addressing
    notes: list = field(default_factory=list)  # human-readable annotations


class LLVMDagBackend:
    def __init__(self, ir: BehaviorIR, xlen: int = 32, reg_class_info: Optional[dict] = None,
                 imm_operand_types: Optional[dict] = None,
                 combined_imm_type: Optional[str] = None):
        self.ir = ir
        self.xlen = xlen
        # reg_class_info: register-file name → {"class": <TableGen class>, "is_float": bool}
        self.reg_class_info = reg_class_info or {}
        self._reg_class = "GPR"  # fallback when a register's file is unknown
        # imm_operand_types: immediate field name → TableGen operand type
        # ("simm12", "uimm5", …), derived from the schema. combined_imm_type is
        # the operand for split-immediate schemas (referenced as `$imm`).
        self.imm_operand_types = imm_operand_types or {}
        self.combined_imm_type = combined_imm_type

    def _imm_type(self, name: str) -> str:
        """TableGen operand type for an immediate behavior variable."""
        return self.imm_operand_types.get(name, "simm12")

    def _class_of(self, name: str) -> str:
        """TableGen register class for a register/field name, via its register file."""
        f = self.ir.register_map.get(name)
        if f and f in self.reg_class_info:
            return self.reg_class_info[f]["class"]
        return f.upper() if f else self._reg_class

    def _is_float_reg(self, name: str) -> bool:
        f = self.ir.register_map.get(name)
        return bool(f and self.reg_class_info.get(f, {}).get("is_float"))

    def get_branch_condition(self) -> Optional[str]:
        """For conditional branch instructions, return the ISD setcc condition name.

        Returns a lowercase string like "seteq", "setlt", "setult" etc., or None
        if the condition cannot be determined.
        """
        if not self.ir.modifies_pc or self.ir.is_unconditional_jump:
            return None
        for stmt in self.ir.tree.body:
            if isinstance(stmt, ast.If):
                return self._cond_to_isd(stmt.test)
        return None

    def _cond_to_isd(self, node: ast.expr) -> Optional[str]:
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            return None
        op = node.ops[0]
        left, right = node.left, node.comparators[0]

        def _is_signed(n: ast.expr) -> bool:
            return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in ("signed", "sext"))

        is_signed = _is_signed(left) or _is_signed(right)

        if isinstance(op, ast.Eq):    return "seteq"
        if isinstance(op, ast.NotEq): return "setne"
        if isinstance(op, ast.Lt):    return "setlt"  if is_signed else "setult"
        if isinstance(op, ast.LtE):   return "setle"  if is_signed else "setule"
        if isinstance(op, ast.Gt):    return "setgt"  if is_signed else "setugt"
        if isinstance(op, ast.GtE):   return "setge"  if is_signed else "setuge"
        return None

    def _try_vector_elementwise(self) -> Optional[DagPattern]:
        """Recognize the canonical 1-D vector op
        `for i in range(N): vd[i] = vs1[i] OP vs2[i]` over a single shaped register
        file → a vector DAG `(set CLS:$vd, (OP CLS:$vs1, CLS:$vs2))`. Anything else
        shaped stays custom (simulator-only)."""
        ir = self.ir
        body = ir.tree.body
        if len(body) != 1 or not isinstance(body[0], ast.For):
            return None
        loop = body[0]
        if not (isinstance(loop.iter, ast.Call) and isinstance(loop.iter.func, ast.Name)
                and loop.iter.func.id == "range" and len(loop.body) == 1
                and isinstance(loop.body[0], ast.Assign)
                and isinstance(loop.target, ast.Name)):
            return None
        loopvar = loop.target.id
        assign = loop.body[0]
        if len(assign.targets) != 1 or not isinstance(assign.value, ast.BinOp):
            return None
        tgt = ir.reg_element_access(assign.targets[0])
        lhs = ir.reg_element_access(assign.value.left)
        rhs = ir.reg_element_access(assign.value.right)
        if tgt is None or lhs is None or rhs is None:
            return None
        if len({tgt[1], lhs[1], rhs[1]}) != 1:        # all the same register file
            return None
        for _n, _f, _e, shape, indices in (tgt, lhs, rhs):
            if len(shape) != 1 or len(indices) != 1:   # 1-D, single index
                return None
            if not (isinstance(indices[0], ast.Name) and indices[0].id == loopvar):
                return None
        # the loop must span exactly the lane count
        args = loop.iter.args
        n = args[-1].value if args and isinstance(args[-1], ast.Constant) else None
        if n != tgt[3][0]:
            return None
        dagop = _ARITH_BINOP_TO_DAG.get((tgt[2].arith_class, type(assign.value.op)))
        if dagop is None:
            return None
        cls = self._class_of(tgt[0])
        dag = (f"(set {cls}:${tgt[0]}, "
               f"({dagop} {cls}:${lhs[0]}, {cls}:${rhs[0]}))")
        return DagPattern(category="vector", dag=dag, op=dagop,
                          notes=[f"vector elementwise {dagop}"])

    def _contig_base(self, addr: ast.AST, loopvar: str, esize: int) -> Optional[str]:
        """If `addr` is `base + loopvar*esize` (or `base + loopvar` when esize==1)
        with `base` a scalar register, return base's name — i.e. a contiguous,
        unit-stride address over the loop. Else None."""
        if not (isinstance(addr, ast.BinOp) and isinstance(addr.op, ast.Add)):
            return None
        base, off = addr.left, addr.right
        if not (isinstance(base, ast.Name) and base.id in self.ir.register_map
                and self.ir.register_map[base.id] not in self.ir.regfile_shapes):
            return None
        if esize == 1 and isinstance(off, ast.Name) and off.id == loopvar:
            return base.id
        if isinstance(off, ast.BinOp) and isinstance(off.op, ast.Mult):
            names = {x.id for x in (off.left, off.right) if isinstance(x, ast.Name)}
            consts = [x.value for x in (off.left, off.right) if isinstance(x, ast.Constant)]
            if loopvar in names and esize in consts:
                return base.id
        return None

    def _try_vector_mem(self) -> Optional[DagPattern]:
        """Contiguous vector load/store over a 1-D vector file:
          load:  `for i in range(N): vd[i]  = memW[base + i*esize]`
          store: `for i in range(N): memW[base + i*esize] = vs[i]`
        → `(set VEC:$vd, (load GPR:$base))` / `(store VEC:$vs, GPR:$base)`. These
        ride on the LOAD/STORE legality the backend already emits for the vector MVT."""
        ir = self.ir
        body = ir.tree.body
        if len(body) != 1 or not isinstance(body[0], ast.For):
            return None
        loop = body[0]
        if not (isinstance(loop.iter, ast.Call) and isinstance(loop.iter.func, ast.Name)
                and loop.iter.func.id == "range" and isinstance(loop.target, ast.Name)
                and len(loop.body) == 1 and isinstance(loop.body[0], ast.Assign)
                and len(loop.body[0].targets) == 1):
            return None
        loopvar = loop.target.id
        args = loop.iter.args
        n = args[-1].value if args and isinstance(args[-1], ast.Constant) else None
        tgt, val = loop.body[0].targets[0], loop.body[0].value

        def _vec_lane(acc):
            name, _f, elem, shape, idx = acc
            ok = (len(shape) == 1 and len(idx) == 1 and n == shape[0]
                  and isinstance(idx[0], ast.Name) and idx[0].id == loopvar)
            return (name, elem) if ok else (None, None)

        def _is_mem(node):
            return (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id in ir.MEM_KEYWORDS)

        # load: vd[i] = memW[base + i*esize]
        lane = ir.reg_element_access(tgt)
        if lane is not None and _is_mem(val):
            name, elem = _vec_lane(lane)
            if name and ir.MEM_KEYWORDS[val.value.id] == elem.width:
                base = self._contig_base(val.slice, loopvar, elem.width // 8)
                if base:
                    return DagPattern(category="vector_load", notes=["contiguous vector load"],
                                      dag=f"(set {self._class_of(name)}:${name}, "
                                          f"(load {self._class_of(base)}:${base}))")
        # store: memW[base + i*esize] = vs[i]
        lane = ir.reg_element_access(val)
        if _is_mem(tgt) and lane is not None:
            name, elem = _vec_lane(lane)
            if name and ir.MEM_KEYWORDS[tgt.value.id] == elem.width:
                base = self._contig_base(tgt.slice, loopvar, elem.width // 8)
                if base:
                    return DagPattern(category="vector_store", notes=["contiguous vector store"],
                                      dag=f"(store {self._class_of(name)}:${name}, "
                                          f"{self._class_of(base)}:${base})")
        return None

    def translate(self) -> DagPattern:
        """Return a DagPattern describing the SelectionDAG representation."""
        ir = self.ir

        # Canonical 1-D vector elementwise op → a vector DAG pattern.
        vec = self._try_vector_elementwise()
        if vec is not None:
            return vec

        # Contiguous vector load / store → a vector load/store pattern.
        vmem = self._try_vector_mem()
        if vmem is not None:
            return vmem

        # CSR / trap / system instructions aren't compiler-selected (they're used
        # via inline asm / intrinsics); lower them as custom so the coverage
        # report lists them instead of attempting a pattern.
        if ir.uses_sys:
            return DagPattern(category="custom",
                              notes=["CSR / system instruction — custom lowering"])

        # Unconditional jump to register+imm  (JALR pattern)
        if ir.modifies_pc and ir.is_unconditional_jump:
            return self._try_jump()

        # Conditional branch
        if ir.modifies_pc and not ir.is_unconditional_jump:
            return DagPattern(category="branch", dag=None,
                              notes=["conditional branch — TableGen Pat<> pattern"])

        # Memory store: Assign where target is a Subscript (mem32[...] = rs2)
        if (len(ir.tree.body) == 1 and isinstance(ir.tree.body[0], ast.Assign)
                and isinstance(ir.tree.body[0].targets[0], ast.Subscript)):
            return self._try_store(ir.tree.body[0])

        # Single assignment to a named destination
        if len(ir.tree.body) == 1 and isinstance(ir.tree.body[0], ast.Assign):
            return self._try_assign(ir.tree.body[0])

        # SLT-style: if cond: rd=1 else: rd=0
        if len(ir.tree.body) == 1 and isinstance(ir.tree.body[0], ast.If):
            return self._try_slt(ir.tree.body[0])

        return DagPattern(category="custom", notes=["multi-statement behavior"])

    # ── helpers ───────────────────────────────────────────────────────────────

    def _try_jump(self) -> DagPattern:
        ir = self.ir
        # Look for "pc = rs1 + imm" (jump_ind) or "pc = pc + imm" (jump_abs)
        for stmt in ir.tree.body:
            if not isinstance(stmt, ast.Assign):
                continue
            if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "pc"):
                continue
            val = stmt.value
            if isinstance(val, ast.BinOp) and isinstance(val.op, ast.Add):
                lname = val.left.id if isinstance(val.left, ast.Name) else None
                rname = val.right.id if isinstance(val.right, ast.Name) else None
                if lname and lname in ir.register_map:
                    imm_arg = rname or "imm"
                    return DagPattern(
                        category="jump_ind",
                        dag=f"(brind (add {self._class_of(lname)}:${lname}, "
                            f"{self._imm_type(imm_arg)}:${imm_arg}))",
                    )
                # "pc = pc + imm" — PC-relative unconditional jump (JAL-style)
                # No simple TableGen pattern; handled via ISD::BR in ISelDAGToDAG.
                if lname == "pc":
                    return DagPattern(
                        category="jump_abs",
                        dag=None,
                        notes=["PC-relative jump; lowered via ISD::BR in ISelDAGToDAG"],
                    )
            if isinstance(val, ast.Name) and val.id in ir.register_map:
                return DagPattern(
                    category="jump_ind",
                    dag=f"(brind {self._class_of(val.id)}:${val.id})",
                )
        return DagPattern(category="custom", notes=["complex jump"])

    def _try_assign(self, stmt: ast.Assign) -> DagPattern:
        ir = self.ir
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            return DagPattern(category="custom", notes=["non-name target"])

        dest = stmt.targets[0].id
        val = stmt.value

        # Peel an optional sext()/signed()/zext()/unsigned() wrapper around the RHS.
        # `wrap_signed` is True for sign-extend, False for zero-extend, None if absent.
        wrap_signed: Optional[bool] = None
        core = val
        if (isinstance(val, ast.Call) and isinstance(val.func, ast.Name)
                and val.func.id in ("sext", "signed", "zext", "unsigned")
                and len(val.args) >= 1):
            wrap_signed = val.func.id in ("sext", "signed")
            core = val.args[0]

        dest_cls = self._class_of(dest)
        dest_is_float = self._is_float_reg(dest)

        # Memory load: rd = [sext|zext](mem8/16/32/64[rs1 + imm])
        if isinstance(core, ast.Subscript) and isinstance(core.value, ast.Name):
            width = {"mem8": 8, "mem16": 16, "mem32": 32, "mem64": 64}.get(core.value.id)
            if width:
                addr_dag, _, indexed = self._parse_addr(core.slice)
                if dest_is_float:
                    # Floating-point load: a plain typed load into the FP class.
                    return DagPattern(
                        category="load", load_width=width, load_signed=False, is_float=True,
                        addr_indexed=indexed,
                        dag=f"(set {dest_cls}:${dest}, (load {addr_dag}))",
                    )
                if width == self.xlen:
                    # Full-width: a plain non-extending load. (zextloadiN with
                    # N == the result width never matches — natural loads are
                    # NON_EXTLOAD, so LW-style instructions would be unselectable.)
                    signed, ld = False, "load"
                elif width > self.xlen:
                    return DagPattern(
                        category="custom",
                        notes=[f"load width {width} exceeds data width {self.xlen}"],
                    )
                else:
                    signed = bool(wrap_signed)                # default zero-extend
                    ld = f"sextloadi{width}" if signed else f"zextloadi{width}"
                return DagPattern(
                    category="load",
                    load_width=width,
                    load_signed=signed,
                    addr_indexed=indexed,
                    dag=f"(set {dest_cls}:${dest}, ({ld} {addr_dag}))",
                )

        inner = core

        # rd = rs1 OP rs2   (register-register), or rs1 OP imm (register-immediate).
        # A shift amount written as a register slice (rs2[0:5]) still denotes a
        # register operand, so it classifies as alu_rr.
        if isinstance(inner, ast.BinOp):
            arith = ArithClass.IEEE_FLOAT if dest_is_float else ArithClass.INT
            dag_op = _ARITH_BINOP_TO_DAG.get((arith, type(inner.op)))
            if dag_op:
                # Arithmetic vs logical right shift: signed(rs1) >> x → sra.
                if isinstance(inner.op, ast.RShift) and self._is_signed_wrapped(inner.left):
                    dag_op = "sra"
                left = self._node(inner.left)
                right = self._node(inner.right)
                # A usable ALU pattern has a register left operand (rd = rs1 OP x).
                # When the left side is an immediate/PC (e.g. LUI's imm<<12, AUIPC),
                # it is constant/address materialization, not an ALU op → custom.
                if left and right and self._is_reg_or_slice(inner.left):
                    is_rr = self._is_reg_or_slice(inner.right)
                    return DagPattern(
                        # float-ness is carried by is_float; the category is the
                        # same alu_rr/alu_ri the templates consume.
                        category="alu_rr" if is_rr else "alu_ri",
                        op=dag_op,
                        is_float=dest_is_float,
                        dag=f"(set {dest_cls}:${dest}, ({dag_op} {left}, {right}))",
                    )

        # rd = rs1  (copy / move)
        if isinstance(inner, ast.Name) and inner.id in ir.register_map:
            return DagPattern(
                category="alu_rr",
                op="copy",
                dag=f"(set {dest_cls}:${dest}, {self._class_of(inner.id)}:${inner.id})",
            )

        # rd = imm  (load immediate — no matching pattern; use ISelLowering)
        if isinstance(inner, ast.Constant):
            return DagPattern(category="custom", notes=["load-immediate — ISelLowering"])

        return DagPattern(category="custom", notes=["complex expression"])

    def _try_store(self, stmt: ast.Assign) -> DagPattern:
        """Handle mem32[rs1 + imm] = rs2 → (store GPR:$rs2, (add GPR:$rs1, simm12:$imm))."""
        target = stmt.targets[0]
        if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
            return DagPattern(category="custom", notes=["non-subscript store"])
        width = {"mem8": 8, "mem16": 16, "mem32": 32, "mem64": 64}.get(target.value.id)
        if not width:
            return DagPattern(category="custom", notes=["unknown mem type in store"])
        addr_dag, _, indexed = self._parse_addr(target.slice)
        val_dag = self._node(stmt.value)
        if val_dag is None:
            return DagPattern(category="custom", notes=["complex store value"])
        val_is_float = (isinstance(stmt.value, ast.Name)
                        and self._is_float_reg(stmt.value.id))
        if val_is_float:
            return DagPattern(category="store", load_width=width, is_float=True,
                              addr_indexed=indexed,
                              dag=f"(store {val_dag}, {addr_dag})")
        if width > self.xlen:
            return DagPattern(
                category="custom",
                notes=[f"store width {width} exceeds data width {self.xlen}"],
            )
        store_op = "store" if width == self.xlen else f"truncstorei{width}"
        return DagPattern(
            category="store",
            load_width=width,
            addr_indexed=indexed,
            dag=f"({store_op} {val_dag}, {addr_dag})",
        )

    def _try_slt(self, stmt: ast.If) -> DagPattern:
        """Handle if cond: rd=1 else: rd=0 → (set GPR:$rd, (setlt GPR:$rs1, GPR:$rs2))."""
        body, orelse = stmt.body, stmt.orelse
        if len(body) != 1 or len(orelse) != 1:
            return DagPattern(category="custom", notes=["complex if/else"])
        if not (isinstance(body[0], ast.Assign) and isinstance(orelse[0], ast.Assign)):
            return DagPattern(category="custom", notes=["non-assign in if/else"])
        dest_t = body[0].targets[0]
        dest_f = orelse[0].targets[0]
        if not (isinstance(dest_t, ast.Name) and isinstance(dest_f, ast.Name)
                and dest_t.id == dest_f.id):
            return DagPattern(category="custom", notes=["dest mismatch in if/else"])
        vt = body[0].value
        vf = orelse[0].value
        if not (isinstance(vt, ast.Constant) and vt.value == 1
                and isinstance(vf, ast.Constant) and vf.value == 0):
            return DagPattern(category="custom", notes=["non-1/0 if/else"])
        cond_str = self._cond_to_isd(stmt.test)
        if not cond_str:
            return DagPattern(category="custom", notes=["unrecognized condition"])
        if isinstance(stmt.test, ast.Compare):
            left = self._node(stmt.test.left)
            right = self._node(stmt.test.comparators[0])
            if left and right:
                # set-less-than family: register-register (SLT) vs register-immediate (SLTI).
                rr = self._is_reg_or_slice(stmt.test.comparators[0])
                return DagPattern(
                    category="alu_rr" if rr else "alu_ri",
                    op=cond_str,   # e.g. "setlt"/"setult" → drives cmp.* role inference
                    dag=f"(set {self._class_of(dest_t.id)}:${dest_t.id}, ({cond_str} {left}, {right}))",
                )
        return DagPattern(category="custom", notes=["cannot build slt pattern"])

    def _node(self, node: ast.expr) -> Optional[str]:
        ir = self.ir
        # Unwrap sext(x) / signed(x) / zext(x) wrappers
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("sext", "signed", "zext", "unsigned")
                and len(node.args) == 1):
            return self._node(node.args[0])
        if isinstance(node, ast.Name):
            if node.id in ir.register_map:
                return f"{self._class_of(node.id)}:${node.id}"
            return f"{self._imm_type(node.id)}:${node.id}"  # immediate operand
        if isinstance(node, ast.Constant):
            return f"(i{self.xlen} {node.value})"
        # Register slice rs2[lo:hi] — use base register (hardware takes lower bits)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base in ir.register_map:
                return f"{self._class_of(base)}:${base}"
        return None

    def _is_reg(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.ir.register_map
        return False

    def _is_reg_or_slice(self, node: ast.expr) -> bool:
        """True if node is a register or a slice of one (e.g. rs2[0:5] shift amount),
        peeling any sext()/zext()/signed()/unsigned() wrapper first."""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("sext", "signed", "zext", "unsigned")
                and len(node.args) >= 1):
            node = node.args[0]
        if self._is_reg(node):
            return True
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            return node.value.id in self.ir.register_map
        return False

    def _is_signed_wrapped(self, node: ast.expr) -> bool:
        """True if node is signed()/sext()-wrapped (marks an arithmetic right shift)."""
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("sext", "signed"))

    def _parse_addr(self, node: ast.expr) -> tuple[str, bool, bool]:
        """Parse a memory subscript → (DAG address node, sign-extend hint, indexed?).

        `indexed` is True for base+register addressing (mem[rs1 + rs2]); False for
        base+immediate and bare-register addressing.
        """
        ir = self.ir
        signed = False
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            base = node.left.id if isinstance(node.left, ast.Name) else "rs1"
            base_cls = self._class_of(base)
            rhs = node.right
            # {imm_11_5, imm_4_0} → combined $imm (S-type split immediate)
            if isinstance(rhs, ast.Set):
                ctype = self.combined_imm_type or self._imm_type("imm")
                return f"(add {base_cls}:${base}, {ctype}:$imm)", signed, False
            # base + index register → indexed addressing
            if isinstance(rhs, ast.Name) and rhs.id in ir.register_map:
                idx_cls = self._class_of(rhs.id)
                return f"(add {base_cls}:${base}, {idx_cls}:${rhs.id})", signed, True
            imm = rhs.id if isinstance(rhs, ast.Name) else "imm"
            return f"(add {base_cls}:${base}, {self._imm_type(imm)}:${imm})", signed, False
        if isinstance(node, ast.Name):
            if node.id in ir.register_map:
                return f"{self._class_of(node.id)}:${node.id}", signed, False
            return f"{self._imm_type(node.id)}:${node.id}", signed, False
        return f"(add {self._reg_class}:$rs1, {self._imm_type('imm')}:$imm)", signed, False
