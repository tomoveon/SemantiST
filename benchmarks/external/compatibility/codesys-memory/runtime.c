#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define SEMANTIST_UNKNOWN_OBJECT_SIZE UINT64_MAX

void __asan_report_load_n(void *addr, size_t size) __attribute__((weak));
void __asan_report_store_n(void *addr, size_t size) __attribute__((weak));

static void semantist_report_memory_load(const uint8_t *addr, uint64_t size) {
    if (__asan_report_load_n) {
        __asan_report_load_n((void *)addr, (size_t)size);
    }
    abort();
}

static void semantist_report_memory_store(uint8_t *addr, uint64_t size) {
    if (__asan_report_store_n) {
        __asan_report_store_n(addr, (size_t)size);
    }
    abort();
}

static void semantist_check_load(const uint8_t *source, uint64_t source_size, uint32_t count) {
    if (source_size != SEMANTIST_UNKNOWN_OBJECT_SIZE && (uint64_t)count > source_size) {
        semantist_report_memory_load(source + source_size, (uint64_t)count - source_size);
    }
}

static void semantist_check_store(uint8_t *dest, uint64_t dest_size, uint32_t count) {
    if (dest_size != SEMANTIST_UNKNOWN_OBJECT_SIZE && (uint64_t)count > dest_size) {
        semantist_report_memory_store(dest + dest_size, (uint64_t)count - dest_size);
    }
}

uint8_t *__SEMANTIST_MEMSET_CHECKED(uint8_t *dest, uint64_t dest_size, uint32_t value, uint32_t count) {
    semantist_check_store(dest, dest_size, count);
    return memset(dest, (int)(value & 0xffu), count);
}

uint8_t *__SEMANTIST_MEMCPY_CHECKED(
    uint8_t *dest,
    uint64_t dest_size,
    const uint8_t *source,
    uint64_t source_size,
    uint32_t count
) {
    semantist_check_load(source, source_size, count);
    semantist_check_store(dest, dest_size, count);
    return memcpy(dest, source, count);
}

uint8_t *__SEMANTIST_MEMMOVE_CHECKED(
    uint8_t *dest,
    uint64_t dest_size,
    const uint8_t *source,
    uint64_t source_size,
    uint32_t count
) {
    semantist_check_load(source, source_size, count);
    semantist_check_store(dest, dest_size, count);
    return memmove(dest, source, count);
}

uint8_t *__SEMANTIST_MEMSET(uint8_t *dest, uint32_t value, uint32_t count) {
    return __SEMANTIST_MEMSET_CHECKED(dest, SEMANTIST_UNKNOWN_OBJECT_SIZE, value, count);
}

uint8_t *__SEMANTIST_MEMCPY(uint8_t *dest, const uint8_t *source, uint32_t count) {
    return __SEMANTIST_MEMCPY_CHECKED(
        dest,
        SEMANTIST_UNKNOWN_OBJECT_SIZE,
        source,
        SEMANTIST_UNKNOWN_OBJECT_SIZE,
        count
    );
}

uint8_t *__SEMANTIST_MEMMOVE(uint8_t *dest, const uint8_t *source, uint32_t count) {
    return __SEMANTIST_MEMMOVE_CHECKED(
        dest,
        SEMANTIST_UNKNOWN_OBJECT_SIZE,
        source,
        SEMANTIST_UNKNOWN_OBJECT_SIZE,
        count
    );
}


#include <stdbool.h>

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
        uint16_t src_byte = source_start_bit / 8;
        uint16_t src_bit = source_start_bit % 8;
        uint16_t dst_byte = dest_start_bit / 8;
        uint16_t dst_bit = dest_start_bit % 8;
        uint16_t bits = 8 - (src_bit > dst_bit ? src_bit : dst_bit);
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
