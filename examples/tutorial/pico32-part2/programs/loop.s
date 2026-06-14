# loop.s — the first pico32 loop: print the alphabet.
# Demonstrates BNE with a backward branch target (a label).

.text
    lui   r1, 0x10000       # r1 = UART base
    addi  r2, r0, 65        # r2 = 'A'
    addi  r3, r0, 91        # r3 = one past 'Z'

print_loop:
    sw    r1, r2, 0         # UART ← current letter
    addi  r2, r2, 1         # next letter
    bne   r2, r3, print_loop

    addi  r2, r0, 10        # '\n'
    sw    r1, r2, 0

    # power off (sifive_test: 0x5555 = exit 0)
    lui   r4, 0x100
    lui   r5, 0x5
    addi  r5, r5, 0x555
    sw    r4, r5, 0
