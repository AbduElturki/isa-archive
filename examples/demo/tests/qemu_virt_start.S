/* Minimal startup: set up stack, call main */
.section .text.start
.globl _start
_start:
    la   sp, _stack_top
    call main
    /* main() never returns (calls qemu_exit), but just in case */
    li   a0, 0x100000
    li   a1, 0x3333
    sw   a1, 0(a0)
1:  wfi
    j    1b
