/* Freestanding models for IEC standard-library entry points that the target
 * module declares.  KLEE only reports a problem if an execution actually calls
 * one that is not modelled faithfully; the startup-seed path only needs the
 * pure integer/XOR/division subset. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <klee/klee.h>

extern uint64_t __semantist_vclock_ns(void);
extern uint8_t __semantist_timer_semantics;

int64_t TIME(void) { return (int64_t)__semantist_vclock_ns(); }

/* ST instance layout of TON/TOF/TP: vtable, IN, PT, Q, ET, __signal__,
 * __is_running__, then the 24-byte __BUFFER__ used here for the start time. */
struct SemantistTimer {
    uint64_t vtable;
    uint8_t input;
    uint8_t pad0[7];
    int64_t preset;
    uint8_t output;
    uint8_t pad1[7];
    int64_t elapsed;
    uint8_t signal;
    uint8_t running;
    uint8_t buffer[24];
};

extern uint64_t __semantist_vclock_ns(void);
extern uint8_t __semantist_timer_semantics;

static uint64_t timer_now(void) { return __semantist_vclock_ns(); }

static void timer_store_start(struct SemantistTimer *timer, uint64_t now) {
    memcpy(timer->buffer, &now, sizeof(now));
}

static uint64_t timer_load_start(const struct SemantistTimer *timer) {
    uint64_t start = 0;
    memcpy(&start, timer->buffer, sizeof(start));
    return start;
}

static void timer_start(struct SemantistTimer *timer) {
    timer_store_start(timer, timer_now());
    timer->running = 1;
    timer->elapsed = 0;
}

static int64_t timer_elapsed(struct SemantistTimer *timer) {
    uint64_t now = timer_now();
    uint64_t start = timer_load_start(timer);
    uint64_t elapsed = now >= start ? now - start : 0;
    if (elapsed > (uint64_t)timer->preset) {
        elapsed = (uint64_t)(timer->preset > 0 ? timer->preset : 0);
    }
    timer->elapsed = (int64_t)elapsed;
    return timer->elapsed;
}

static int timer_reached(const struct SemantistTimer *timer) {
    if (__semantist_timer_semantics == 1) {
        return timer->elapsed >= timer->preset;
    }
    return timer->elapsed > timer->preset;
}

void TON(void *pointer) {
    struct SemantistTimer *timer = (struct SemantistTimer *)pointer;
    if (!timer) return;
    int rising = timer->input && !timer->signal;
    if (timer->input) {
        if (!timer->running) {
            if (rising) {
                timer_start(timer);
                timer->output = (__semantist_timer_semantics == 1 && timer->preset == 0) ? 1 : 0;
            } else {
                timer->output = 1;
            }
        } else {
            timer_elapsed(timer);
            timer->output = timer_reached(timer) ? 1 : 0;
        }
    } else {
        timer->running = 0;
        timer->output = 0;
        timer->elapsed = 0;
        timer_store_start(timer, timer_now());
    }
    timer->signal = timer->input;
}

void TOF(void *pointer) {
    struct SemantistTimer *timer = (struct SemantistTimer *)pointer;
    if (!timer) return;
    int falling = !timer->input && timer->signal;
    if (timer->input) {
        timer->running = 0;
        timer->output = 1;
        timer->elapsed = 0;
    } else if (falling) {
        timer_start(timer);
        timer->output = 1;
    } else if (timer->running) {
        timer_elapsed(timer);
        timer->output = timer_reached(timer) ? 0 : 1;
    } else {
        timer->output = 0;
    }
    timer->signal = timer->input;
}

void TP(void *pointer) {
    struct SemantistTimer *timer = (struct SemantistTimer *)pointer;
    if (!timer) return;
    int rising = timer->input && !timer->signal;
    if (rising) {
        timer_start(timer);
        timer->output = 1;
    } else if (timer->running) {
        timer_elapsed(timer);
        timer->output = timer_reached(timer) ? 0 : 1;
        if (!timer->output && !timer->input) {
            timer->running = 0;
        }
    } else {
        timer->output = 0;
    }
    timer->signal = timer->input;
}

void TON_TIME(void *timer) { TON(timer); }
void TOF_TIME(void *timer) { TOF(timer); }
void TP_TIME(void *timer) { TP(timer); }
void TON_LTIME(void *timer) { TON(timer); }
void TOF_LTIME(void *timer) { TOF(timer); }
void TP_LTIME(void *timer) { TP(timer); }
void TON__ctor(void *timer) { (void)timer; }
void TOF__ctor(void *timer) { (void)timer; }
void TP__ctor(void *timer) { (void)timer; }

struct SemantistTrigger {
    uint64_t vtable;
    uint8_t clk;
    uint8_t output;
    uint8_t internal;
};

void R_TRIG(void *pointer) {
    struct SemantistTrigger *trigger = (struct SemantistTrigger *)pointer;
    if (!trigger) return;
    trigger->output = trigger->clk && (trigger->internal ^ trigger->clk);
    trigger->internal = trigger->clk;
}

