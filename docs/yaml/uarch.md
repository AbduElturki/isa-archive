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
  name: pico32-tiny
  description: A single-issue in-order pipeline
spec:
  isa: pico32                 # the ISA this implements (by metadata.name)

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

## Many implementations, one ISA

A uArch is separate from the ISA, so one ISA can have several — a small
single-issue core, a wider superscalar one with duplicated ALUs (`count: 2`)
and a pipelined multiplier (`latency: 4`) — each just a different `blocks:`
list over the same `isa:`. The tutorial ships
[`examples/tutorial/pico32-part4/uarch.yaml`](../../examples/tutorial/pico32-part4/uarch.yaml)
(the `pico32-tiny` block model above). Generate RTL from it with:

```sh
isa-archive generate --isa examples/tutorial/pico32-part4/isa.yaml \
    --uarch examples/tutorial/pico32-part4/uarch.yaml -t verilog -o build/rtl
```

## Current boundaries

- uArch data (latencies, issue width) shapes the generated RTL skeletons
  only — the QEMU model stays purely functional and the compiler's scheduling
  model doesn't consume it yet.
