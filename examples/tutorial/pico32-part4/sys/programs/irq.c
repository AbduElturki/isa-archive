/* irq.c - hardware-interrupt demo for the generated pico32sys-virt machine.
 *
 * Sets a trap vector, enables machine interrupts (mstatus.mie), then writes the
 * board's interrupt-test register (0x02000000) to raise the CPU's hard IRQ line.
 * The CPU vectors through mtvec to the ISR (mepc/mcause/mstatus saved by the
 * generated do_interrupt), which records the cause and deasserts the line; mret
 * then resumes here. PASS = the interrupt was taken with mcause.interrupt set.
 *
 * pico32sys uses RISC-V-compatible CSR encodings, so a stock riscv32 clang
 * assembles these CSR ops and the `interrupt` attribute's mret - but they need
 * Zicsr + the privileged spec, so build with -march=rv32i_zicsr:
 *
 *   clang --target=riscv32-unknown-elf -march=rv32i_zicsr -mabi=ilp32 \
 *         -nostdlib -ffreestanding -O1 -fuse-ld=lld -T link.ld irq.c -o irq.elf
 *   qemu-system-pico32sys -M pico32sys-virt -display none -bios none \
 *         -serial stdio -kernel irq.elf; echo "exit=$?"   # 0 = PASS
 *
 * (The PR CI gate builds fib.c on pico32; this interrupt demo is run by hand /
 * a dedicated job, since it needs the pico32sys machine.)
 */
#define MSTATUS_MIE  (1u << 3)
#define MCAUSE_IRQ   (1u << 31)

static volatile unsigned int *const IRQTEST = (volatile unsigned int *)0x02000000UL;
static volatile unsigned int *const EXITDEV = (volatile unsigned int *)0x00100000UL;

static volatile int g_handled;
static volatile unsigned int g_cause;

/* The `interrupt` attribute makes clang save/restore registers and return with
 * mret, so this is a complete machine-mode trap handler. */
__attribute__((interrupt("machine")))
static void isr(void) {
    unsigned int c;
    __asm__ volatile("csrr %0, mcause" : "=r"(c));
    g_cause = c;
    g_handled = 1;
    *IRQTEST = 0;            /* deassert the IRQ line */
}

__attribute__((section(".text.start"), noreturn))
void _start(void) {
    /* Direct-mode trap vector → the ISR; then enable machine interrupts. */
    __asm__ volatile("csrw mtvec, %0" :: "r"((unsigned long)&isr));
    __asm__ volatile("csrs mstatus, %0" :: "r"(MSTATUS_MIE));

    *IRQTEST = 1;           /* raise the interrupt */

    /* Let the interrupt be taken at the next instruction boundary. */
    for (volatile int i = 0; i < 1000 && !g_handled; i++) { }

    int ok = g_handled && (g_cause & MCAUSE_IRQ);
    *EXITDEV = ok ? 0x5555u : 0x3333u;   /* SiFive test: PASS / FAIL */
    for (;;) { }
}
