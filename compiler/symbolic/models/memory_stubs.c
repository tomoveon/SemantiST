/* KLEE models for the CODESYS memory compatibility runtime.
 *
 * The native runtime routes MemCpy/MemSet/BitCpy through checked shims that
 * abort on an out-of-bounds count.  KLEE has no ASan runtime, so the models
 * preserve the data movement but deliberately omit the abort: the native
 * executor is still the authority for hazard classification.  This keeps the
 * scan-cycle and memory benchmarks symbolically loadable.
 */
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

uint8_t *__SEMANTIST_MEMCPY_CHECKED(
    uint8_t *dest,
    uint64_t dest_size,
    const uint8_t *source,
    uint64_t source_size,
    uint32_t count) {
    (void)dest_size;
    (void)source_size;
    for (uint32_t i = 0; i < count; i++) {
        dest[i] = source[i];
    }
    return dest;
}

uint8_t *__SEMANTIST_MEMMOVE_CHECKED(
    uint8_t *dest,
    uint64_t dest_size,
    const uint8_t *source,
    uint64_t source_size,
    uint32_t count) {
    (void)dest_size;
    (void)source_size;
    if (dest < source) {
        for (uint32_t i = 0; i < count; i++) {
            dest[i] = source[i];
        }
    } else {
        for (uint32_t i = count; i > 0; i--) {
            dest[i - 1] = source[i - 1];
        }
    }
    return dest;
}

uint8_t *__SEMANTIST_MEMSET_CHECKED(
    uint8_t *dest,
    uint64_t dest_size,
    uint32_t value,
    uint32_t count) {
    (void)dest_size;
    for (uint32_t i = 0; i < count; i++) {
        dest[i] = (uint8_t)(value & 0xffu);
    }
    return dest;
}

uint8_t *__SEMANTIST_MEMCPY(uint8_t *dest, const uint8_t *source, uint32_t count) {
    return __SEMANTIST_MEMCPY_CHECKED(dest, 0, source, 0, count);
}

uint8_t *__SEMANTIST_MEMMOVE(uint8_t *dest, const uint8_t *source, uint32_t count) {
    return __SEMANTIST_MEMMOVE_CHECKED(dest, 0, source, 0, count);
}

uint8_t *__SEMANTIST_MEMSET(uint8_t *dest, uint32_t value, uint32_t count) {
    return __SEMANTIST_MEMSET_CHECKED(dest, 0, value, count);
}

bool __SEMANTIST_BITCPY(
    uint8_t *dest,
    uint16_t dest_start_bit,
    const uint8_t *source,
    uint16_t source_start_bit,
    uint16_t bit_count) {
    if (!dest || !source) {
        return false;
    }
    while (bit_count > 0) {
        uint16_t src_byte = (uint16_t)(source_start_bit / 8);
        uint16_t src_bit = (uint16_t)(source_start_bit % 8);
        uint16_t dst_byte = (uint16_t)(dest_start_bit / 8);
        uint16_t dst_bit = (uint16_t)(dest_start_bit % 8);
        uint16_t bits = (uint16_t)(8 - (src_bit > dst_bit ? src_bit : dst_bit));
        if (bits > bit_count) {
            bits = bit_count;
        }
        uint8_t src_mask = (uint8_t)(((1u << bits) - 1u) << src_bit);
        uint8_t dst_mask = (uint8_t)(((1u << bits) - 1u) << dst_bit);
        uint8_t value = (uint8_t)((source[src_byte] & src_mask) >> src_bit);
        dest[dst_byte] = (uint8_t)((dest[dst_byte] & ~dst_mask) | (value << dst_bit));
        dest_start_bit = (uint16_t)(dest_start_bit + bits);
        source_start_bit = (uint16_t)(source_start_bit + bits);
        bit_count = (uint16_t)(bit_count - bits);
    }
    return true;
}

void *calloc(size_t count, size_t size) {
    size_t total = count * size;
    void *memory = malloc(total);
    if (memory) {
        memset(memory, 0, total);
    }
    return memory;
}
