/* KLEE semantic runtime: only the goal condition is observable. */
#include <klee/klee.h>
#include <stdint.h>

extern int __semantist_goal_id;

void __semantist_semantic_hazard(uint32_t runtime_id, uint8_t violation) {
    if (violation && (int)runtime_id == __semantist_goal_id) {
        klee_report_error(__FILE__, __LINE__, "semantist goal", "goal");
    }
}

void __semantist_semantic_hit(uint32_t runtime_id) { (void)runtime_id; }
void __semantist_semantic_cycle(uint32_t cycle) { (void)cycle; }
void __semantist_semantic_state(uint64_t before, uint64_t after) { (void)before; (void)after; }
void __semantist_semantic_reset(void) { }
