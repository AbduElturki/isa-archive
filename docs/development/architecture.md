# Architecture

ISA-Archive is a linear, validated pipeline: **YAML manifests → a validated in-memory model → many
generators**, each of which lowers the one `behavior:` definition to its target language and renders
a template.

```mermaid
flowchart TB
    CLI["cli.py<br/><i>parse · generate · build · init</i>"] --> LOADER

    subgraph FRONT["Front end"]
        LOADER["compiler/loader.py<br/><i>load_isa / load_uarch / load_project</i>"]
        MODELS["models/<br/><i>Pydantic StrictModels per kind</i>"]
        REG["Registry / ISARegistry<br/><i>validated, in-memory</i>"]
        LOADER --> MODELS --> REG
    end

    REG --> TARGETS["generators/targets.py<br/><i>name → generator dispatch</i>"]

    subgraph BACK["Per-target generation"]
        TARGETS --> GEN["generators/*<br/><i>qemu · llvm · sv · software · docs · asm · cpp_isa</i>"]
        GEN --> IR["compiler/behavior.py<br/><i>BehaviorIR</i>"]
        IR --> BK["compiler/backends/*<br/><i>qemu_c · qemu_tcg · verilog · rust · llvm_dag</i>"]
        GEN --> TPL["generators/templates/*<br/><i>Jinja</i>"]
    end

    BK --> OUT["generated artifacts"]
    TPL --> OUT
```

## The layers

| Layer | Where | Responsibility |
|---|---|---|
| **CLI** | `cli.py` | Typer commands `parse` / `generate` / `build` / `init`; the `-t` target list is built from `targets.TARGET_NAMES`. |
| **Models** | `models/` | One Pydantic `StrictModel` per manifest kind (`extra="forbid"`). `scalar_types.py` holds the element-type registry; `abi`/`machine`/`compiler`/`csr`/`constraint` are sub-models. |
| **Loader** | `compiler/loader.py` | `load_manifest` maps `kind:` → model; `load_isa`/`load_uarch`/`load_project` build the `Registry`; `extends`/`includes` are resolved here. `ISARegistry.validate()` runs the validation passes. |
| **Behavior IR** | `compiler/behavior.py` | `BehaviorIR` parses a `behavior:` string to a Python AST and analyzes it: used/read/written vars, bit-width inference (`get_width`), and recognizers for the DSL's namespaces (`csr_ref`, `reg_element_access`, `reg_attr_access`, trap builtins) plus flags (`modifies_pc`, `uses_sys`, `uses_structured`). |
| **Backends** | `compiler/backends/` | Lower the *same* `BehaviorIR` to each language: `qemu_c` / `qemu_tcg` (C / TCG), `verilog`, `rust`, `llvm_dag` (SelectionDAG patterns). `base._BackendBase` holds the shared expression lowering. |
| **Generators** | `generators/` | Per target: consult the registry, lower behaviors via the backends, and render Jinja templates. `base.py` provides `make_jinja_env` / `make_renderer` / `write_generated`. `targets.py` is the dispatch taxonomy (`_TARGETS`, `PARENTS`, `ALL_TARGETS`, `run_target`), shared by both `generate -t` and `build`. |
| **Templates** | `generators/templates/` | One Jinja directory per backend (`qemu`, `llvm`, `sv`, `sw`, `asm`, `cpp_isa`, `docs`) + shared `_macros.j2`. |

## One behavior, every backend

The defining principle: an instruction's semantics live in a single `behavior:` line, and each
backend *derives* its output from that one definition. `rd = rs1 + rs2` becomes a QEMU TCG op or C
helper, an LLVM `(add …)` selection pattern, and a SystemVerilog datapath - never three
hand-written copies. The same `Schema` field placements drive the decoder, assembler, and encoder.
Because everything is derived, the targets can't drift apart.

## A worked trace: `ADD`

```yaml
kind: Instruction
metadata: { name: ADD }
spec: { schema: RType, opcode: OP, funct3: F3_ALU.ADD_SUB, funct7: F7_ALU.BASE,
        behavior: "rd = rs1 + rs2" }
```

1. **Loader** resolves the fixed fields (`opcode`/`funct3`/`funct7` via the enum/constant tables),
   checks the encoding against every other instruction (decoder collisions), and validates the
   behavior's variables against the schema + register state.
2. **`build_reg_maps`** (in `compiler/utils.py`) maps `rd`/`rs1`/`rs2` to the `gpr` file and their
   widths; **`BehaviorIR`** parses `rd = rs1 + rs2`, infers widths, and records `rd` as written.
