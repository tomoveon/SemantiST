#ifndef SemantiST_SEMANTIC_COVERAGE_H
#define SemantiST_SEMANTIC_COVERAGE_H

#include <stdint.h>

void __semantist_semantic_hit(uint32_t runtime_id);
void __semantist_semantic_hazard(uint32_t runtime_id, uint8_t violation);
void __semantist_semantic_cycle(uint32_t cycle);
void __semantist_semantic_state(uint64_t before, uint64_t after);

/* Bounded per-execution telemetry, reset by the parent before each input.
 * Keep this layout in sync with fuzzer/semantic/observers.rs. */
#define SEMANTIST_TELEMETRY_EVENTS 4096
#define SEMANTIST_TELEMETRY_STATES 512
struct semantist_telemetry_event { uint32_t runtime_id, cycle; };
struct semantist_telemetry_state { uint64_t before, after; };
struct semantist_telemetry {
    uint32_t scan_count, event_count, state_count, flags;
    struct semantist_telemetry_event events[SEMANTIST_TELEMETRY_EVENTS];
    struct semantist_telemetry_state states[SEMANTIST_TELEMETRY_STATES];
};

#endif