void F_TRIG(void *pointer) {
    struct SemantistTrigger *trigger = (struct SemantistTrigger *)pointer;
    if (!trigger) return;
    trigger->output = !trigger->clk && (trigger->internal ^ trigger->clk);
    trigger->internal = trigger->clk;
}

static double havoc_real(const char *message) {
    double value = 0.0;
    klee_warning(message);
    klee_make_symbolic(&value, sizeof(value), "havoc_real");
    return value;
}

double ACOS__REAL(double x) { (void)x; return havoc_real("imprecise: ACOS__REAL"); }
double ASIN__REAL(double x) { (void)x; return havoc_real("imprecise: ASIN__REAL"); }
double ATAN__REAL(double x) { (void)x; return havoc_real("imprecise: ATAN__REAL"); }
double COS__REAL(double x) { (void)x; return havoc_real("imprecise: COS__REAL"); }
double SIN__REAL(double x) { (void)x; return havoc_real("imprecise: SIN__REAL"); }
double SIN__LREAL(double x) { (void)x; return havoc_real("imprecise: SIN__LREAL"); }
double TAN__REAL(double x) { (void)x; return havoc_real("imprecise: TAN__REAL"); }
/* Bounded, deterministic approximation for concrete KLEE float paths. KLEE
 * still concretizes floating point expressions, so native replay remains
 * mandatory and no negative result can establish completeness. */
float EXP__REAL(float x) {
    if (x != x) return x;
    if (x > 89.0f) return (float)1.0e39;
    if (x < -104.0f) return 0.0f;
    int negative = x < 0.0;
    double reduced = negative ? -x : x;
    unsigned squarings = 0;
    while (reduced > 0.5 && squarings < 8) {
        reduced *= 0.5;
        squarings++;
    }
    double term = 1.0;
    double sum = 1.0;
    for (unsigned i = 1; i <= 24; i++) {
        term *= reduced / (double)i;
        sum += term;
    }
    while (squarings--) sum *= sum;
    return (float)(negative ? 1.0 / sum : sum);
}
double LN__REAL(double x) { (void)x; return havoc_real("imprecise: LN__REAL"); }
double SQRT__REAL(double x) { (void)x; return havoc_real("imprecise: SQRT__REAL"); }
float ROUND__REAL(float x) { return (float)(int64_t)(x >= 0 ? x + 0.5f : x - 0.5f); }

double DWORD_TO_REAL(uint32_t x) {
    float value;
    memcpy(&value, &x, sizeof(value));
    return (double)value;
}

uint32_t REAL_TO_DWORD(double x) {
    float value = (float)x;
    uint32_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return bits;
}

uint32_t ROL__DWORD(uint32_t value, uint32_t count) {
    count &= 31u;
    return count == 0 ? value : (value << count) | (value >> (32u - count));
}

int32_t LIMIT__DINT(int32_t low, int32_t value, int32_t high) {
    if (value < low) return low;
    if (value > high) return high;
    return value;
}
double LIMIT__REAL(double low, double value, double high) {
    if (value < low) return low;
    if (value > high) return high;
    return value;
}
uint8_t MAX__BYTE(uint8_t a, uint8_t b) { return a > b ? a : b; }
uint8_t MIN__BYTE(uint8_t a, uint8_t b) { return a < b ? a : b; }
int32_t MAX__DINT(int32_t a, int32_t b) { return a > b ? a : b; }
int32_t MIN__DINT(int32_t a, int32_t b) { return a < b ? a : b; }
int16_t MAX__INT(int16_t a, int16_t b) { return a > b ? a : b; }
int16_t MIN__INT(int16_t a, int16_t b) { return a < b ? a : b; }
double MAX__REAL(double a, double b) { return a > b ? a : b; }
double MIN__REAL(double a, double b) { return a < b ? a : b; }

void *__ubsan_handle_local_out_of_bounds_abort(void *data, void *ptr) { (void)data; (void)ptr; return 0; }

int64_t DATE_AND_TIME_TO_DATE(int64_t value) { return value; }
int64_t DATE_AND_TIME_TO_TIME_OF_DAY(int64_t value) { return value; }
int32_t DAY_OF_WEEK(int64_t value) { (void)value; return (int32_t)(value % 7); }
int32_t LEN__STRING(const char *text) {
    int32_t length = 0;
    if (!text) return 0;
    for (size_t index = 0; text[index]; ++index) {
        if (((unsigned char)text[index] & 0xc0u) != 0x80u) length++;
    }
    return length;
}

int32_t LINT_TO_STRING_EXT(int64_t input, char *dest) {
    if (!dest) klee_report_error(__FILE__, __LINE__, "null string destination", "model.err");
    char digits[21];
    unsigned count = 0;
    uint64_t magnitude = input < 0 ? (uint64_t)(-(input + 1)) + 1u : (uint64_t)input;
    do {
        digits[count++] = (char)('0' + magnitude % 10u);
        magnitude /= 10u;
    } while (magnitude);
    unsigned output = 0;
    if (input < 0) dest[output++] = '-';
    while (count) dest[output++] = digits[--count];
    dest[output] = 0;
    return 0;
}

