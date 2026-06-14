# Bare-metal startup for rv32i-virt machine.
# Sets up the stack, calls main, then exits via the SiFive test device.

.section .text.start
.global _start

_start:
    la   sp, _stack_top
    call main
    # a0 = return value from main
    bnez a0, _fail
    li   t0, 0x5555          # PASS code for sifive_test
    j    _exit
_fail:
    # Encode exit code: (code << 1) | 1, in upper 16 bits with 0x3333 marker
    slli a0, a0, 1
    ori  a0, a0, 1
    lui  t0, 0x3333
    or   t0, t0, a0
_exit:
    li   t1, 0x00100000      # sifive_test base address
    sw   t0, 0(t1)
    j    _exit               # halt (unreachable if device triggers)
