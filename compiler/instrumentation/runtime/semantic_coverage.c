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

static void semantist_semantic_initialize(void) {
    if (semantist_semantic_initialized) {
        return;
    }
    semantist_semantic_initialized = 1;

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
    const char *enabled = getenv("SEMANTIST_SEMANTIC_TRACE_RUNTIME");
    const char *path = getenv("SEMANTIST_SEMANTIC_RUNTIME_TRACE_FILE");
    if (!enabled || strcmp(enabled, "1") != 0 || !path || !*path) {
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
    semantist_semantic_trace(runtime_id);
}

void __semantist_semantic_hazard(uint32_t runtime_id, uint8_t violation) {
    if (violation) {
        __semantist_semantic_hit(runtime_id);
    }
}

void __semantist_semantic_cycle(uint32_t cycle) {
    semantist_semantic_cycle_id = cycle;
}