3. Each generator lowers it: **`llvm_dag`** emits `(set GPR:$rd, (add GPR:$rs1, GPR:$rs2))`;
   **`qemu_c`/`qemu_tcg`** emit the helper / TCG op; **`verilog`** emits the ALU datapath.
4. The generator renders the per-target Jinja templates into files.

The AST mechanics behind step 2 - parsing, `BehaviorIR`'s analysis, width inference, and how a
backend walks the tree - are documented in [the behavior IR page](behavior-ir.md).

## What runs when: the `generate` call chain

The diagram above is the *structural* view (which modules exist). This is the *execution* view -
which file and function fires, in order, for `isa-archive generate -i isa.yaml -t <target> -o out/`:

```mermaid
flowchart TB
    A["<b>cli.py</b> · generate()"] --> B["<b>loader.py</b> · load_isa()<br/><i>load_manifest (kind→model) · ISARegistry · resolve extends/includes</i>"]
    B --> V["<b>loader.py</b> · ISARegistry.validate()<br/><i>per instruction: BehaviorIR(…) + QemuCBackend(ir).translate()<br/>+ decoder-collision / field / enum / CSR checks</i>"]
    V --> D["<b>targets.py</b> · run_target(name)<br/><i>_TARGETS[name] lambda (+ components= for a sub-target)</i>"]
    D --> G["a generator entry<br/><i>qemu/core._write_isa_files · cpp_isa.generate_cpp_isa · llvm/core.generate_llvm · …</i>"]
    G --> C["build the template ctx<br/><i>consult the registry; lower behaviors via compiler/backends/*</i>"]
    C --> R["<b>base.py</b> · make_renderer → render(template, dest)"]
    R --> W["<b>base.py</b> · write_generated → normalize_generated"]
    W --> F["file on disk"]
```

1. **`cli.py` · `generate()`** builds an empty `Registry`, calls `load_isa` (and `load_uarch`) for
   each `-i`/`-u`, keeps only the explicitly requested ISAs, then resolves `-t` to one or more
   target names (`targets.ALL_TARGETS` for `-t all`) and calls `run_target` for each. The `-t`
   choices themselves come from `targets.TARGET_NAMES`.
2. **`loader.py` · `load_isa()`** reads the YAML docs, maps each `kind:` to a Pydantic model
   (`load_manifest`), builds the `ISARegistry`, resolves `extends:`/`includes:`, then runs
   **`ISARegistry.validate()`** - which constructs a `BehaviorIR` for every instruction and lowers
   it through `QemuCBackend(ir).translate()` to prove it's well-formed, alongside the structural
   checks (decoder collisions, field bounds, enum refs, CSR addresses). Validation happens **once**,
   before any target runs.
3. **`targets.py` · `run_target(name)`** looks `name` up in the `_TARGETS` dispatch table and calls
   its lambda, which invokes the matching generator - passing a `components={…}` filter for
   sub-targets (`qemu-isa`, `llvm-tablegen`, `docs-html`, …).
4. **The generator** (e.g. `qemu/core.py`'s `_write_isa_files`, or `cpp_isa.py`'s
   `generate_cpp_isa`) loops over the ISAs, builds a context dict - consulting the registry and
   lowering each `behavior:` through the relevant `compiler/backends/*` - and calls `render(...)`
   per output file.
5. **`base.py`** turns each `(template, ctx)` into text (`make_renderer` → Jinja
   `env.get_template(...).render(**ctx)`) and writes it through `write_generated`, which runs
   `normalize_generated` (trailing-whitespace/blank-line cleanup, single final newline) and
   optionally `clang-format`.

`isa-archive build <project.yaml>` is the same chain with a different front: `load_project` loads
every ISA/uArch the [`Project`](../yaml/project.md) references, then calls `run_target` once per
`generate:` entry into that entry's `output` path (honouring its `on_exist` policy). `parse` stops
after step 2 - it loads and validates, then reports, without generating.

## Graceful degradation

Not every construct maps to every backend. Rather than fail, a backend that can't model something
**skips or comments** it:

- QEMU/TCG falls back from the fast path to a C helper for anything non-trivial.
- The LLVM backend lists instructions it can't pattern-match as **custom-lowered** in
  `COMPILER_COVERAGE.md` (CSR/system/trap instructions, exotic shapes).
- The SystemVerilog backend emits a `// … not modeled` placeholder for CSR/trap/vector/attribute
  behaviors (gated by `BehaviorIR.uses_structured`).
- Register files the compiler can't type stay **architectural state** (simulator-only) and their
  instructions are omitted from the LLVM backend with a warning.

If generation succeeds, the output is structurally valid for that toolchain; what a backend can't
express is reported, never silently wrong.
