#include "semantic_coverage.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/shm.h>

static unsigned char *semantist_semantic_map;
static size_t semantist_semantic_map_size;
static int semantist_semantic_initialized;
static uint32_t semantist_semantic_cycle_id;
static struct semantist_telemetry *semantist_telemetry;
static const char *semantist_runtime_trace_path;

static void *semantist_attach(const char *name) {
    const char *text = getenv(name);
    if (!text || !*text) return NULL;
    char *end = NULL;
    errno = 0;
    long id = strtol(text, &end, 10);
    if (errno || end == text || *end != '\0') return NULL;
    void *memory = shmat((int)id, NULL, 0);
    return memory == (void *)-1 ? NULL : memory;
}

static void semantist_semantic_initialize(void) {
    if (semantist_semantic_initialized) {
        return;
    }
    semantist_semantic_initialized = 1;
    semantist_telemetry = semantist_attach("SEMANTIST_TELEMETRY_SHM_ID");
    const char *trace_enabled = getenv("SEMANTIST_SEMANTIC_TRACE_RUNTIME");
    if (trace_enabled && strcmp(trace_enabled, "1") == 0) {
        semantist_runtime_trace_path = getenv("SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE");
    }

    const char *size_text = getenv("SEMANTIST_SEMANTIC_MAP_SIZE");
    semantist_semantic_map_size =
        size_text && *size_text ? (size_t)strtoull(size_text, NULL, 10) : 0;

    const char *shm_text = getenv("SEMANTIST_SEMANTIC_SHM_ID");
    if (!shm_text || !*shm_text || semantist_semantic_map_size == 0) {
        return;
    }
    char *end = NULL;
    errno = 0;
    long shm_id = strtol(shm_text, &end, 10);
    if (errno || end == shm_text || *end != '\0') {
        return;
    }
    void *memory = shmat((int)shm_id, NULL, 0);
    if (memory != (void *)-1) {
        semantist_semantic_map = (unsigned char *)memory;
    }
}

static void semantist_semantic_trace(uint32_t runtime_id) {
    const char *path = semantist_runtime_trace_path;
    if (!path || !*path) {
        return;
    }
    FILE *stream = fopen(path, "a");
    if (!stream) {
        return;
    }
    fprintf(
        stream,
        "{\"probe\":\"semantic_ir\",\"runtime_id\":%u,\"cycle\":%u}\n",
        runtime_id,
        semantist_semantic_cycle_id
    );
    fclose(stream);
}

void __semantist_semantic_hit(uint32_t runtime_id) {
    semantist_semantic_initialize();
    if (semantist_semantic_map && runtime_id < semantist_semantic_map_size) {
        unsigned char value = semantist_semantic_map[runtime_id];
        semantist_semantic_map[runtime_id] = value == 255 ? 255 : (unsigned char)(value + 1);
    }
    if (semantist_telemetry) {
        semantist_telemetry->flags |= 4; /* telemetry ABI is active */
        uint32_t index = semantist_telemetry->event_count;
        if (index < SEMANTIST_TELEMETRY_EVENTS) {
            semantist_telemetry->events[index].runtime_id = runtime_id;
            semantist_telemetry->events[index].cycle =
                semantist_telemetry->scan_count ? semantist_telemetry->scan_count - 1 : 0;
            semantist_telemetry->event_count = index + 1;
        } else {
            semantist_telemetry->flags |= 1;
        }
    }
    semantist_semantic_trace(runtime_id);
}

void __semantist_semantic_hazard(uint32_t runtime_id, uint8_t violation) {
    if (violation) {
        __semantist_semantic_hit(runtime_id);
    }
}

void __semantist_semantic_cycle(uint32_t cycle) {
    semantist_semantic_initialize();
    semantist_semantic_cycle_id = cycle;
    if (semantist_telemetry && semantist_telemetry->scan_count < UINT32_MAX) {
        semantist_telemetry->flags |= 4;
        semantist_telemetry->scan_count++;
    }
}

void __semantist_semantic_reset(void) {
    /* Map attachments survive across inputs; only per-input state is reset.
     * The fuzzer clears the maps and the telemetry header itself. */
    semantist_semantic_cycle_id = 0;
}

void __semantist_semantic_state(uint64_t before, uint64_t after) {
    semantist_semantic_initialize();
    if (!semantist_telemetry) return;
    semantist_telemetry->flags |= 4;
    uint32_t index = semantist_telemetry->state_count;
    if (index < SEMANTIST_TELEMETRY_STATES) {
        semantist_telemetry->states[index].before = before;
        semantist_telemetry->states[index].after = after;
        semantist_telemetry->state_count = index + 1;
    } else {
        semantist_telemetry->flags |= 2;
    }
}
