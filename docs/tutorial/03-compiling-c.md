# Part 3 — Compiling C

No new instructions this part. The thirteen we have are enough — what's
missing is *meaning*: which registers hold arguments, which instruction
adjusts the stack, how to build a constant. We declare that, generate an LLVM
backend, build it once, and compile real C.

Finished state: [`examples/tutorial/pico32-part3/`](../../examples/tutorial/pico32-part3/).

## What a C compiler needs from an ISA

The checklist (this is the `c-baremetal` contract — [details](../compiler/README.md#what-complete-means-target-profiles)):

1. **ABI names** — `zero`, `ra`, `sp`, argument and saved registers.
2. **Roles** — which instruction is the add, the word load, the stack
   adjuster, the constant builder. Mostly inferred; a few declared.
3. **An object-format identity** — what the ELF files claim to be, so a
   linker will touch them.

## 1. Name the registers

In `isa.yaml`, give the register file its ABI aliases — and add the profile
declaration while we're here:

```yaml
spec:
  compiler:
    profile: c-baremetal      # our ambition, checked by the coverage report

  state:
    registers:
      - name: gpr
        width: 32
        count: 32
        zero_register: 0
        canonical_prefix: r
        aliases:
          zero: 0             # hardwired zero
          ra:   1             # return address
          sp:   2             # stack pointer
          t0:   5             # temporaries
          t1:   6
          t2:   7
          s0:   8             # saved / frame pointer
          s1:   9
          a0:   10            # arguments / return values
          a1:   11
          a2:   12
          a3:   13

  abi:
    stack_alignment: 16
    arg_registers: [a0, a1, a2, a3]
    ret_registers: [a0, a1]
    callee_saved: [ra, sp, s0, s1]
    frame_pointer: s0
```

Aliases are never invented for you — no `sp` alias, no stack, and the
coverage report would say `missing: alias:sp`. Side benefit: the QEMU board
now pre-loads `sp` with the top of RAM at reset, so C can run without
assembly startup code.

## 2. Tag the roles

Most roles the generator **infers from behaviors** you already wrote:
`rd = rs1 + rs2` is the add, `mem32[...]` loads, the branch conditions fall
out of the `if`s, `JAL`/`JALR` are the jumps. Three conventions can't be
inferred, because they're choices, not semantics:

```yaml
# LUI — "use me for the top half of constants and addresses"
  compiler:
    roles: [const.hi, global.hi]

# ADDI — "…and me for the low half, and for stack adjustment"
  compiler:
    roles: [const.lo, global.lo, frame.sp_adjust]

# JAL / JALR — calling convention duties
  compiler:
    roles: [control.jump, control.call]          # on JAL
  compiler:
    roles: [control.call_indirect, control.ret]  # on JALR
```

Remember part 1's `hello.s` building `0x5555` with `LUI`+`ADDI`? You just
taught the compiler that exact idiom — from ADDI's sign-extending behavior it
infers the `hi_lo_add` materialization strategy
([how that works](../compiler/roles-and-coverage.md#constant-materialization--a-worked-contrast)).

## 3. Declare the object format

```yaml
spec:
  triple_arch: riscv32        # register under this LLVM triple
  elf_machine: 243            # EM_RISCV
  nop_encoding: "00000013"    # our NOP: ADDI r0, r0, 0
```

This cashes in [the decision from the overview](README.md#one-design-decision-made-up-front):
pico32's field placements match RISC-V's, so we borrow its triple and
relocations, and stock LLD links our programs. The mnemonics, opcode values,
and register names in those object files are still entirely pico32.

## 4. Generate — and read your scorecard

```sh
$ isa-archive generate --isa pico32/isa.yaml -t llvm -o build/llvm-gen --strict
PICO32: compiler backend COMPILER-COMPLETE for profile 'c-baremetal' (strategy=hi_lo_add)
```

Open `build/llvm-gen/llvm/lib/Target/PICO32/COMPILER_COVERAGE.md`:

```markdown
- **ALU rr**: add ✓  sub ✓  and ✗  or ✓  xor ✗  shl ✗  srl ✗  sra ✗
- **Memory**: load8s ✗  load8u ✗  ...  load32 ✓  ...  store32 ✓
- **Branch**: eq ✓  ne ✓  lt ✓  ge ✗  ltu ✓  geu ✗
- **Control**: jump ✓  call ✓  call_indirect ✓  ret ✓
- **Frame**: sp_adjust ✓
- **Const strategy**: `hi_lo_add`

**STATUS: COMPILER-COMPLETE ✓** (profile `c-baremetal`)
```

The ✗s are honest: pico32 has no AND, no shifts, no byte loads. COMPLETE
means the *required* core is there; C that needs the missing ops is
constrained accordingly (see boundaries below). `--strict` makes this a hard
gate — wire it into CI and an ISA edit can never silently break the
compiler. ([Reading the report in full.](../compiler/roles-and-coverage.md))

## 5. Build LLVM (one time, ~40–60 min)

```sh
git clone --depth=1 --branch llvmorg-18.1.8 \
    https://github.com/llvm/llvm-project.git llvm-src
bash build/llvm-gen/patch_llvm.sh llvm-src
cmake -S llvm-src/llvm -B llvm-build -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_TARGETS_TO_BUILD=PICO32 \
    -DLLVM_ENABLE_PROJECTS="clang;lld" \
    -DLLVM_ENABLE_ASSERTIONS=OFF \
    -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF -DLLVM_INCLUDE_DOCS=OFF
ninja -C llvm-build clang llc lld
```

Long coffee. (`examples/demo/02_build_llvm.sh` is the scripted equivalent.)
The result is a clang whose **only** backend is pico32.

## 6. Compile and run

The snapshot's `programs/` has a minimal bare-metal trio, commented:
`start.c` (calls `main`, exits through the power switch), `fib.c` (computes
fib(10), prints it on the UART), `link.ld` (puts `.text` at `ram_base`).

```sh
$ llvm-build/bin/clang --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 \
      -nostdlib -ffreestanding -O1 -fuse-ld=lld \
      -T pico32/programs/link.ld pico32/programs/start.c pico32/programs/fib.c \
      -o fib.elf

$ qemu-build/qemu-system-pico32 -M pico32-virt -display none -serial stdio \
      -monitor none -bios none -kernel fib.elf
fib(10) = 55
$ echo $?
0
```

C source → your compiler → your linker → your simulator → correct answer.
Look at what the compiler actually wrote — `clang -S` shows pure pico32, your
thirteen instructions and `r`-registers doing register allocation, loops, and
calling convention:

```asm
fib:
	addi	r3, r0, 2
	blt	r10, r3, .LBB0_3
	jal	r0, .LBB0_1
.LBB0_1:
	addi	r3, r10, -1
	addi	r4, r0, 0
	addi	r5, r0, 1
...
.LBB0_3:
	jalr	r0, r1, 0
```

## Current boundaries (of this compiler)

- **The C you compile is bounded by your ISA.** pico32 deliberately has a
  small instruction set, and the compiler can only build C from instructions
  you gave it. Multiply/divide (`*`, `/`) become compiler-runtime calls
  (`__mulsi3`) — a clear *link* error under `-nostdlib` until Part 4 adds
  `MUL`. Bitwise AND and shifts have no pico32 instruction, so C that needs
  them (including patterns the optimizer rewrites *into* a shift, like the
  signed `x < 0` sign-test) won't compile. The snapshot's `fib.c` deliberately
  sticks to what the ISA can do.
- **Comparisons as values do work** — `int flag = (a < b);` and the other
  nine comparison forms compile, even though pico32 has no set-less-than
  instruction: the backend builds the 0/1 result with the same conditional
  branches it uses for control flow. (An ISA *with* a set-less-than
  instruction uses that directly instead.)
- Byte/halfword memory access is synthesized from word accesses (pico32 has
  no `LB`/`SB`) — correct, just not compact.
- `-march=rv32i -mabi=ilp32` configure clang's *driver* for the borrowed
  triple; code generation is entirely your backend.
- The full linking story and the LLVM version pin:
  [the compiler build guide](../compiler/build-and-use.md).

[**Part 4: growing the ISA →**](04-growing-the-isa.md)
