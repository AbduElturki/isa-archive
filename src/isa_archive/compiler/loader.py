import yaml
import logging
import pathlib
from typing import List, Dict, Any, Optional, Union

_loader_logger = logging.getLogger("isa_archive.loader")
from ..models import ManifestBase, Operand, Schema, Instruction, ISA, uArch, Constant, EnumDef
from ..models.project import Project
from ..models.machine import MachineLayout
from ..models.enums import FieldRole
from .utils import build_reg_maps, instruction_pattern

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
        self._source_files: Dict[str, str] = {}  # manifest name → source file path
        # Architectural State
        self.registers = manifest.spec.state.registers
        self.arch_csrs = manifest.spec.state.csrs
        # Machine layout — from YAML if provided, else default values
        self.machine: MachineLayout = manifest.spec.machine or MachineLayout()

    def add(self, manifest: ManifestBase, source_file: str = "") -> None:
        name = manifest.metadata.name
        if source_file:
            self._source_files[name] = source_file
        if isinstance(manifest, Operand):
            self.operands[name] = manifest
        elif isinstance(manifest, Schema):
            self.schemas[name] = manifest
        elif isinstance(manifest, Instruction):
            self.instructions[name] = manifest
        elif isinstance(manifest, Constant):
            self.constants[name] = manifest
        elif isinstance(manifest, EnumDef):
            self.enums[name] = manifest

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
        self._validate_constraint_syntax()
        self._validate_enum_refs()
        self._validate_csr_addresses()
        self._validate_schema_fields()
        instr_patterns = self._validate_instructions()
        self._validate_decoder_collisions(instr_patterns)
        self._warn_opcode_width_inconsistency()

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
                raise ValueError(
                    f"CSR Address Collision: '{csr.name}' and '{seen[csr.address]}' both use address {hex(csr.address)}"
                    f"{self._src(csr.name)}"
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

    def _validate_instructions(self) -> dict:
        logger = logging.getLogger("isa_archive.validator")
        arch_state_names = (
            {r.name for r in self.registers}
            | {alias for r in self.registers for alias in r.aliases.keys()}
            | {c.name for c in self.arch_csrs}
        )
        arch_state_names.add("pc")

        instr_patterns: dict[str, str] = {}

        for instr in self.instructions.values():
            schema = self.schemas.get(instr.spec.schema_name)
            if not schema:
                raise ValueError(
                    f"Instruction '{instr.metadata.name}' references unknown schema '{instr.spec.schema_name}'"
                    f"{self._src(instr.metadata.name)}"
                )

            schema_fields = {f.name: f for f in schema.spec.fields}

            if not any(f.role == FieldRole.OPCODE for f in schema.spec.fields):
                raise ValueError(
                    f"Schema '{schema.metadata.name}' used by instruction '{instr.metadata.name}' "
                    f"has no field with role='opcode' — every schema must have at least one opcode field"
                    f"{self._src(schema.metadata.name)}"
                )

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
                resolved[field_name] = self._resolve_value(field_value)

            instr.spec.opcode = resolved.pop("opcode")
            instr.spec.constants.update(resolved)

            instr_patterns[instr.metadata.name] = instruction_pattern(instr, schema)

            from .behavior import BehaviorIR
            from .backends import QemuCBackend
            reg_map, var_widths = build_reg_maps(schema, self)
            try:
                ir = BehaviorIR(
                    instr.spec.behavior,
                    register_map=reg_map,
                    var_widths=var_widths,
                    operands=self.operands,
                    csrs={}
                )
                QemuCBackend(ir).translate()
                for var in ir.used_vars:
                    if var in schema_fields: continue
                    if var in arch_state_names: continue
                    if var in self.constants or var in self.enums: continue
                    if var in self.operands: continue
                    if var in ir.temporaries: continue
                    raise ValueError(
                        f"Instruction '{instr.metadata.name}' behavior uses unknown variable '{var}'"
                        f"{self._src(instr.metadata.name)}"
                    )
            except SyntaxError as e:
                raise ValueError(f"Instruction {instr.metadata.name} has invalid behavior syntax: {e}")

        return instr_patterns

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
                f"— verify this is intentional"
            )


class uArchRegistry:
    def __init__(self, manifest: uArch, isa: ISARegistry):
        self.manifest = manifest
        self.name = manifest.metadata.name
        self.isa = isa
        self.blocks = manifest.spec.blocks
        # Micro-architectural state
        self.custom_csrs = manifest.spec.state.csrs

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
    }
    if kind not in mapping: raise ValueError(f"Unknown kind: {kind}")
    return mapping[kind](**data)

def load_isa(isa_path: str, global_registry: Optional[Registry] = None) -> ISARegistry:
    if global_registry is None: global_registry = Registry()
    path = pathlib.Path(isa_path).resolve()
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
    global_registry.isas[isa_reg.name] = isa_reg
    for m in other_manifests: isa_reg.add(m, source_file=str(path))
    if isa_manifest.spec.extends:
        base_isa_path = (path.parent / isa_manifest.spec.extends).resolve()
        base_isa_reg = load_isa(str(base_isa_path), global_registry)
        isa_reg.operands.update(base_isa_reg.operands)
        isa_reg.schemas.update(base_isa_reg.schemas)
        isa_reg.instructions.update(base_isa_reg.instructions)
        isa_reg.constants.update(base_isa_reg.constants)
        isa_reg.enums.update(base_isa_reg.enums)
        isa_reg._source_files.update(base_isa_reg._source_files)
        if not isa_reg.registers:
            isa_reg.registers = base_isa_reg.registers
        if not isa_reg.arch_csrs:
            isa_reg.arch_csrs = base_isa_reg.arch_csrs
        # Spec-level identity is inherited too, unless the extension explicitly
        # sets it (pydantic's model_fields_set distinguishes "set to the
        # default" from "not set"). Without this, an extension silently lost
        # its base's xlen/ABI/machine/triple — e.g. its LLVM backend would
        # register under a Triple that doesn't exist and fail to build.
        base_spec = base_isa_reg.manifest.spec
        spec = isa_manifest.spec
        for fld in ("xlen", "byte_order", "abi", "machine", "compiler",
                    "triple_arch", "elf_machine", "nop_encoding",
                    "elf_relocations"):
            if fld not in spec.model_fields_set:
                setattr(spec, fld, getattr(base_spec, fld))
        isa_reg.xlen = spec.xlen
        isa_reg.machine = spec.machine if spec.machine is not None else isa_reg.machine
    for pattern in isa_manifest.spec.includes:
        for matched_path in path.parent.glob(pattern):
            if matched_path.resolve() == path: continue
            if matched_path.stat().st_size > MAX_YAML_BYTES:
                raise ValueError(f"Manifest file {matched_path} exceeds size limit ({MAX_YAML_BYTES} bytes)")
            with open(matched_path, 'r') as f:
                for doc in yaml.safe_load_all(f):
                    if doc: isa_reg.add(load_manifest(doc), source_file=str(matched_path))
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
    uarch_reg = uArchRegistry(uarch_manifest, global_registry.isas[uarch_manifest.spec.isa])
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
            continue
        try:
            with open(yaml_file, "r") as f:
                for doc in yaml.safe_load_all(f):
                    if not doc:
                        continue
                    kind = doc.get("kind")
                    if kind == "ISA":
                        isa_paths.append(str(yaml_file))
                    elif kind == "uArch":
                        uarch_paths.append(str(yaml_file))
                    break
        except Exception:
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

    Returns ``(registry, project, project_dir, requested_isa_names)`` — the last is
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
