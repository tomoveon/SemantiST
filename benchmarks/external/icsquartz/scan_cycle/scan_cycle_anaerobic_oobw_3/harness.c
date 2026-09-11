/* Auto-generated precise C harness for the RuSTy-compiled PLC_PRG wrapper.
 * The struct size and input field offsets below are derived from the LLVM IR
 * produced by compiling target.st + program.st.  Do not edit by hand; regenerate
 * with benchmarks/generate_benchmark_files.py. */

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#define SEMANTIST_INSTANCE_SIZE 72
#define SEMANTIST_INPUT_SIZE    16

#ifdef __cplusplus
extern "C" {
#endif
void PLC_PRG(void *instance);
void PLC_PRG__ctor(void *instance);
extern unsigned char PLC_PRG_instance[SEMANTIST_INSTANCE_SIZE];
#ifdef __cplusplus
}
#endif

/* ICSQuartz scan-cycle / sizing symbols. */
size_t PLC_PRG_instance_size = SEMANTIST_INSTANCE_SIZE;
size_t PLC_PRG_input_size = SEMANTIST_INPUT_SIZE;
unsigned scan_cycle = 0;
unsigned scan_cycle_max = 2;

static unsigned char semantist_fuzzer_instance[SEMANTIST_INSTANCE_SIZE];

uint8_t *program_state_fresh = PLC_PRG_instance + SEMANTIST_INPUT_SIZE;
uint8_t *program_state_start = semantist_fuzzer_instance + SEMANTIST_INPUT_SIZE;
uint8_t *program_state_end = semantist_fuzzer_instance + SEMANTIST_INSTANCE_SIZE;
unsigned program_state_size = SEMANTIST_INSTANCE_SIZE - SEMANTIST_INPUT_SIZE;

static unsigned char semantist_ptr_buf_2[32768];

int LLVMFuzzerTestOneInput(const uint8_t *Data, size_t Size) {
    static int semantist_initialized = 0;
    if (!semantist_initialized) {
        PLC_PRG__ctor(PLC_PRG_instance);
        memcpy(semantist_fuzzer_instance, PLC_PRG_instance, SEMANTIST_INSTANCE_SIZE);
        semantist_initialized = 1;
    }

    /* Scan-cycle model: state persists across executions; only the
     * input region is refreshed.  ICSQuartz's ScanCycleResetFeedback
     * copies program_state_fresh -> program_state_start on stale state. */
    scan_cycle++;
    memset(semantist_fuzzer_instance, 0, SEMANTIST_INPUT_SIZE);

    /* Write fuzz bytes into explicit input fields only (never padding). */
    size_t off = 0;
    if (off < Size) {
        size_t n = 1;
        if (n > Size - off) n = Size - off;
        memcpy(semantist_fuzzer_instance + 0, Data + off, n);
        off += n;
    }
    if (off < Size) {
        size_t n = 2;
        if (n > Size - off) n = Size - off;
        memcpy(semantist_fuzzer_instance + 2, Data + off, n);
        off += n;
    }
    if (off < Size) {
        size_t n = 32768;
        if (n > Size - off) n = Size - off;
        memcpy(semantist_ptr_buf_2, Data + off, n);
        memset(semantist_ptr_buf_2 + n, 0, 32768 - n);
        off += n;
    } else {
        memset(semantist_ptr_buf_2, 0, 32768);
    }
    { void *semantist_ptr = semantist_ptr_buf_2; memcpy(semantist_fuzzer_instance + 8, &semantist_ptr, sizeof(semantist_ptr)); }

    PLC_PRG(semantist_fuzzer_instance);

    /* Consume the output/state region so the run is not dead-code eliminated. */
    volatile uint8_t sink = 0;
    for (size_t i = SEMANTIST_INPUT_SIZE; i < SEMANTIST_INSTANCE_SIZE; i++) {
        sink ^= semantist_fuzzer_instance[i];
    }
    (void)sink;
    return 0;
}

#ifndef SEMANTIST_HARNESS_NO_MAIN
int main(int argc, char **argv) {
    FILE *fp = stdin;
    if (argc >= 2) {
        fp = fopen(argv[1], "rb");
        if (!fp) {
            return 1;
        }
    }
    unsigned char *buf = (unsigned char *)malloc(1 << 20);
    if (!buf) {
        if (argc >= 2) fclose(fp);
        return 1;
    }
    size_t n = fread(buf, 1, 1 << 20, fp);
    if (argc >= 2) {
        fclose(fp);
    }
    LLVMFuzzerTestOneInput(buf, n);
    free(buf);
    return 0;
}
#endif
