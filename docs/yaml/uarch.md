# uArch — a micro-architecture for your ISA

An ISA says *what* instructions do; a uArch manifest says *what hardware
executes them*: functional blocks, their counts, latencies, and pipelining.
It's a separate kind so one ISA can have several implementations — an
in-order single-issue chassis and a superscalar one, from the same ISA.

Today the uArch manifest is consumed by the **Verilog generator**
(`-t verilog --uarch …`); see [the Verilog target](../targets/verilog.md).

```yaml
apiVersion: isa-archive/v1
kind: uArch
metadata:
  name: rv32i-classic
  description: Classic 5-stage in-order single-issue pipeline
spec:
  isa: rv32i                  # the ISA this implements (by metadata.name)

  blocks:
    - name: IntegerALU
      count: 1                # how many of these units exist
      latency: 1              # cycles
      pipelined: true         # can accept a new op every cycle
      handles:                # which instructions run here…
        - alu_int             # …matched by the instructions' exec_type tags
        - alu_branch
        - alu_jump
    - name: LoadStoreUnit
      count: 1
      latency: 2
      pipelined: true
      handles: [mem_load, mem_store]

  state:                      # implementation-only CSRs (cycle counters, …)
    csrs:
      - name: mcycle_impl
        address: 0xB00
        width: 64
        fields:
          - { name: count, start: 0, end: 63, access: rw }
```

## How instructions reach blocks

The link is the instruction's `exec_type` tag:

```yaml
# in the ISA:           # in the uArch:
spec:                   blocks:
  exec_type: alu_int      - name: IntegerALU
  ...                       handles: [alu_int]
```

Every instruction whose `exec_type` appears in a block's `handles` list is
implemented by that block's generated module.

## Two implementations, one ISA

Compare the bundled pair — same `isa: rv32i`, different machines:

- `examples/rv32/uarch/in-order.yaml` — one ALU, one load/store unit,
  single-issue.
- `examples/rv32/uarch/superscalar.yaml` — duplicated ALUs (`count: 2`), a
  pipelined multiplier with `latency: 4`, separate branch unit.

Generate either with:

```sh
isa-archive generate --isa examples/rv32/base/isa.yaml \
    --uarch examples/rv32/uarch/in-order.yaml -t verilog -o build/rtl
```

## Current boundaries

- uArch data (latencies, issue width) shapes the generated RTL skeletons
  only — the QEMU model stays purely functional and the compiler's scheduling
  model doesn't consume it yet.
