# sum.s — store five words to RAM, load them back, sum them, check the answer.
# Demonstrates SW/LW round-trips, a counted loop, and BEQ.

.text
    lui   r1, 0x80001       # r1 = 0x80001000, scratch space in RAM

    # -- store 1,2,3,4,5 at r1[0..4] -------------------------------------
    addi  r2, r0, 1         # value
    addi  r3, r0, 6         # stop value
    addi  r4, r0, 0         # byte offset... kept in a register-free style:
store_loop:
    sw    r1, r2, 0         # mem[r1] = value
    addi  r1, r1, 4         # advance pointer
    addi  r2, r2, 1
    bne   r2, r3, store_loop

    # -- load them back and accumulate -----------------------------------
    lui   r1, 0x80001       # rewind pointer
    addi  r5, r0, 0         # r5 = sum
    addi  r6, r0, 5         # element count
sum_loop:
    lw    r7, r1, 0
    add   r5, r5, r7
    addi  r1, r1, 4
    addi  r6, r6, -1
    bne   r6, r0, sum_loop

    # -- check: 1+2+3+4+5 == 15 ------------------------------------------
    lui   r1, 0x10000       # UART
    addi  r8, r0, 15
    beq   r5, r8, ok
    addi  r2, r0, 78        # 'N'
    sw    r1, r2, 0
    addi  r2, r0, 79        # 'O'
    sw    r1, r2, 0
    jal   r0, done
ok:
    addi  r2, r0, 79        # 'O'
    sw    r1, r2, 0
    addi  r2, r0, 75        # 'K'
    sw    r1, r2, 0
done:
    addi  r2, r0, 10        # '\n'
    sw    r1, r2, 0

    # power off
    lui   r4, 0x100
    lui   r5, 0x5
    addi  r5, r5, 0x555
    sw    r4, r5, 0
