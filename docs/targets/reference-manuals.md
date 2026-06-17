# Reference manuals (`-t docs`)

```sh
isa-archive generate --isa my-isa/isa.yaml -t docs -o build/manual            # Markdown
isa-archive generate --isa my-isa/isa.yaml -t docs -f html -o build/manual   # HTML
isa-archive generate --isa my-isa/isa.yaml -t docs -f pdf  -o build/manual   # PDF
isa-archive generate --isa my-isa/isa.yaml -t docs -f all  -o build/manual   # all three
```

Each format is also addressable as a sub-target - `-t docs-md`, `-t docs-html`,
`-t docs-pdf` - the form a [Project](../yaml/project.md) manifest uses to pin one
format per output path.

**Preview the bundled examples.** `bash examples/view-docs.sh` generates the HTML
manuals for pico32 and npu-probe and serves them on `http://localhost:8000`
(pass a port, e.g. `bash examples/view-docs.sh 9000`).

Produces `{isa}_reference.md` / `.html` / `.pdf` - a human-readable
architecture manual generated from the same manifests as the toolchain:

- per-instruction pages: encoding diagram (bit positions from the
  [schema](../yaml/schemas.md)), operands, and semantics,
- the register files and their ABI aliases,
- CSR tables with field layouts and access modes,
- operand-struct layouts.

The prose comes from your `description` fields - `metadata.description` for
the one-liner, `spec.description` for the semantics line:

```yaml
metadata:
  name: BEQ
  description: Branch if equal
spec:
  ...
  description: "if rs1 == rs2: pc ← pc + sext(offset)"
```

Write them as you go and the manual stays current for free; the
[quickstart](../getting-started/quickstart.md#2-generate-a-reference-manual)
generates one for the bundled RISC-V example in one command.

## Current boundaries

- PDF output uses WeasyPrint (installed with the tool); complex/very large
  ISAs render noticeably slower as PDF than HTML.
- The manual reflects what the manifests declare - behaviors are shown as
  written, not decompiled into prose.
