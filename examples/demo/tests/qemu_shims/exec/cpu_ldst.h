#pragma once
/* Memory-access shims backed by sim_memory[] defined in qemu_test_harness.c. */
#include <stdint.h>
#include <string.h>

extern uint8_t sim_memory[];
#define SIM_MASK (4u * 1024u * 1024u - 1u)
#define GETPC()  ((uintptr_t)0)

__attribute__((unused)) static uint8_t
cpu_ldub_data_ra(void *e, uint64_t a, uintptr_t r) {
    (void)e; (void)r; return sim_memory[a & SIM_MASK];
}
__attribute__((unused)) static uint16_t
cpu_lduw_data_ra(void *e, uint64_t a, uintptr_t r) {
    (void)e; (void)r; uint16_t v; memcpy(&v, &sim_memory[a & SIM_MASK], 2); return v;
}
__attribute__((unused)) static uint32_t
cpu_ldl_data_ra(void *e, uint64_t a, uintptr_t r) {
    (void)e; (void)r; uint32_t v; memcpy(&v, &sim_memory[a & SIM_MASK], 4); return v;
}
__attribute__((unused)) static uint64_t
cpu_ldq_data_ra(void *e, uint64_t a, uintptr_t r) {
    (void)e; (void)r; uint64_t v; memcpy(&v, &sim_memory[a & SIM_MASK], 8); return v;
}
__attribute__((unused)) static void
cpu_stb_data_ra(void *e, uint64_t a, uint8_t  v, uintptr_t r) {
    (void)e; (void)r; sim_memory[a & SIM_MASK] = v;
}
__attribute__((unused)) static void
cpu_stw_data_ra(void *e, uint64_t a, uint16_t v, uintptr_t r) {
    (void)e; (void)r; memcpy(&sim_memory[a & SIM_MASK], &v, 2);
}
__attribute__((unused)) static void
cpu_stl_data_ra(void *e, uint64_t a, uint32_t v, uintptr_t r) {
    (void)e; (void)r; memcpy(&sim_memory[a & SIM_MASK], &v, 4);
}
__attribute__((unused)) static void
cpu_stq_data_ra(void *e, uint64_t a, uint64_t v, uintptr_t r) {
    (void)e; (void)r; memcpy(&sim_memory[a & SIM_MASK], &v, 8);
}
