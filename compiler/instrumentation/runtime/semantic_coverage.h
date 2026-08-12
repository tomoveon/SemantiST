#ifndef SemantiST_SEMANTIC_COVERAGE_H
#define SemantiST_SEMANTIC_COVERAGE_H

#include <stdint.h>

void __semantist_semantic_hit(uint32_t runtime_id);
void __semantist_semantic_hazard(uint32_t runtime_id, uint8_t violation);
void __semantist_semantic_cycle(uint32_t cycle);

#endif
