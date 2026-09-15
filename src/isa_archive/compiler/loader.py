import yaml
import logging
import pathlib
from typing import List, Dict, Any, Optional, Union

_loader_logger = logging.getLogger("isa_archive.loader")
from ..models import ManifestBase, Operand, Schema, Instruction, ISA, uArch, Constant, EnumDef
from ..models.project import Project
from ..models.scalar_type_def import ScalarTypeDef
from ..models.behavior_func import BehaviorFunc
from ..models.machine import MachineLayout
from ..models.enums import FieldRole
from ..models import scalar_types
from .behavior import BehaviorIR, validate_ir
from .utils import (build_reg_maps, instruction_pattern, csr_map,
                    build_regfile_shapes, build_regfile_attrs)

MAX_YAML_BYTES = 10 * 1024 * 1024  # 10 MB

class ISARegistry:
    """Contains all components for a specific ISA."""
    def __init__(self, manifest: ISA):
        self.manifest = manifest
        self.name = manifest.metadata.name
        self.xlen: int = manifest.spec.xlen
        self.operands: Dict[str, Operand] = {}
        self.schemas: Dict[str, Schema] = {}
        self.instructions: Dict[str, Instruction] = {}
        self.constants: Dict[str, Constant] = {}
        self.enums: Dict[str, EnumDef] = {}
        self.scalar_types: Dict[str, ScalarTypeDef] = {}
        self.behavior_funcs: Dict[str, BehaviorFunc] = {}  # reusable DSL functions
        self._source_files: Dict[str, str] = {}  # manifest name → source file path
        # Architectural State
        self.registers = manifest.spec.state.registers
        self.arch_csrs = manifest.spec.state.csrs
        self.trap = manifest.spec.trap  # trap/exception wiring (or None)
        # Machine layout - from YAML if provided, else default values
        self.machine: MachineLayout = manifest.spec.machine or MachineLayout()

    def add(self, manifest: ManifestBase, source_file: str = "") -> None:
        name = manifest.metadata.name
        kind_to_dict = {
            Operand: self.operands, Schema: self.schemas,
            Instruction: self.instructions, Constant: self.constants,
            EnumDef: self.enums, ScalarTypeDef: self.scalar_types,
            BehaviorFunc: self.behavior_funcs,
        }
        target = next((d for cls, d in kind_to_dict.items()
                       if isinstance(manifest, cls)), None)
        if target is None:
            _loader_logger.warning(
                "ISA '%s': ignoring unsupported document kind '%s' (%s)%s",
                self.name, manifest.kind, name,
                f" [{source_file}]" if source_file else "")
            return
        if name in target:
            _loader_logger.warning(
                "ISA '%s': %s '%s' is defined more than once; the definition from "
                "%s replaces the one from %s",
                self.name, manifest.kind, name,
                source_file or "<unknown>", self._source_files.get(name, "<unknown>"))
        if source_file:
            self._source_files[name] = source_file
        target[name] = manifest
        if isinstance(manifest, ScalarTypeDef):
            scalar_types.register_from_manifest(manifest)  # visible to resolve() at once

    def activate_scalar_types(self) -> None:
        """Make this ISA's declared scalar types the process-wide registered set.

        The scalar-type registry is global (``resolve()`` is called from model
        properties with no ISA context), so when several ISAs live in one
        process each consumer activates the ISA it is working on first - the
        loader before validation, every generator at the top of its per-ISA
        loop. This keeps one ISA's ``kind: ScalarType`` definitions from
        leaking into another's."""
        scalar_types.clear_registered()
        for st in self.scalar_types.values():
            scalar_types.register_from_manifest(st)

    @property
    def display_name(self) -> str:
        return self.manifest.spec.name or self.manifest.metadata.name

    def _src(self, name: str) -> str:
        p = self._source_files.get(name, "")
        return f" [{p}]" if p else ""

    def _resolve_value(self, value: Union[int, str]) -> int:
        if isinstance(value, int): return value
        if "." in value:
            enum_name, member_name = value.split(".", 1)
            if enum_name in self.enums:
                enum = self.enums[enum_name]
                if member_name in enum.spec.values:
                    return enum.spec.values[member_name]
        if value in self.constants:
            return self.constants[value].spec.value
        raise ValueError(f"Could not resolve: {value}")

    def validate(self):
        """Validate the assembled ISA. Also runs one documented normalization
        pass (:meth:`_resolve_fixed_fields`): named opcode/constant values are
        resolved to ints in place on the manifests. Idempotent."""
        self._validate_register_types()
        self._validate_constraint_syntax()
        self._validate_enum_refs()
        self._validate_csr_addresses()
        self._validate_schema_fields()
        self._validate_behavior_funcs()  # before instructions: funcs must be known
        instr_patterns = self._validate_instructions()
        self._validate_decoder_collisions(instr_patterns)
        self._warn_opcode_width_inconsistency()

    def _validate_register_types(self):
        """A register file's `type:` must name a known scalar type (built-in or a
        declared `kind: ScalarType`) or an Operand struct - catches typos that
        would otherwise silently fall back to opaque integer storage."""
        for reg in self.registers:
            t = getattr(reg, "type", None)
            if t and scalar_types.resolve(t) is None and t not in self.operands:
                raise ValueError(
                    f"Register file '{reg.name}' has unknown type '{t}'; expected a "
                    f"scalar type (e.g. i32/f32, or a declared kind: ScalarType) or an "
                    f"Operand struct{self._src(self.name)}"
                )
            if reg.is_shaped:
                if any(d < 1 for d in reg.shape):
                    raise ValueError(
                        f"Register file '{reg.name}' has a non-positive shape dimension "
                        f"in {reg.shape}{self._src(self.name)}"
                    )
                st = scalar_types.resolve(t) if t else None
                if st is None:
                    raise ValueError(
                        f"Register file '{reg.name}' is shaped {reg.shape} but its element "
                        f"`type:` '{t}' is not a scalar type (shaped registers hold scalar "
                        f"elements, not Operand structs){self._src(self.name)}"
                    )
                expected = st.width * reg.lane_count
                if reg.width != expected:
                    raise ValueError(
                        f"Register file '{reg.name}' width {reg.width} ≠ element width "
                        f"{st.width} × {reg.lane_count} lanes = {expected} (shape {reg.shape}, "
                        f"element '{t}'){self._src(self.name)}"
                    )

    def _validate_constraint_syntax(self):
        import ast as _ast
        for schema in self.schemas.values():
            for c in schema.spec.constraints:
                try:
                    _ast.parse(c.expr, mode='eval')
                except SyntaxError as e:
                    raise ValueError(
                        f"Schema '{schema.metadata.name}' has invalid constraint expression '{c.expr}': {e}"
                        f"{self._src(schema.metadata.name)}"
                    )
        for instr in self.instructions.values():
            for c in instr.spec.constraints:
                try:
                    _ast.parse(c.expr, mode='eval')
                except SyntaxError as e:
                    raise ValueError(
                        f"Instruction '{instr.metadata.name}' has invalid constraint expression '{c.expr}': {e}"
                        f"{self._src(instr.metadata.name)}"
                    )
        for operand in self.operands.values():
            for c in operand.spec.constraints:
                try:
                    _ast.parse(c.expr, mode='eval')
                except SyntaxError as e:
                    raise ValueError(
                        f"Operand '{operand.metadata.name}' has invalid constraint expression '{c.expr}': {e}"
                    )

    def _validate_enum_refs(self):
        logger = logging.getLogger("isa_archive.validator")
        for schema in self.schemas.values():
            for field in schema.spec.fields:
                if field.enum_ref is not None:
                    if field.enum_ref not in self.enums:
                        raise ValueError(
                            f"Schema '{schema.metadata.name}' field '{field.name}' references unknown enum '{field.enum_ref}'"
                            f"{self._src(schema.metadata.name)}"
                        )
                    declared_width = self.enums[field.enum_ref].spec.width
                    if declared_width != field.width:
                        logger.warning(
                            f"Schema '{schema.metadata.name}' field '{field.name}': "
                            f"width {field.width}b doesn't match enum '{field.enum_ref}' width {declared_width}b"
                        )

    def _validate_csr_addresses(self):
        seen: dict[int, str] = {}
        for csr in self.arch_csrs:
            if csr.address in seen:
                # CSRs are declared inline in the ISA spec, so point at the ISA file.
                raise ValueError(
                    f"CSR Address Collision: '{csr.name}' and '{seen[csr.address]}' both use address {hex(csr.address)}"
                    f"{self._src(self.name)}"
                )
            seen[csr.address] = csr.name

    def _validate_schema_fields(self):
        logger = logging.getLogger("isa_archive.validator")
        reg_map_by_name = {r.name: r for r in self.registers}
        for schema in self.schemas.values():
            allocated_bits: set[int] = set()
            for field in schema.spec.fields:
                if field.start > field.end:
                    raise ValueError(
                        f"Schema '{schema.metadata.name}' field '{field.name}' has invalid bounds: "
                        f"start ({field.start}) > end ({field.end}){self._src(schema.metadata.name)}"
                    )
                if field.end >= schema.spec.length:
                    raise ValueError(
                        f"Schema '{schema.metadata.name}' field '{field.name}' out of bounds "
                        f"(end {field.end} >= length {schema.spec.length}){self._src(schema.metadata.name)}"
                    )
                field_bits = set(range(field.start, field.end + 1))
                if allocated_bits & field_bits:
                    raise ValueError(
                        f"Schema '{schema.metadata.name}' field '{field.name}' overlaps with another field"
                        f"{self._src(schema.metadata.name)}"
                    )
                allocated_bits.update(field_bits)
                if field.maps_to_state:
                    if field.maps_to_state not in reg_map_by_name:
                        raise ValueError(
                            f"Schema '{schema.metadata.name}' field '{field.name}' maps to unknown state "
                            f"'{field.maps_to_state}'{self._src(schema.metadata.name)}"
                        )
                    reg = reg_map_by_name[field.maps_to_state]
                    max_addressable = 1 << field.width
                    if max_addressable < reg.count:
                        raise ValueError(
                            f"Schema '{schema.metadata.name}' field '{field.name}' is too narrow to address "
                            f"all {reg.count} registers in '{reg.name}'{self._src(schema.metadata.name)}"
                        )
                    if max_addressable > reg.count:
                        logger.warning(
                            f"Schema '{schema.metadata.name}' field '{field.name}' is wider than necessary "
                            f"for {reg.count} registers in '{reg.name}'"
                        )

    def _validate_behavior_funcs(self):
        """Structural checks on `kind: BehaviorFunc` (before instruction validation,
        so calls can be resolved): arg/return types resolve, exactly one tail
        `return` iff `returns:` is set, no assignment to a read-only arg, and no
        recursion (direct or transitive) - inlining can't expand a cycle."""
        import ast
        funcs = self.behavior_funcs
        if not funcs:
            return
        call_graph = {}
        for fname, fdef in funcs.items():
            src = self._src(fname)
            for a in fdef.spec.args:
                if scalar_types.resolve(a.type) is None and a.type not in self.operands:
                    raise ValueError(f"BehaviorFunc '{fname}' arg '{a.name}' has unknown "
                                     f"type '{a.type}'{src}")
            if fdef.spec.returns is not None and \
                    scalar_types.resolve(fdef.spec.returns) is None and \
                    fdef.spec.returns not in self.operands:
                raise ValueError(f"BehaviorFunc '{fname}' has unknown return type "
                                 f"'{fdef.spec.returns}'{src}")
            try:
                tree = ast.parse(fdef.spec.behavior)
            except SyntaxError:
                raise ValueError(f"BehaviorFunc '{fname}' has invalid behavior syntax{src}")
            returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
            if fdef.spec.returns is not None:
                if not tree.body or not isinstance(tree.body[-1], ast.Return) \
                        or len(returns) != 1 or tree.body[-1].value is None:
                    raise ValueError(f"BehaviorFunc '{fname}' declares `returns:` and must "
                                     f"have exactly one `return <value>`, as the last "
                                     f"statement{src}")
            elif returns:
                raise ValueError(f"BehaviorFunc '{fname}' returns nothing (no `returns:`) "
                                 f"but its body has a `return`{src}")
            # A read-only *scalar* arg may be reassigned inside the body (pass-by-value:
            # the change is local). But a read-only *operand* arg can't be written -
            # only editable operand args write back to the caller.
            ro_operand = {a.name for a in fdef.spec.args
                          if a.type in self.operands and not a.editable}
            for n in ast.walk(tree):
                if isinstance(n, (ast.Assign, ast.AugAssign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id in ro_operand:
                            raise ValueError(f"BehaviorFunc '{fname}' assigns to read-only "
                                             f"operand arg '{t.id}' - mark it "
                                             f"`editable: true`{src}")
            call_graph[fname] = {n.func.id for n in ast.walk(tree)
                                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                 and n.func.id in funcs}
        # Cycle detection (DFS with grey/black colouring).
        WHITE, GREY, BLACK = 0, 1, 2
        color = {f: WHITE for f in funcs}

        def _dfs(u):
            color[u] = GREY
            for v in call_graph.get(u, ()):
                if color[v] == GREY:
                    raise ValueError(f"BehaviorFunc '{u}' is part of a recursive call "
                                     f"cycle; recursion is not supported{self._src(u)}")
                if color[v] == WHITE:
                    _dfs(v)
            color[u] = BLACK

        for f in funcs:
            if color[f] == WHITE:
                _dfs(f)

    def _resolve_fixed_fields(self, instr: Instruction, schema: Schema,
                              schema_fields: dict) -> None:
        """Normalization pass: resolve an instruction's fixed-field values
        (opcode + constants, possibly named constants or enum members) to plain
        ints, **in place** on the manifest. Idempotent - already-int values pass
        through unchanged, so re-validating is safe. Every downstream consumer
        (pattern building, assembler, TableGen fixed fields) relies on this
        having run. Also enforces that each resolved value fits its field."""
        logger = logging.getLogger("isa_archive.validator")
        fixed_fields = {f.name for f in schema.spec.fields if f.role in (FieldRole.OPCODE, FieldRole.CONSTANT)}
        instr_fixed = {"opcode": instr.spec.opcode}
        instr_fixed.update(instr.spec.constants)

        missing = fixed_fields - set(instr_fixed.keys())
        if missing:
            raise ValueError(
                f"Instruction '{instr.metadata.name}' is missing values for fixed fields: {missing}"
                f"{self._src(instr.metadata.name)}"
            )

        resolved: dict[str, int] = {}
        for field_name, field_value in instr_fixed.items():
            if field_name not in schema_fields:
                raise ValueError(
                    f"Instruction '{instr.metadata.name}' sets unknown field '{field_name}'"
                    f"{self._src(instr.metadata.name)}"
                )
            field = schema_fields[field_name]
            if field.role not in (FieldRole.OPCODE, FieldRole.CONSTANT):
                raise ValueError(
                    f"Instruction '{instr.metadata.name}' fixed entry '{field_name}' must be a "
                    f"role='opcode' or role='constant' field{self._src(instr.metadata.name)}"
                )
            if field.enum_ref is not None and isinstance(field_value, str) and "." in field_value:
                used_enum = field_value.split(".", 1)[0]
                if used_enum != field.enum_ref:
                    logger.warning(
                        f"Instruction '{instr.metadata.name}' field '{field_name}' uses enum '{used_enum}' "
                        f"but schema declares enum '{field.enum_ref}'"
                    )
            value = self._resolve_value(field_value)
            if value < 0 or value >= (1 << field.width):
                raise ValueError(
                    f"Instruction '{instr.metadata.name}' field '{field_name}' value "
                    f"{value:#x} does not fit in the field's {field.width} bit(s)"
                    f"{self._src(instr.metadata.name)}"
                )
            resolved[field_name] = value

        instr.spec.opcode = resolved.pop("opcode")
        instr.spec.constants.update(resolved)

    def _validate_instructions(self) -> dict:
        alias_names = {alias for r in self.registers for alias in r.aliases}
        csr_names = {c.name for c in self.arch_csrs}
        regfile_names = {r.name for r in self.registers}

        instr_patterns: dict[str, str] = {}

        for instr in self.instructions.values():
            name = instr.metadata.name
            schema = self.schemas.get(instr.spec.schema_name)
            if not schema:
                raise ValueError(
                    f"Instruction '{name}' references unknown schema '{instr.spec.schema_name}'"
                    f"{self._src(name)}"
                )

            schema_fields = {f.name: f for f in schema.spec.fields}

            if not any(f.role == FieldRole.OPCODE for f in schema.spec.fields):
                raise ValueError(
                    f"Schema '{schema.metadata.name}' used by instruction '{name}' "
                    f"has no field with role='opcode' - every schema must have at least one opcode field"
                    f"{self._src(schema.metadata.name)}"
                )

            self._resolve_fixed_fields(instr, schema, schema_fields)

            instr_patterns[name] = instruction_pattern(instr, schema)

            reg_map, var_widths = build_reg_maps(schema, self)
            try:
                ir = BehaviorIR(
                    instr.spec.behavior,
                    register_map=reg_map,
                    var_widths=var_widths,
                    operands=self.operands,
                    csrs=csr_map(self),
                    regfile_shapes=build_regfile_shapes(self),
                    regfile_attrs=build_regfile_attrs(self),
                    behavior_funcs=self.behavior_funcs,
                )
            except ValueError as e:
                raise ValueError(f"Instruction '{name}' has invalid behavior: {e}"
                                 f"{self._src(name)}")
            for w in ir.inline_warnings:
                logging.getLogger("isa_archive.validator").warning(
                    f"Instruction '{name}': {w}")
            self._validate_sys_usage(instr, ir)
            if ir.unknown_reg_attrs:
                rg, at = sorted(ir.unknown_reg_attrs)[0]
                raise ValueError(
                    f"Instruction '{name}' accesses '{rg}.{at}', but "
                    f"register file '{reg_map.get(rg, rg)}' declares no attribute "
                    f"'{at}'{self._src(name)}")
            # Variable hygiene first, so a misused name gets its targeted message
            # instead of a width error from the structural checks below.
            for var in ir.used_vars:
                if var == "pc" or var in schema_fields or var in ir.temporaries:
                    continue
                if var in self.constants or var in self.enums or var in self.operands:
                    continue
                if var in alias_names:
                    raise ValueError(
                        f"Instruction '{name}' behavior references register alias "
                        f"'{var}'; aliases name fixed registers for the ABI and "
                        f"assembler only - use a schema field with role: register "
                        f"instead{self._src(name)}"
                    )
                if var in csr_names:
                    raise ValueError(
                        f"Instruction '{name}' behavior references CSR '{var}' as a "
                        f"bare name; use 'csr.{var}'{self._src(name)}"
                    )
                if var in regfile_names:
                    raise ValueError(
                        f"Instruction '{name}' behavior references register file "
                        f"'{var}' directly; registers are accessed through schema "
                        f"fields with role: register{self._src(name)}"
                    )
                raise ValueError(
                    f"Instruction '{name}' behavior uses unknown variable '{var}'"
                    f"{self._src(name)}"
                )
            try:
                validate_ir(ir)
            except ValueError as e:
                raise ValueError(f"Instruction '{name}': {e}{self._src(name)}")

        return instr_patterns

    def _validate_sys_usage(self, instr, ir) -> None:
        """Check CSR / trap usage against the ISA's declared state and trap block."""
        known_csrs = {c.name for c in self.arch_csrs}
        for csr_name in ir.csrs_used:
            if csr_name not in known_csrs:
                raise ValueError(
                    f"Instruction '{instr.metadata.name}' references undeclared CSR "
                    f"'csr.{csr_name}'{self._src(instr.metadata.name)}"
                )
        if ir.uses_trap and self.trap is None:
            raise ValueError(
                f"Instruction '{instr.metadata.name}' uses trap()/trap_return() but the "
                f"ISA declares no `trap:` block{self._src(instr.metadata.name)}"
            )
        for cause in ir.trap_causes_used:
            if self.trap is None or cause not in self.trap.causes:
                raise ValueError(
                    f"Instruction '{instr.metadata.name}' uses trap('{cause}') but that "
                    f"cause is not declared in spec.trap.causes{self._src(instr.metadata.name)}"
                )

    def _validate_decoder_collisions(self, instr_patterns: dict):
        names = list(instr_patterns.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                n1, n2 = names[i], names[j]
                p1, p2 = instr_patterns[n1], instr_patterns[n2]
                if len(p1) != len(p2):
                    continue
                conflict = all(
                    b1 == '.' or b2 == '.' or b1 == b2
                    for b1, b2 in zip(p1, p2)
                )
                if conflict:
                    raise ValueError(
                        f"Decoder Collision: Instructions '{n1}' and '{n2}' have overlapping opcode patterns"
                        f"{self._src(n1)}"
                    )

    def _warn_opcode_width_inconsistency(self):
        logger = logging.getLogger("isa_archive.validator")
        opcode_widths: dict[str, int] = {}
        for schema in self.schemas.values():
            total = sum(f.width for f in schema.spec.fields if f.role == FieldRole.OPCODE)
            if total > 0:
                opcode_widths[schema.metadata.name] = total
        unique_widths = set(opcode_widths.values())
        if len(unique_widths) > 1:
            details = ", ".join(f"'{n}'={w}b" for n, w in sorted(opcode_widths.items()))
            logger.warning(
                f"ISA '{self.name}': schemas have inconsistent opcode field widths ({details}) "
                f"- verify this is intentional"
            )


class uArchRegistry:
    def __init__(self, manifest: uArch, isa: ISARegistry):
        self.manifest = manifest
        self.name = manifest.metadata.name
        self.isa = isa
        self.blocks = manifest.spec.blocks
        # Micro-architectural state
        self.custom_csrs = manifest.spec.state.csrs

    def add(self, manifest: ManifestBase) -> None:
        """uArch state (CSRs, blocks) lives inline in the uArch spec; there is no
        semantics yet for standalone documents alongside a uArch, so anything a
        uArch file (or its `includes:` globs) pulls in is skipped with a warning
        rather than crashing the load."""
        _loader_logger.warning(
            "uArch '%s': ignoring document kind '%s' (%s) - uArch manifests carry "
            "their state inline in spec:, standalone documents are not supported",
            self.name, manifest.kind, manifest.metadata.name)

class Registry:
    def __init__(self):
        self.isas: Dict[str, ISARegistry] = {}
        self.uarches: Dict[str, uArchRegistry] = {}

    def get_isa(self, name: str) -> ISARegistry:
        return self.isas[name]


def load_manifest(data: Dict[str, Any]) -> ManifestBase:
    kind = data.get("kind")
    mapping = {
        "ISA": ISA, "uArch": uArch, "Operand": Operand,
        "Schema": Schema, "Instruction": Instruction,
        "Constant": Constant, "Enum": EnumDef, "Project": Project,
        "ScalarType": ScalarTypeDef, "BehaviorFunc": BehaviorFunc,
    }
    if kind not in mapping: raise ValueError(f"Unknown kind: {kind}")
    return mapping[kind](**data)

def load_isa(isa_path: str, global_registry: Optional[Registry] = None,
             _extends_chain: Optional[List[str]] = None) -> ISARegistry:
    if global_registry is None: global_registry = Registry()
    path = pathlib.Path(isa_path).resolve()
    _extends_chain = list(_extends_chain or [])
    if str(path) in _extends_chain:
        raise ValueError(
            "Circular extends: " + " -> ".join(_extends_chain + [str(path)]))
    _extends_chain.append(str(path))
    if path.stat().st_size > MAX_YAML_BYTES:
        raise ValueError(f"Manifest file {path} exceeds size limit ({MAX_YAML_BYTES} bytes)")
    with open(path, 'r') as f:
        docs = list(yaml.safe_load_all(f))
    isa_manifest = None
    other_manifests = []
    for doc in docs:
        if not doc: continue
        manifest = load_manifest(doc)
        if isinstance(manifest, ISA): isa_manifest = manifest
        else: other_manifests.append(manifest)
    if not isa_manifest: raise ValueError(f"No ISA in {isa_path}")
    isa_reg = ISARegistry(isa_manifest)
    isa_reg._source_files[isa_reg.name] = str(path)
    global_registry.isas[isa_reg.name] = isa_reg
    for m in other_manifests: isa_reg.add(m, source_file=str(path))
    for pattern in isa_manifest.spec.includes:
        for matched_path in path.parent.glob(pattern):
            if matched_path.resolve() == path: continue
            if matched_path.stat().st_size > MAX_YAML_BYTES:
                raise ValueError(f"Manifest file {matched_path} exceeds size limit ({MAX_YAML_BYTES} bytes)")
            with open(matched_path, 'r') as f:
                for doc in yaml.safe_load_all(f):
                    if doc: isa_reg.add(load_manifest(doc), source_file=str(matched_path))
    if isa_manifest.spec.extends:
        base_isa_path = (path.parent / isa_manifest.spec.extends).resolve()
        base_isa_reg = load_isa(str(base_isa_path), global_registry, _extends_chain)
        # The base's content is merged *under* the extension: a name defined by
        # the extension (inline or via includes:) overrides the base's, so an
        # extension can redefine a base instruction/schema/operand.
        isa_reg.operands = {**base_isa_reg.operands, **isa_reg.operands}
        isa_reg.schemas = {**base_isa_reg.schemas, **isa_reg.schemas}
        isa_reg.instructions = {**base_isa_reg.instructions, **isa_reg.instructions}
        isa_reg.constants = {**base_isa_reg.constants, **isa_reg.constants}
        isa_reg.enums = {**base_isa_reg.enums, **isa_reg.enums}
        isa_reg.scalar_types = {**base_isa_reg.scalar_types, **isa_reg.scalar_types}
        isa_reg.behavior_funcs = {**base_isa_reg.behavior_funcs, **isa_reg.behavior_funcs}
        isa_reg._source_files = {**base_isa_reg._source_files, **isa_reg._source_files}
        if not isa_reg.registers:
            isa_reg.registers = base_isa_reg.registers
        if not isa_reg.arch_csrs:
            isa_reg.arch_csrs = base_isa_reg.arch_csrs
        # Spec-level identity is inherited too, unless the extension explicitly
        # sets it (pydantic's model_fields_set distinguishes "set to the
        # default" from "not set"). Without this, an extension silently lost
        # its base's xlen/ABI/machine/triple - e.g. its LLVM backend would
        # register under a Triple that doesn't exist and fail to build.
        base_spec = base_isa_reg.manifest.spec
        spec = isa_manifest.spec
        for fld in ("xlen", "byte_order", "asm_comment", "abi", "machine", "compiler",
                    "triple_arch", "elf_machine", "nop_encoding",
                    "elf_relocations", "trap"):
            if fld not in spec.model_fields_set:
                setattr(spec, fld, getattr(base_spec, fld))
        isa_reg.xlen = spec.xlen
        isa_reg.machine = spec.machine if spec.machine is not None else isa_reg.machine
        isa_reg.trap = spec.trap
    isa_reg.activate_scalar_types()
    isa_reg.validate()
    return isa_reg

def load_uarch(uarch_path: str, global_registry: Registry) -> uArchRegistry:
    path = pathlib.Path(uarch_path).resolve()
    if path.stat().st_size > MAX_YAML_BYTES:
        raise ValueError(f"Manifest file {path} exceeds size limit ({MAX_YAML_BYTES} bytes)")
    with open(path, 'r') as f:
        docs = list(yaml.safe_load_all(f))
    uarch_manifest = None
    other_manifests = []
    for doc in docs:
        if not doc: continue
        m = load_manifest(doc)
        if isinstance(m, uArch): uarch_manifest = m
        else: other_manifests.append(m)
    if uarch_manifest is None:
        raise ValueError(f"No uArch manifest in {uarch_path}")
    isa_name = uarch_manifest.spec.isa
    if isa_name not in global_registry.isas:
        raise ValueError(
            f"uArch '{uarch_manifest.metadata.name}' targets ISA '{isa_name}', which is "
            f"not loaded (known: {', '.join(sorted(global_registry.isas)) or 'none'})")
    uarch_reg = uArchRegistry(uarch_manifest, global_registry.isas[isa_name])
    global_registry.uarches[uarch_reg.name] = uarch_reg
    for m in other_manifests: uarch_reg.add(m)
    for pattern in uarch_manifest.spec.includes:
        for matched_path in path.parent.glob(pattern):
            if matched_path.resolve() == path: continue
            if matched_path.stat().st_size > MAX_YAML_BYTES:
                raise ValueError(f"Manifest file {matched_path} exceeds size limit ({MAX_YAML_BYTES} bytes)")
            with open(matched_path, 'r') as f:
                for doc in yaml.safe_load_all(f):
                    if doc: uarch_reg.add(load_manifest(doc))
    isa_exec_types = {instr.spec.exec_type for instr in uarch_reg.isa.instructions.values() if instr.spec.exec_type}
    for block in uarch_reg.blocks:
        unmatched = set(block.handles) - isa_exec_types
        if unmatched:
            _loader_logger.warning(
                f"uArch block '{block.name}' handles {sorted(unmatched)!r} but no instructions in ISA "
                f"'{uarch_reg.isa.name}' have those exec_types"
            )
    return uarch_reg

def load_directory(directory: str) -> Registry:
    """Load all ISA and uArch manifests found in a directory (non-recursive)."""
    global_registry = Registry()
    dir_path = pathlib.Path(directory).resolve()
    if not dir_path.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    isa_paths: list[str] = []
    uarch_paths: list[str] = []

    for yaml_file in sorted(dir_path.glob("*.yaml")):
        if yaml_file.stat().st_size > MAX_YAML_BYTES:
            _loader_logger.warning("skipping %s: exceeds size limit (%d bytes)",
                                   yaml_file, MAX_YAML_BYTES)
            continue
        try:
            with open(yaml_file, "r") as f:
                # Scan every document: a file is an ISA/uArch root wherever the
                # root manifest sits in it, not only when it is the first doc.
                kinds = {doc.get("kind") for doc in yaml.safe_load_all(f)
                         if isinstance(doc, dict)}
            if "ISA" in kinds:
                isa_paths.append(str(yaml_file))
            elif "uArch" in kinds:
                uarch_paths.append(str(yaml_file))
        except Exception as e:
            _loader_logger.warning("skipping %s: cannot scan for manifests (%s)",
                                   yaml_file, e)
            continue

    if not isa_paths:
        raise ValueError(f"No ISA manifest found in {directory}")

    for p in isa_paths:
        load_isa(p, global_registry)
    for p in uarch_paths:
        load_uarch(p, global_registry)

    return global_registry


def load_project(project_path: str, global_registry: Optional[Registry] = None):
    """Load a `kind: Project` manifest: parse it, then load every ISA and uArch it
    references (paths relative to the project file) into a Registry.

    Returns ``(registry, project, project_dir, requested_isa_names)`` - the last is
    the names of the explicitly-listed ISAs (an ``extends:`` base is also loaded so
    an extension can resolve, but it is not in this list).
    """
    if global_registry is None:
        global_registry = Registry()
    path = pathlib.Path(project_path).resolve()
    if path.stat().st_size > MAX_YAML_BYTES:
        raise ValueError(f"Manifest file {path} exceeds size limit ({MAX_YAML_BYTES} bytes)")
    with open(path, "r") as f:
        docs = list(yaml.safe_load_all(f))
    project = None
    for doc in docs:
        if not doc:
            continue
        manifest = load_manifest(doc)
        if isinstance(manifest, Project):
            project = manifest
            break
    if project is None:
        raise ValueError(f"No Project manifest in {project_path}")

    requested: list[str] = []
    for isa_rel in project.spec.isas:
        requested.append(load_isa(str((path.parent / isa_rel).resolve()), global_registry).name)
    for uarch_rel in project.spec.uarch:
        load_uarch(str((path.parent / uarch_rel).resolve()), global_registry)

    return global_registry, project, path.parent, requested
