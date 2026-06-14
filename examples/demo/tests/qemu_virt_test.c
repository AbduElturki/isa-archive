/*
 * RV32I bare-metal test suite for QEMU -machine virt
 *
 * Each test uses inline assembly to directly exercise a specific RV32I
 * instruction and checks the result.  The same instruction categories
 * are covered by test_sim.py, so you can cross-validate our generator
 * output against real QEMU.
 *
 * Peripherals (QEMU virt):
 *   0x10000000 — NS16550 UART  (stdout)
 *   0x00100000 — SiFive test device (0x5555 = exit 0, 0x3333 = exit fail)
 */

typedef unsigned int  uint32_t;
typedef unsigned char uint8_t;

#define UART     ((volatile uint8_t  *)0x10000000)
#define TEST_DEV ((volatile uint32_t *)0x00100000)

/* ── UART driver ────────────────────────────────────────────────────────── */

static void uart_init(void) {
    UART[1] = 0x00;   /* disable interrupts           */
    UART[3] = 0x80;   /* DLAB=1: access baud divisor  */
    UART[0] = 0x01;   /* divisor low  (doesn't matter in simulation) */
    UART[1] = 0x00;   /* divisor high */
    UART[3] = 0x03;   /* 8N1, DLAB=0  */
    UART[2] = 0xC7;   /* enable+reset FIFOs           */
    UART[4] = 0x03;   /* DTR + RTS                    */
}

static void uart_putc(char c) {
    while (!(UART[5] & 0x20)) {}   /* wait for THRE */
    UART[0] = (uint8_t)c;
}

static void uart_puts(const char *s) { while (*s) uart_putc(*s++); }

static void uart_putu(uint32_t v) {
    char buf[11]; int i = 0;
    if (!v) { uart_putc('0'); return; }
    while (v) { buf[i++] = (char)('0' + v % 10); v /= 10; }
    while (i > 0) uart_putc(buf[--i]);
}

/* ── Test harness ───────────────────────────────────────────────────────── */

static int pass_count, fail_count;

static void check(const char *name, int ok) {
    uart_puts(ok ? "PASS " : "FAIL ");
    uart_puts(name);
    uart_putc('\n');
    if (ok) pass_count++; else fail_count++;
}

/* ── Tests ──────────────────────────────────────────────────────────────── */

