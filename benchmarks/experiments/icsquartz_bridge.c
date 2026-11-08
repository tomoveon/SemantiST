#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef ICS_INPUT_SIZE
#error ICS_INPUT_SIZE must match the Common PLC Harness ABI layout
#endif
#ifndef ICS_STATE_SIZE
#error ICS_STATE_SIZE must match the target FB state layout
#endif
#ifndef ICS_DEFAULT_INPUT_BYTES
#define ICS_DEFAULT_INPUT_BYTES {0}
#endif

/* The native driver reads a fixed input prefix and, for FBs, observes the
 * following state region.  The Common PLC Harness owns the actual ABI state. */
unsigned char PLC_PRG_instance[ICS_INPUT_SIZE + ICS_STATE_SIZE];
size_t PLC_PRG_instance_size = sizeof(PLC_PRG_instance);
size_t PLC_PRG_input_size = ICS_INPUT_SIZE;
static const unsigned char ics_default_input[ICS_INPUT_SIZE] = ICS_DEFAULT_INPUT_BYTES;
extern int common_harness_execute(const uint8_t *data, size_t len);
extern void common_harness_reset(void);

#if ICS_STATE_SIZE > 0
/* Only raw inputs since the last native state reset are retained. Capacity is
 * reused across cycles; objective metadata copies the history on a crash. */
#define ICS_SEQUENCE_LIMIT 100
#define ICS_FRAME_LIMIT (1u << 20)
struct ics_frame {
    uint8_t *bytes;
    size_t length;
    size_t capacity;
};
static struct ics_frame ics_history[ICS_SEQUENCE_LIMIT];
static size_t ics_history_count;
static int ics_history_complete = 1;

static void ics_record_input(const uint8_t *data, size_t len) {
    if (!ics_history_complete) return;
    if (ics_history_count == ICS_SEQUENCE_LIMIT || len > ICS_FRAME_LIMIT) {
        ics_history_complete = 0;
        return;
    }
    struct ics_frame *frame = &ics_history[ics_history_count];
    if (len > frame->capacity) {
        uint8_t *next = (uint8_t *)realloc(frame->bytes, len);
        if (!next) { ics_history_complete = 0; return; }
        frame->bytes = next;
        frame->capacity = len;
    }
    if (len) memcpy(frame->bytes, data, len);
    frame->length = len;
    ics_history_count++;
}

size_t ics_sequence_count(void) {
    return ics_history_complete ? ics_history_count : 0;
}
size_t ics_sequence_length(size_t index) {
    return index < ics_sequence_count() ? ics_history[index].length : 0;
}
const uint8_t *ics_sequence_data(size_t index) {
    return index < ics_sequence_count() ? ics_history[index].bytes : NULL;
}

extern void common_harness_get_state(uint8_t *output);
extern void common_harness_set_state(const uint8_t *input);
static unsigned char fresh_state[ICS_INPUT_SIZE > ICS_STATE_SIZE ? ICS_INPUT_SIZE : ICS_STATE_SIZE];
unsigned char *program_state_fresh = fresh_state;
unsigned char *program_state_start = PLC_PRG_instance + ICS_INPUT_SIZE;
unsigned char *program_state_end = PLC_PRG_instance + sizeof(PLC_PRG_instance);
size_t program_state_size = ICS_STATE_SIZE;
size_t scan_cycle;
size_t scan_cycle_max = 100;
#endif

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t len) {
#if ICS_STATE_SIZE > 0
    if (scan_cycle >= scan_cycle_max) {
        memcpy(program_state_start, fresh_state, ICS_STATE_SIZE);
        scan_cycle = 0;
    }
    if (scan_cycle == 0) {
        ics_history_count = 0;
        ics_history_complete = 1;
    }
    ics_record_input(data, len);
    common_harness_set_state(program_state_start);
    int result = common_harness_execute(data, len);
    common_harness_get_state(program_state_start);
    scan_cycle++;
    return result;
#else
    common_harness_reset();
    return common_harness_execute(data, len);
#endif
}

extern void libafl_main(void);

static int replay_file(const char *path) {
    FILE *stream = fopen(path, "rb");
    if (!stream) return 1;
    if (fseek(stream, 0, SEEK_END) != 0) { fclose(stream); return 1; }
    long end = ftell(stream);
    if (end < 0) { fclose(stream); return 1; }
    rewind(stream);
    size_t length = (size_t)end;
    uint8_t *data = (uint8_t *)malloc(length ? length : 1);
    if (!data) { fclose(stream); return 1; }
    size_t read = fread(data, 1, length, stream);
    fclose(stream);
    int result = read == length ? LLVMFuzzerTestOneInput(data, length) : 1;
    free(data);
    return result;
}

#if ICS_STATE_SIZE > 0
static int replay_sequence(const char *path) {
    FILE *stream = fopen(path, "rb");
    if (!stream) return 1;
    uint8_t header[12];
    if (fread(header, 1, sizeof(header), stream) != sizeof(header)
            || memcmp(header, "ICSQSEQ1", 8) != 0) { fclose(stream); return 1; }
    uint32_t count = (uint32_t)header[8] | ((uint32_t)header[9] << 8)
                   | ((uint32_t)header[10] << 16) | ((uint32_t)header[11] << 24);
    if (!count || count > ICS_SEQUENCE_LIMIT) { fclose(stream); return 1; }
    common_harness_reset();
    common_harness_get_state(fresh_state);
    memcpy(program_state_start, fresh_state, ICS_STATE_SIZE);
    scan_cycle = 0;
    int result = 0;
    for (uint32_t index = 0; index < count; index++) {
        uint8_t length_bytes[4];
        if (fread(length_bytes, 1, 4, stream) != 4) { result = 1; break; }
        uint32_t length = (uint32_t)length_bytes[0] | ((uint32_t)length_bytes[1] << 8)
                        | ((uint32_t)length_bytes[2] << 16) | ((uint32_t)length_bytes[3] << 24);
        if (length > ICS_FRAME_LIMIT) { result = 1; break; }
        uint8_t *data = (uint8_t *)malloc(length ? length : 1);
        if (!data) { result = 1; break; }
        if (fread(data, 1, length, stream) != length) result = 1;
        else result = LLVMFuzzerTestOneInput(data, length);
        free(data);
        if (result) break;
    }
    fclose(stream);
    return result;
}
#endif

int main(int argc, char **argv) {
    memcpy(PLC_PRG_instance, ics_default_input, ICS_INPUT_SIZE);
#if ICS_STATE_SIZE > 0
    common_harness_reset();
    common_harness_get_state(fresh_state);
    memcpy(program_state_start, fresh_state, ICS_STATE_SIZE);
#endif
    if (argc == 3 && strcmp(argv[1], "--replay") == 0) {
        return replay_file(argv[2]);
    }
#if ICS_STATE_SIZE > 0
    if (argc == 3 && strcmp(argv[1], "--replay-sequence") == 0) {
        return replay_sequence(argv[2]);
    }
#endif
    libafl_main();
    return 0;
}
