#pragma once
/* In real QEMU, helper-proto.h maps HELPER(x) to the TCG-registered name and
 * expands DEF_HELPER_FLAGS_N declarations into function prototypes.
 * Here we just give helpers a plain C name. */
#define HELPER(x) helper_##x
