# hello.s — the first pico32 program.
# Prints "H\n" on the UART, then powers the machine off.
#
# Device map (from isa.yaml's machine: block):
#   0x10000000  ns16550 UART   — writing a byte to offset 0 transmits it
#   0x00100000  sifive_test    — writing 0x5555 exits QEMU with status 0

.text
    lui   r1, 0x10000       # r1 = 0x10000000 (UART base)
    addi  r2, r0, 72        # r2 = 'H'
    sw    r1, r2, 0         # UART ← 'H'
    addi  r2, r0, 10        # r2 = '\n'
    sw    r1, r2, 0         # UART ← '\n'

    lui   r3, 0x100         # r3 = 0x00100000 (sifive_test)
    lui   r4, 0x5           # r4 = 0x5000
    addi  r4, r4, 0x555     # r4 = 0x5555 (the "pass, exit 0" magic value)
    sw    r3, r4, 0         # power off