static void run_tests(void) {
    /* ── Arithmetic ──────────────────────────────────────────────────── */
    { int r; asm("addi %0,x0,42"  : "=r"(r)); check("addi_positive",  r == 42); }
    { int r; asm("addi %0,x0,-7"  : "=r"(r)); check("addi_negative",  (unsigned)r == 0xFFFFFFF9u); }
    { int a=10,b=20,r; asm("add %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("add", r==30); }
    { int a=100,b=37,r; asm("sub %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("sub", r==63); }

    /* x0 is hardwired zero — writing must have no effect */
    { int r=0; asm volatile("addi x0,x0,99"); check("x0_hardwired", r==0); }

    /* ── Logical ─────────────────────────────────────────────────────── */
    { int a=0xFF,b=0x0F,r; asm("and  %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("and",  r==0x0F); }
    { int a=0xF0,b=0x0F,r; asm("or   %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("or",   r==0xFF); }
    { int a=0xFF,b=0x0F,r; asm("xor  %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("xor",  r==0xF0); }
    { int a=0xFF,r; asm("andi %0,%1,0x3F":"=r"(r):"r"(a)); check("andi", r==0x3F); }
    { int a=0xF0,r; asm("ori  %0,%1,0x0F":"=r"(r):"r"(a)); check("ori",  r==0xFF); }
    { int a=0xFF,r; asm("xori %0,%1,0x0F":"=r"(r):"r"(a)); check("xori", r==0xF0); }

    /* ── Shifts ──────────────────────────────────────────────────────── */
    { int a=1,r;    asm("slli %0,%1,8" :"=r"(r):"r"(a)); check("slli",    r==256); }
    { int a=0xFF,r; asm("srli %0,%1,4" :"=r"(r):"r"(a)); check("srli",    r==0x0F); }
    { int a=1,b=3,r;    asm("sll %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("sll_reg", r==8); }
    { int a=0x80,b=3,r; asm("srl %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("srl_reg", r==16); }

    /* ── Memory ──────────────────────────────────────────────────────── */
    { static int mem;
      int v=0x7FF; asm volatile("sw %1,%0":"=m"(mem):"r"(v));
      int r; asm volatile("lw %0,%1":"=r"(r):"m"(mem));
      check("sw_lw", r==0x7FF); }

    { static int mem2[2];   /* store 4 bytes into mem2[1] via base+4 */
      int v=0x42, r;
      int *p = &mem2[1];
      asm volatile("sw %1,0(%0)" :: "r"(p),"r"(v) : "memory");
      asm volatile("lw %0,0(%1)" : "=r"(r) : "r"(p));
      check("sw_lw_offset", r==0x42); }

    /* ── SLT / SLTU / SLTI ───────────────────────────────────────────── */
    { int a=-1,b=1,r; asm("slt  %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("slt_signed_neg", r==1); }
    { int a=5,b=3,r;  asm("slt  %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("slt_not_less",  r==0); }
    { unsigned a=0xFFFFFFFFu,b=1; int r;
      asm("sltu %0,%1,%2":"=r"(r):"r"(a),"r"(b)); check("sltu_unsigned", r==0); }
    { int a=-3,r; asm("slti %0,%1,0":"=r"(r):"r"(a)); check("slti_signed", r==1); }

    /* ── Branches ────────────────────────────────────────────────────── */
    { int r=0;
      asm volatile("beq x0,x0,1f\n j 2f\n 1: li %0,1\n 2:" : "=r"(r));
      check("beq_taken", r==1); }

    { int a=1,b=2,r=0;
      asm volatile("beq %1,%2,1f\n li %0,99\n 1:" : "+r"(r) : "r"(a),"r"(b));
      check("beq_not_taken", r==99); }

    { int a=3,b=7,r=0;
      asm volatile("bne %1,%2,1f\n j 2f\n 1: li %0,55\n 2:" : "=r"(r) : "r"(a),"r"(b));
      check("bne_taken", r==55); }

    { int a=5,b=5,r=0;
      asm volatile("bne %1,%2,1f\n li %0,11\n 1:" : "+r"(r) : "r"(a),"r"(b));
      check("bne_not_taken", r==11); }

    { int a=-1,b=1,r=0;   /* -1 < 1 signed → taken */
      asm volatile("blt %1,%2,1f\n j 2f\n 1: li %0,22\n 2:" : "=r"(r) : "r"(a),"r"(b));
      check("blt_signed_taken", r==22); }

    { unsigned a=0xFFFFFFFFu,b=1; int r=0;   /* 0xFFFF... >= 1 unsigned → bltu not taken */
      asm volatile("bltu %1,%2,1f\n li %0,33\n 1:" : "+r"(r) : "r"(a),"r"(b));
      check("bltu_not_taken", r==33); }

    { int a=10,b=5,r=0;
      asm volatile("bge %1,%2,1f\n j 2f\n 1: li %0,44\n 2:" : "=r"(r) : "r"(a),"r"(b));
      check("bge_signed_taken", r==44); }

    { unsigned a=0xFFFFFFFFu,b=1; int r=0;
      asm volatile("bgeu %1,%2,1f\n j 2f\n 1: li %0,66\n 2:" : "=r"(r) : "r"(a),"r"(b));
      check("bgeu_taken", r==66); }

    /* Loop: x = 3; do { x-- } while (x != 0) */
    { int x=3;
      asm volatile("1: addi %0,%0,-1\n bne %0,x0,1b" : "+r"(x));
      check("backward_branch_loop", x==0); }

    /* ── Jumps ───────────────────────────────────────────────────────── */
    { int r=0,ra=0;
      asm volatile("jal %1,1f\n j 2f\n 1: li %0,7\n 2:" : "=r"(r),"=r"(ra));
      check("jal_saves_return_addr", ra!=0 && r==7); }

    { int r=0;
      asm volatile("jal x0,1f\n j 2f\n 1: li %0,5\n 2:" : "=r"(r));
      check("jal_x0_plain_jump", r==5); }

    { int base,r=0,ra;
      asm volatile("la %0,1f\n jalr %2,%0,0\n j 2f\n 1: li %1,42\n 2:"
                   : "=r"(base),"=r"(r),"=r"(ra));
      check("jalr", r==42); }
}

/* ── Entry point ────────────────────────────────────────────────────────── */

void main(void) {
    uart_init();
    uart_puts("=== RV32I on qemu-system-riscv32 ===\n");
    run_tests();
    uart_puts("---\n");
    uart_putu((uint32_t)pass_count); uart_puts(" passed, ");
    uart_putu((uint32_t)fail_count); uart_puts(" failed\n");
    /* 0x5555 = clean exit, 0x3333 = failure */
    *TEST_DEV = fail_count ? 0x3333u : 0x5555u;
    while (1) {}
}
