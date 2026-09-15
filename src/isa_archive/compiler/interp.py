"""Reference interpreter for the behavior DSL.

Executes an ISA's `behavior:` definitions directly on a Python machine state, so
the semantics a manifest declares can be run without building any generated
backend. It is the oracle the generated QEMU / RTL / compiler outputs are
measured against: every operator follows the same rules the C backend lowers to
(unsigned by default, `signed()` opts in, C integer promotion and wrap-around,
truncating division), immediates arrive exactly as decodetree hands them to the
QEMU helpers (signed fields sign-extended, split fields raw), and the PC
advances the way the generated helpers advance it.

Three layers:

* :class:`MachineState` - registers, CSRs, per-register attributes, PC, a
  sparse byte-addressed memory, and optional memory-mapped I/O handlers.
* :class:`Interpreter` - per-ISA: decodes an instruction word against every
  schema pattern, and executes one instruction's behavior on a state.
* :func:`run_program` - fetch/decode/execute loop with a step limit and a
  halt hook (used by the ``isa-archive run`` CLI).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .behavior import BehaviorIR
from .utils import (build_reg_maps, build_regfile_attrs, build_regfile_shapes,
                    csr_map, instruction_pattern)
from ..models.enums import FieldRole


class InterpError(Exception):
    """A behavior could not be executed (unsupported construct, bad access)."""


class HaltRequested(Exception):
    """Raised by an I/O handler to stop :func:`run_program` (e.g. a test/exit
    device). ``code`` is the exit status to report."""

    def __init__(self, code: int = 0):
        super().__init__(f"halt({code})")
        self.code = code


# ── Integer helpers ───────────────────────────────────────────────────────────

def _mask(v: int, w: int) -> int:
    return v & ((1 << w) - 1)


def _to_signed(v: int, w: int) -> int:
    v = _mask(v, w)
    return v - (1 << w) if v >> (w - 1) else v


def _c_width(w: int) -> int:
    """Width of the C type the QEMU backend computes in: standard integer
    promotion (at least 32 bits), rounded up to a real integer type."""
    for b in (32, 64, 128):
        if w <= b:
            return b
    raise InterpError(f"no host integer type holds {w} bits")


def _trunc_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


@dataclass
class _Val:
    """An evaluated expression: a Python int plus whether it is currently
    signed (i.e. whether the C expression would have a signed type)."""
    v: int
    signed: bool = False


# ── Machine state ─────────────────────────────────────────────────────────────

IOHandler = Callable[[str, int, int, int], Optional[int]]
"""``handler(op, addr, width_bits, value) -> int | None``; ``op`` is ``"load"``
(return the value) or ``"store"`` (``value`` is the data; return ignored)."""


@dataclass
class MachineState:
    xlen: int
    byte_order: str = "little"
    pc: int = 0
    regs: Dict[str, list] = field(default_factory=dict)      # file → [value|elements]
    reg_widths: Dict[str, int] = field(default_factory=dict)  # file → bits
    attrs: Dict[str, Dict[str, list]] = field(default_factory=dict)  # file → attr → [v]
    csrs: Dict[str, int] = field(default_factory=dict)
    mem: Dict[int, int] = field(default_factory=dict)         # byte address → byte
    io: List[Tuple[int, int, IOHandler]] = field(default_factory=list)  # (base, size, fn)
    trace: List[str] = field(default_factory=list)

    @classmethod
    def for_isa(cls, isa_reg) -> "MachineState":
        spec = isa_reg.manifest.spec
        st = cls(xlen=isa_reg.xlen, byte_order=spec.byte_order)
        for r in isa_reg.registers:
            if r.is_shaped:
                st.regs[r.name] = [_zeros(list(r.shape)) for _ in range(r.count)]
            else:
                st.regs[r.name] = [0] * r.count
            st.reg_widths[r.name] = r.width
            if r.attributes:
                st.attrs[r.name] = {a.name: [0] * r.count for a in r.attributes}
        for c in isa_reg.arch_csrs:
            st.csrs[c.name] = c.reset_value
        st.pc = isa_reg.machine.effective_reset_vector()
        return st

    # -- memory --
    def _io_for(self, addr: int):
        for base, size, fn in self.io:
            if base <= addr < base + size:
                return fn
        return None

    def load(self, addr: int, width: int) -> int:
        addr = _mask(addr, self.xlen)
        fn = self._io_for(addr)
        if fn is not None:
            return _mask(fn("load", addr, width, 0) or 0, width)
        n = width // 8
        bs = bytes(self.mem.get(addr + i, 0) for i in range(n))
        return int.from_bytes(bs, self.byte_order)

    def store(self, addr: int, width: int, value: int) -> None:
        addr = _mask(addr, self.xlen)
        value = _mask(value, width)
        fn = self._io_for(addr)
        if fn is not None:
            fn("store", addr, width, value)
            return
        for i, b in enumerate(value.to_bytes(width // 8, self.byte_order)):
            self.mem[addr + i] = b

    def load_bytes(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def add_io(self, base: int, size: int, handler: IOHandler) -> None:
        self.io.append((base, size, handler))


def _zeros(shape: list):
    if len(shape) == 1:
        return [0] * shape[0]
    return [_zeros(shape[1:]) for _ in range(shape[0])]


# ── Per-instruction decode/execute info ───────────────────────────────────────

@dataclass
class _InstrInfo:
    name: str
    schema_name: str
    nbytes: int
    mask: int
    match: int
    fixed_bits: int
    fields: List[Tuple[str, int, int, bool]]   # (name, start, width, signed)
    reg_fields: Dict[str, str]                 # field → register file
    zero_regs: Dict[str, int]                  # written reg field → zero index
    ir: BehaviorIR
    var_widths: Dict[str, int]


class Interpreter:
    """Executes behaviors of one ISA (an :class:`ISARegistry`) on a
    :class:`MachineState`."""

    def __init__(self, isa_reg):
        self.isa = isa_reg
        self.xlen = isa_reg.xlen
        self.regs = {r.name: r for r in isa_reg.registers}
        self.csr_info = {c.name: c for c in isa_reg.arch_csrs}
        self.trap = isa_reg.trap
        self.shapes = build_regfile_shapes(isa_reg)
        self.attr_widths = build_regfile_attrs(isa_reg)
        self.instrs: Dict[str, _InstrInfo] = {}
        for instr in isa_reg.instructions.values():
            self.instrs[instr.metadata.name] = self._build(instr)
        # Most-specific pattern first, as the generated decoders do.
        self._decode_order = sorted(self.instrs.values(),
                                    key=lambda i: i.fixed_bits, reverse=True)

    def _build(self, instr) -> _InstrInfo:
        schema = self.isa.schemas[instr.spec.schema_name]
        pattern = instruction_pattern(instr, schema)      # MSB first
        mask = match = 0
        for i, ch in enumerate(reversed(pattern)):        # i = bit index
            if ch != ".":
                mask |= 1 << i
                if ch == "1":
                    match |= 1 << i
        reg_map, var_widths = build_reg_maps(schema, self.isa)
        ir = BehaviorIR(instr.spec.behavior, register_map=reg_map,
                        var_widths=dict(var_widths), operands=self.isa.operands,
                        csrs=csr_map(self.isa), regfile_shapes=self.shapes,
                        regfile_attrs=self.attr_widths,
                        behavior_funcs=self.isa.behavior_funcs)
        fields = [(f.name, f.start, f.width, f.is_signed)
                  for f in schema.spec.fields if not f.is_fixed_value]
        zero_regs = {}
        for f in schema.spec.fields:
            if f.role == FieldRole.REGISTER and f.name in ir.write_vars:
                r = self.regs.get(f.maps_to_state)
                if r is not None and r.zero_register is not None:
                    zero_regs[f.name] = r.zero_register
        return _InstrInfo(
            name=instr.metadata.name, schema_name=schema.metadata.name,
            nbytes=schema.spec.length // 8, mask=mask, match=match,
            fixed_bits=bin(mask).count("1"), fields=fields, reg_fields=reg_map,
            zero_regs=zero_regs, ir=ir, var_widths=ir.var_widths)

    # -- decode --
    def decode(self, word: int) -> Tuple[str, Dict[str, int]]:
        """Match an instruction word (as an int) and extract its fields.
        Signed immediate fields are sign-extended (as decodetree does); every
        other field is the raw bit value."""
        for info in self._decode_order:
            if word & info.mask == info.match:
                fields = {}
                for name, start, width, signed in info.fields:
                    raw = (word >> start) & ((1 << width) - 1)
                    fields[name] = _to_signed(raw, width) if signed else raw
                return info.name, fields
        raise InterpError(f"illegal instruction: no encoding matches {word:#x}")

    def insn_bytes(self) -> int:
        return next(iter(self.instrs.values())).nbytes

    def fetch(self, st: MachineState) -> int:
        n = self.insn_bytes()
        return st.load(st.pc, n * 8)

    # -- execute --
    def execute(self, name: str, fields: Dict[str, int], st: MachineState) -> None:
        """Run one instruction's behavior. ``fields`` maps every non-fixed
        schema field to its decoded value. Advances ``st.pc`` past the
        instruction unless the behavior wrote ``pc``."""
        info = self.instrs[name]
        ex = _Exec(self, info, fields, st)
        for stmt in info.ir.tree.body:
            ex.stmt(stmt)
        if not ex.pc_written:
            st.pc = _mask(st.pc + info.nbytes, self.xlen)

    def step(self, st: MachineState) -> str:
        """Fetch, decode and execute the instruction at ``st.pc``; returns
        the instruction name."""
        name, fields = self.decode(self.fetch(st))
        self.execute(name, fields, st)
        return name

    def do_trap(self, st: MachineState, cause: int) -> None:
        """Vector through the trap CSRs exactly like the generated
        ``trap()`` / ``do_interrupt``: save epc + cause, mpie←mie, mie←0,
        pc←vector & ~3."""
        t = self.trap
        if t is None:
            raise InterpError("trap() used but the ISA declares no `trap:` block")
        st.csrs[t.epc_csr] = st.pc
        st.csrs[t.cause_csr] = _mask(cause, self.csr_info[t.cause_csr].width)
        self._status_copy(st, "mie", "mpie")
        self._status_clear(st, "mie")
        st.pc = _mask(st.csrs[t.vector_csr] & ~0x3, self.xlen)

    def do_trap_return(self, st: MachineState) -> None:
        t = self.trap
        if t is None:
            raise InterpError("trap_return() used but the ISA declares no `trap:` block")
        self._status_copy(st, "mpie", "mie")
        st.pc = _mask(st.csrs[t.epc_csr], self.xlen)

    def _csr_field(self, csr_name: str, fname: str) -> Tuple[int, int]:
        c = self.csr_info.get(csr_name)
        if c is None:
            raise InterpError(f"unknown CSR '{csr_name}'")
        for f in c.fields or []:
            if f.name == fname:
                return f.start, f.end - f.start + 1
        raise InterpError(f"CSR '{csr_name}' has no field '{fname}'")

    def _status_copy(self, st, src, dst):
        sc = self.trap.status_csr if self.trap else None
        if not sc or sc not in self.csr_info:
            return
        names = {f.name for f in self.csr_info[sc].fields or []}
        if src not in names or dst not in names:
            return
        ss, sw = self._csr_field(sc, src)
        ds, dw = self._csr_field(sc, dst)
        v = st.csrs[sc]
        bits = (v >> ss) & ((1 << sw) - 1)
        st.csrs[sc] = (v & ~(((1 << dw) - 1) << ds)) | (bits << ds)

    def _status_clear(self, st, fname):
        sc = self.trap.status_csr if self.trap else None
        if not sc or sc not in self.csr_info:
            return
        if fname not in {f.name for f in self.csr_info[sc].fields or []}:
            return
        s, w = self._csr_field(sc, fname)
        st.csrs[sc] &= ~(((1 << w) - 1) << s)


class _Exec:
    """Evaluates one instruction's statements. Mirrors QemuCBackend: values are
    computed at the C promoted width of the statement (`W`), signedness follows
    C's usual conversions, and every architectural write is masked."""

    def __init__(self, interp: Interpreter, info: _InstrInfo,
                 fields: Dict[str, int], st: MachineState):
        self.it = interp
        self.info = info
        self.ir = info.ir
        self.fields = fields
        self.st = st
        self.xlen = interp.xlen
        self.temps: Dict[str, int] = {}
        self.pc_written = False
        self.W = 32  # set per statement

    # -- widths --
    def _stmt_width(self, node: ast.AST) -> int:
        widths = [32]
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                if n.id == "pc":
                    widths.append(self.xlen)
                elif n.id in self.ir.var_widths:
                    widths.append(self.ir.var_widths[n.id])
                elif n.id in self.ir.register_map:
                    widths.append(self.it.regs[self.ir.register_map[n.id]].width)
            elif isinstance(n, (ast.Attribute, ast.Subscript)):
                try:
                    widths.append(self.ir.get_width(n))
                except ValueError:
                    pass
        return _c_width(max(widths))

    def _var_width(self, name: str) -> int:
        if name == "pc":
            return self.xlen
        if name in self.ir.register_map:
            return self.it.regs[self.ir.register_map[name]].width
        if name in self.ir.temporaries:
            # Temporaries are declared with the C type that holds their inferred
            # width (uint8_t for ≤8 bits, …), so they wrap at that storage width.
            w = self.ir.temporaries[name][0]
            return next(b for b in (8, 16, 32, 64, 128) if w <= b)
        if name in self.ir.var_widths:
            return self.ir.var_widths[name]
        raise InterpError(f"unknown variable '{name}'")

    # -- statements --
    def stmt(self, node: ast.stmt) -> None:
        if isinstance(node, ast.If):
            self.W = self._stmt_width(node.test)
            if self._truth(self.expr(node.test)):
                for s in node.body:
                    self.stmt(s)
            else:
                for s in node.orelse:
                    self.stmt(s)
            return
        if isinstance(node, ast.For):
            self.W = self._stmt_width(node.iter)
            args = [self.expr(a) for a in node.iter.args]
            lo, hi = (0, args[0].v) if len(args) == 1 else (args[0].v, args[1].v)
            for i in range(lo, hi):
                self.temps[node.target.id] = i
                for s in node.body:
                    self.stmt(s)
            return
        if isinstance(node, ast.Expr):
            v = node.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                if v.func.id == "trap":
                    self.it.do_trap(self.st, self._trap_cause(v))
                    self.pc_written = True
                    return
                if v.func.id == "trap_return":
                    self.it.do_trap_return(self.st)
                    self.pc_written = True
                    return
            raise InterpError(f"unsupported statement '{ast.unparse(node)}'")
        if isinstance(node, ast.Assign):
            self.W = self._stmt_width(node)
            self.assign(node.targets[0], node.value)
            return
        raise InterpError(f"unsupported statement '{ast.unparse(node)}'")

    def _trap_cause(self, call: ast.Call) -> int:
        arg = call.args[0] if call.args else None
        if isinstance(arg, ast.Constant):
            return int(arg.value)
        if isinstance(arg, ast.Name) and self.it.trap and arg.id in self.it.trap.causes:
            return self.it.trap.causes[arg.id]
        raise InterpError("trap() expects an integer or a declared cause name")

    def assign(self, target: ast.AST, value: ast.AST) -> None:
        ir, st = self.ir, self.st
        # reg.attr = v
        attr = ir.reg_attr_access(target)
        if attr is not None:
            regop, regfile, aname, awidth = attr
            st.attrs[regfile][aname][self._reg_index(regop)] = _mask(self.expr(value).v, awidth)
            return
        # vd[i][j] = v  /  vd[i] = vs[i] (sub-array copy)
        acc = ir.reg_element_access(target)
        if acc is not None:
            name, regfile, elem_st, shape, indices = acc
            idx = [self.expr(ix).v for ix in indices]
            if len(idx) < len(shape):
                racc = ir.reg_element_access(value)
                if racc is None:
                    raise InterpError(f"partially-indexed '{name}' can only be "
                                      f"assigned another sub-array")
                rname, rfile, _e, rshape, rindices = racc
                src = self._elem_ref(rname, rfile, [self.expr(ix).v for ix in rindices])
                self._elem_set(name, regfile, idx, _deep_copy(src))
                return
            self._elem_set(name, regfile, idx, _mask(self.expr(value).v, elem_st.width))
            return
        # csr.X = v / csr.X.f = v
        csr = BehaviorIR.csr_ref(target)
        if csr is not None:
            cname, fname = csr
            v = self.expr(value).v
            cw = self.it.csr_info[cname].width
            if fname is None:
                st.csrs[cname] = _mask(v, cw)
            else:
                s, w = self.it._csr_field(cname, fname)
                st.csrs[cname] = (st.csrs[cname] & ~(((1 << w) - 1) << s)) | (_mask(v, w) << s)
            return
        # memN[addr] = v
        if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                and target.value.id in BehaviorIR.MEM_KEYWORDS):
            width = BehaviorIR.MEM_KEYWORDS[target.value.id]
            addr = self.expr(target.slice).v
            st.store(addr, width, self.expr(value).v)
            return
        if isinstance(target, ast.Name):
            name = target.id
            tw = self._var_width(name)
            v = _mask(self.expr(value).v, tw)
            if name == "pc":
                st.pc = _mask(v, self.xlen)
                self.pc_written = True
            elif name in ir.register_map:
                idx = self._reg_index(name)
                if self.info.zero_regs.get(name) == idx:
                    return
                regfile = ir.register_map[name]
                if regfile in self.it.shapes:
                    raise InterpError(f"shaped register '{name}' must be indexed "
                                      f"to an element")
                st.regs[regfile][idx] = v
            else:
                self.temps[name] = v
            return
        raise InterpError(f"unsupported assignment target '{ast.unparse(target)}'")

    # -- register helpers --
    def _reg_index(self, name: str) -> int:
        try:
            return self.fields[name]
        except KeyError:
            raise InterpError(f"no decoded value for register field '{name}'")

    def _elem_ref(self, name, regfile, idx):
        cur = self.st.regs[regfile][self._reg_index(name)]
        for i in idx:
            cur = cur[i]
        return cur

    def _elem_set(self, name, regfile, idx, value):
        cur = self.st.regs[regfile][self._reg_index(name)]
        for i in idx[:-1]:
            cur = cur[i]
        cur[idx[-1]] = value

    # -- expressions --
    def _truth(self, v: _Val) -> bool:
        return v.v != 0

    def _wrap(self, v: int, signed: bool) -> _Val:
        return _Val(_to_signed(v, self.W) if signed else _mask(v, self.W), signed)

    def expr(self, node: ast.AST) -> _Val:
        ir, st, W = self.ir, self.st, self.W
        if isinstance(node, ast.Constant):
            # A literal is a C `int`: signed. Mixed with an unsigned operand
            # it converts to unsigned (usual arithmetic conversions), so
            # `rs1 + 1` stays unsigned while `signed(rs1) < 0` compares signed.
            return _Val(int(node.value), True)
        if isinstance(node, ast.Name):
            n = node.id
            if n == "pc":
                return _Val(st.pc, False)
            if n in self.temps:
                return _Val(self.temps[n], False)
            if n in ir.register_map:
                regfile = ir.register_map[n]
                if regfile in self.it.shapes:
                    raise InterpError(f"shaped register '{n}' must be indexed to an element")
                return _Val(st.regs[regfile][self._reg_index(n)], False)
            if n in self.fields:
                return _Val(_mask(self.fields[n], W), False)
            if n in self.it.isa.constants:
                return _Val(self.it.isa.constants[n].spec.value, False)
            raise InterpError(f"unknown variable '{n}'")
        if isinstance(node, ast.Attribute):
            attr = ir.reg_attr_access(node)
            if attr is not None:
                regop, regfile, aname, _w = attr
                return _Val(st.attrs[regfile][aname][self._reg_index(regop)], False)
            csr = BehaviorIR.csr_ref(node)
            if csr is not None:
                cname, fname = csr
                v = st.csrs[cname]
                if fname is None:
                    return _Val(v, False)
                s, w = self.it._csr_field(cname, fname)
                return _Val((v >> s) & ((1 << w) - 1), False)
            if (isinstance(node.value, ast.Name) and node.value.id in self.it.isa.enums):
                return _Val(self.it.isa.enums[node.value.id].spec.values[node.attr], False)
            raise InterpError(f"unsupported attribute '{ast.unparse(node)}'")
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id in BehaviorIR.MEM_KEYWORDS:
                width = BehaviorIR.MEM_KEYWORDS[node.value.id]
                return _Val(st.load(self.expr(node.slice).v, width), False)
            acc = ir.reg_element_access(node)
            if acc is not None:
                name, regfile, elem_st, shape, indices = acc
                idx = [self.expr(ix).v for ix in indices]
                if len(idx) != len(shape):
                    raise InterpError(f"'{name}' is a shaped register {shape}; index "
                                      f"all {len(shape)} dimension(s)")
                return _Val(self._elem_ref(name, regfile, idx), False)
            if isinstance(node.slice, ast.Slice):
                lo = node.slice.lower.value if node.slice.lower else 0
                hi = node.slice.upper.value if node.slice.upper else ir.get_width(node.value)
                base = self.expr(node.value).v
                return _Val((base >> lo) & ((1 << (hi - lo)) - 1), False)
            raise InterpError(f"unsupported subscript '{ast.unparse(node)}'")
        if isinstance(node, ast.Set):     # {a, b, c} bit concatenation
            acc = 0
            for elt in node.elts:
                w = ir.get_width(elt)
                acc = (acc << w) | _mask(self.expr(elt).v, w)
            return _Val(_mask(acc, W), False)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            f = node.func.id
            if f == "sext":
                n = node.args[1].value
                return _Val(_to_signed(self.expr(node.args[0]).v, n), True)
            if f == "zext":
                return _Val(_mask(self.expr(node.args[0]).v, W), False)
            if f == "signed":
                return _Val(_to_signed(self.expr(node.args[0]).v, W), True)
            if f in ir.operands:
                raise InterpError(f"Operand-struct constructors ('{f}(...)') are not "
                                  f"supported by the interpreter yet")
            raise InterpError(f"unknown function '{f}'")
        if isinstance(node, ast.UnaryOp):
            v = self.expr(node.operand)
            if isinstance(node.op, ast.Invert):
                return self._wrap(~v.v, v.signed)
            if isinstance(node.op, ast.USub):
                return self._wrap(-v.v, v.signed)
            if isinstance(node.op, ast.Not):
                return _Val(0 if v.v else 1, False)
        if isinstance(node, ast.BinOp):
            return self._binop(node)
        if isinstance(node, ast.Compare):
            left = self.expr(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = self.expr(comp)
                if not self._compare(type(op), left, right):
                    return _Val(0, False)
                left = right
            return _Val(1, False)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return _Val(int(all(self._truth(self.expr(v)) for v in node.values)), False)
            return _Val(int(any(self._truth(self.expr(v)) for v in node.values)), False)
        raise InterpError(f"unsupported expression '{ast.unparse(node)}'")

    def _binop(self, node: ast.BinOp) -> _Val:
        op = type(node.op)
        l, r = self.expr(node.left), self.expr(node.right)
        W = self.W
        if op in (ast.LShift, ast.RShift):
            # C shifts keep the (promoted) type of the left operand.
            sh = _mask(r.v, W) % W
            if op is ast.LShift:
                return self._wrap(l.v << sh, l.signed)
            return self._wrap(l.v >> sh, l.signed)   # Python >> is arithmetic on negatives
        both_signed = l.signed and r.signed
        if both_signed:
            a, b = l.v, r.v
        else:  # usual arithmetic conversions: any unsigned operand → unsigned
            a, b = _mask(l.v, W), _mask(r.v, W)
        if op is ast.Add:
            return self._wrap(a + b, both_signed)
        if op is ast.Sub:
            return self._wrap(a - b, both_signed)
        if op is ast.Mult:
            return self._wrap(a * b, both_signed)
        if op is ast.BitAnd:
            return self._wrap(a & b, both_signed)
        if op is ast.BitOr:
            return self._wrap(a | b, both_signed)
        if op is ast.BitXor:
            return self._wrap(a ^ b, both_signed)
        if op in (ast.Div, ast.Mod):
            if b == 0:
                raise InterpError(f"division by zero in '{ast.unparse(node)}'")
            if both_signed:
                q = _trunc_div(a, b)
                res = q if op is ast.Div else a - q * b
            else:
                res = a // b if op is ast.Div else a % b
            return self._wrap(res, both_signed)
        raise InterpError(f"unsupported operator in '{ast.unparse(node)}'")

    def _compare(self, op, l: _Val, r: _Val) -> bool:
        W = self.W
        if l.signed and r.signed:
            a, b = l.v, r.v
        else:
            a, b = _mask(l.v, W), _mask(r.v, W)
        return {ast.Eq: a == b, ast.NotEq: a != b, ast.Lt: a < b,
                ast.LtE: a <= b, ast.Gt: a > b, ast.GtE: a >= b}[op]


def _deep_copy(x):
    return [_deep_copy(e) for e in x] if isinstance(x, list) else x


# ── Program loading and the standard devices ──────────────────────────────────

def load_elf(st: MachineState, data: bytes) -> int:
    """Load every PT_LOAD segment of an ELF32/ELF64 image into ``st`` and
    return the entry address. Handles both byte orders; anything else raises."""
    import struct
    if data[:4] != b"\x7fELF":
        raise InterpError("not an ELF image")
    cls, order = data[4], data[5]
    end = "<" if order == 1 else ">"
    if cls == 1:
        (entry, phoff) = struct.unpack(end + "II", data[24:32])
        (phentsize, phnum) = struct.unpack(end + "HH", data[42:46])
        fmt, nfields = end + "IIIIIIII", 8
    elif cls == 2:
        (entry, phoff) = struct.unpack(end + "QQ", data[24:40])
        (phentsize, phnum) = struct.unpack(end + "HH", data[54:58])
        fmt, nfields = end + "IIQQQQQQ", 8
    else:
        raise InterpError("unknown ELF class")
    for i in range(phnum):
        off = phoff + i * phentsize
        ph = struct.unpack(fmt, data[off:off + struct.calcsize(fmt)])
        if cls == 1:
            p_type, p_offset, p_vaddr, _pa, p_filesz, p_memsz = ph[:6]
        else:
            p_type, _flags, p_offset, p_vaddr, _pa, p_filesz, p_memsz = ph[:7]
        if p_type != 1:  # PT_LOAD
            continue
        st.load_bytes(p_vaddr, data[p_offset:p_offset + p_filesz])
        if p_memsz > p_filesz:
            st.load_bytes(p_vaddr + p_filesz, bytes(p_memsz - p_filesz))
    return entry


def load_image(st: MachineState, data: bytes, base: Optional[int] = None) -> int:
    """Load an ELF (auto-detected) or a flat binary at ``base`` (default: the
    current ``st.pc``). Returns the entry address."""
    if data[:4] == b"\x7fELF":
        return load_elf(st, data)
    base = st.pc if base is None else base
    st.load_bytes(base, data)
    return base


def attach_machine_devices(st: MachineState, isa_reg, out=None) -> None:
    """Map the ISA's ``machine.qemu.devices`` onto the state with minimal
    models: an ``ns16550`` writes transmitted bytes to ``out`` (default
    stdout), ``sifive_test`` halts the run (``0x5555`` → exit 0, ``0x3333``
    → exit 1, ``(code << 16) | 0x3333`` → that code) exactly like QEMU's
    test-finisher, and ``irq_test`` is accepted but inert."""
    import sys
    out = out if out is not None else sys.stdout
    machine = isa_reg.machine
    if machine.qemu is None:
        return
    for dev in machine.qemu.devices:
        if dev.type == "ns16550":
            def uart(op, addr, width, value, _base=dev.base):
                if op == "store" and addr == _base:
                    out.write(chr(value & 0xFF))
                    out.flush()
                    return None
                if op == "load" and addr == _base + 5:  # LSR: THR empty
                    return 0x60
                return 0
            st.add_io(dev.base, 0x100, uart)
        elif dev.type == "sifive_test":
            def finisher(op, addr, width, value):
                if op == "store":
                    if value == 0x5555:
                        raise HaltRequested(0)
                    if value & 0xFFFF == 0x3333:
                        raise HaltRequested(value >> 16 or 1)
                return 0
            st.add_io(dev.base, 0x1000, finisher)
        elif dev.type == "irq_test":
            st.add_io(dev.base, 0x1000, lambda op, a, w, v: 0)


def format_registers(st: MachineState, isa_reg, per_line: int = 4) -> str:
    """Human-readable register / CSR dump (``isa-archive run --dump``)."""
    lines = [f"pc = {st.pc:#0{2 + (st.xlen + 3) // 4}x}"]
    for r in isa_reg.registers:
        if r.is_shaped:
            continue
        digits = (r.width + 3) // 4
        cells = [f"{r.prefix}{i:<3}= {v:#0{2 + digits}x}"
                 for i, v in enumerate(st.regs[r.name])]
        for i in range(0, len(cells), per_line):
            lines.append("  ".join(cells[i:i + per_line]))
    for name, v in st.csrs.items():
        lines.append(f"csr.{name} = {v:#x}")
    return "\n".join(lines)


# ── Program runner ────────────────────────────────────────────────────────────

def run_program(interp: Interpreter, st: MachineState, max_steps: int = 1_000_000,
                trace: Optional[Callable[[int, str, MachineState], None]] = None) -> int:
    """Fetch/execute until an I/O handler raises :class:`HaltRequested` or
    ``max_steps`` instructions have run. Returns the exit code (0 on halt; -1
    if the step limit was reached)."""
    for _ in range(max_steps):
        name, fields = interp.decode(interp.fetch(st))
        if trace:
            trace(st.pc, name, st)
        try:
            interp.execute(name, fields, st)
        except HaltRequested as h:
            return h.code
    return -1