void CONCAT__STRING(char *dest, int32_t argc, const char *const *argv) {
    if (!dest || !argv) klee_report_error(__FILE__, __LINE__, "null string pointer", "model.err");
    size_t output = 0;
    for (int32_t argument = 0; argument < argc; ++argument) {
        const char *source = argv[argument];
        if (!source) klee_report_error(__FILE__, __LINE__, "null string argument", "model.err");
        for (size_t index = 0; source[index]; ++index) dest[output++] = source[index];
    }
    dest[output] = 0;
}

int32_t INSERT_EXT__STRING(const char *base, const char *insert, int32_t pos, char *dest) {
    if (!base || !insert || !dest || pos < 0)
        klee_report_error(__FILE__, __LINE__, "invalid string insertion", "model.err");
    size_t split = 0;
    int32_t characters = 0;
    while (base[split] && characters < pos) {
        ++split;
        while (base[split] && ((unsigned char)base[split] & 0xc0u) == 0x80u) ++split;
        ++characters;
    }
    if (characters != pos)
        klee_report_error(__FILE__, __LINE__, "string insertion position out of range", "model.err");
    size_t output = 0;
    for (size_t index = 0; index < split; ++index) dest[output++] = base[index];
    for (size_t index = 0; insert[index]; ++index) dest[output++] = insert[index];
    for (size_t index = split; base[index]; ++index) dest[output++] = base[index];
    dest[output] = 0;
    return 0;
}
int32_t MID_EXT__STRING(const char *text, int32_t start, int32_t length, char *dest) {
    if (!text || !dest || start < 0 || length < 0) return -1;
    int32_t index = 0;
    while (index < length && text[start + index]) {
        dest[index] = text[start + index];
        index++;
    }
    dest[index] = 0;
    return index;
}

static size_t model_string_length(const char *text) {
    size_t length = 0;
    if (!text) return 0;
    while (text[length]) length++;
    return length;
}

int32_t LEFT_EXT__STRING(const char *text, int32_t length, char *dest) {
    if (!text || !dest || length < 0) return -1;
    size_t available = model_string_length(text);
    size_t count = (size_t)length < available ? (size_t)length : available;
    for (size_t index = 0; index < count; index++) dest[index] = text[index];
    dest[count] = 0;
    return (int32_t)count;
}

int32_t RIGHT_EXT__STRING(const char *text, int32_t length, char *dest) {
    if (!text || !dest || length < 0) return -1;
    size_t available = model_string_length(text);
    size_t count = (size_t)length < available ? (size_t)length : available;
    size_t start = available - count;
    for (size_t index = 0; index < count; index++) dest[index] = text[start + index];
    dest[count] = 0;
    return (int32_t)count;
}

int32_t DELETE_EXT__STRING(const char *text, int32_t length, int32_t position,
                           char *dest) {
    if (!text || !dest || length < 0 || position < 1) return -1;
    size_t available = model_string_length(text);
    size_t start = (size_t)position - 1;
    if (start > available) start = available;
    size_t count = (size_t)length;
    if (count > available - start) count = available - start;
    size_t output = 0;
    for (size_t index = 0; index < start; index++) dest[output++] = text[index];
    for (size_t index = start + count; index < available; index++) dest[output++] = text[index];
    dest[output] = 0;
    return (int32_t)output;
}

int32_t REPLACE_EXT__STRING(const char *text, const char *replacement,
                            int32_t length, int32_t position, char *dest) {
    if (!text || !replacement || !dest || length < 0 || position < 1) return -1;
    size_t available = model_string_length(text);
    size_t start = (size_t)position - 1;
    if (start > available) start = available;
    size_t remove = (size_t)length;
    if (remove > available - start) remove = available - start;
    size_t output = 0;
    for (size_t index = 0; index < start; index++) dest[output++] = text[index];
    for (size_t index = 0; replacement[index]; index++) dest[output++] = replacement[index];
    for (size_t index = start + remove; index < available; index++) dest[output++] = text[index];
    dest[output] = 0;
    return (int32_t)output;
}

int32_t FIND__STRING(const char *text, const char *search) {
    if (!text || !search || !search[0]) return 0;
    size_t needle = model_string_length(search);
    for (size_t index = 0; text[index]; index++) {
        size_t offset = 0;
        while (offset < needle && text[index + offset]
               && text[index + offset] == search[offset]) offset++;
        if (offset == needle) return (int32_t)index + 1;
    }
    return 0;
}

uint8_t __STRING_EQUAL(int32_t argc, const char *const *argv) {
    if (argc < 2 || !argv || !argv[0] || !argv[1]) return 0;
    const char *left = argv[0];
    const char *right = argv[1];
    size_t index = 0;
    while (left[index] && right[index] && left[index] == right[index]) index++;
    return (uint8_t)(left[index] == right[index]);
}
