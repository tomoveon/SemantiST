/* Minimal freestanding libc surface for the KLEE harness core. */
#include <klee/klee.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <errno.h>
#include <limits.h>
#include <float.h>

typedef struct _FILE FILE;

FILE *fopen(const char *path, const char *mode) { (void)path; (void)mode; return (FILE *)0; }
int fclose(FILE *stream) { (void)stream; return 0; }
int fflush(FILE *stream) { (void)stream; return 0; }
int fprintf(FILE *stream, const char *format, ...) { (void)stream; (void)format; return 0; }
int printf(const char *format, ...) { (void)format; return 0; }
int fputc(int ch, FILE *stream) { (void)stream; return ch; }
int fputs(const char *text, FILE *stream) { (void)text; (void)stream; return 0; }
size_t fwrite(const void *data, size_t size, size_t count, FILE *stream) {
    (void)data; (void)size; (void)stream; return count;
}
char *getenv(const char *name) { (void)name; return (char *)0; }
int atoi(const char *text) { (void)text; return 0; }
void abort(void) { klee_report_error(__FILE__, __LINE__, "abort", "abort"); }
char *strdup(const char *text) {
    if (!text) return (char *)0;
    size_t length = 0;
    while (text[length]) length++;
    char *copy = (char *)malloc(length + 1);
    if (!copy) return (char *)0;
    for (size_t i = 0; i <= length; i++) copy[i] = text[i];
    return copy;
}
char *strchr(const char *text, int ch) {
    if (!text) return (char *)0;
    while (*text) {
        if (*text == (char)ch) return (char *)text;
        text++;
    }
    return ch == 0 ? (char *)text : (char *)0;
}
int strcmp(const char *left, const char *right) {
    while (*left && *left == *right) { left++; right++; }
    return (int)(unsigned char)*left - (int)(unsigned char)*right;
}
char *strtok_r(char *text, const char *delimiters, char **save) {
    (void)delimiters;
    if (text) *save = text;
    if (!save || !*save) return (char *)0;
    char *start = *save;
    while (*start) {
        char *cursor = start;
        while (*cursor && *cursor != '\n') cursor++;
        if (*cursor == '\n') {
            *cursor = 0;
            *save = cursor + 1;
        } else {
            *save = (char *)0;
        }
        return start;
    }
    return (char *)0;
}

size_t strlen(const char *text) {
    size_t length = 0;
    while (text && text[length]) length++;
    return length;
}

long strtol(const char *text, char **end, int base) {
    (void)text; (void)base;
    if (end) *end = (char *)text;
    return 0;
}

int *__errno_location(void) {
    static int value;
    return &value;
}

static int decimal_digit(char ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

static unsigned long long parse_integer(const char *text, char **end, int base,
                                        unsigned long long limit, int *negative) {
    const char *start = text;
    while (*text == ' ' || *text == '\t' || *text == '\n' || *text == '\r') text++;
    *negative = *text == '-';
    if (*text == '+' || *text == '-') text++;
    if (base == 0) base = text[0] == '0' && (text[1] == 'x' || text[1] == 'X') ? 16 : 10;
    if (base == 16 && text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) text += 2;
    const char *digits = text;
    unsigned long long value = 0;
    int overflow = 0;
    for (;;) {
        int digit = decimal_digit(*text);
        if (digit < 0 || digit >= base) break;
        if (value > (limit - (unsigned)digit) / (unsigned)base) overflow = 1;
        else if (!overflow) value = value * (unsigned)base + (unsigned)digit;
        text++;
    }
    if (end) *end = (char *)(text == digits ? start : text);
    if (overflow) {
        errno = ERANGE;
        return limit;
    }
    return value;
}

unsigned long long strtoull(const char *text, char **end, int base) {
    int negative = 0;
    unsigned long long value = parse_integer(text, end, base, ULLONG_MAX, &negative);
    return negative ? 0ULL - value : value;
}

long long strtoll(const char *text, char **end, int base) {
    int negative = 0;
    unsigned long long value = parse_integer(text, end, base,
                                             (unsigned long long)LLONG_MAX + 1ULL, &negative);
    if (errno == ERANGE) return negative ? LLONG_MIN : LLONG_MAX;
    if (!negative && value > (unsigned long long)LLONG_MAX) {
        errno = ERANGE;
        return LLONG_MAX;
    }
    return negative ? (value == (unsigned long long)LLONG_MAX + 1ULL ? LLONG_MIN : -(long long)value)
                    : (long long)value;
}

/* Concrete decimal input adapter. Native cold replay checks every SAT model;
   queries using a REAL/LREAL field are marked approximate by the solver. */
double strtod(const char *text, char **end) {
    const char *start = text;
    while (*text == ' ' || *text == '\t' || *text == '\n' || *text == '\r') text++;
    int negative = *text == '-';
    if (*text == '+' || *text == '-') text++;
    double value = 0.0;
    int digits = 0;
    while (*text >= '0' && *text <= '9') {
        value = value * 10.0 + (double)(*text - '0');
        text++;
        digits++;
    }
    if (*text == '.') {
        text++;
        double factor = 0.1;
        while (*text >= '0' && *text <= '9') {
            value += (double)(*text - '0') * factor;
            factor *= 0.1;
            text++;
            digits++;
        }
    }
    if (!digits) {
        if (end) *end = (char *)start;
        return 0.0;
    }
    if (*text == 'e' || *text == 'E') {
        const char *exponent_start = text;
        const char *cursor = text + 1;
        int exponent_negative = *cursor == '-';
        if (*cursor == '+' || *cursor == '-') cursor++;
        if (*cursor >= '0' && *cursor <= '9') {
            int exponent = 0;
            while (*cursor >= '0' && *cursor <= '9') {
                exponent = exponent < 1000 ? exponent * 10 + (*cursor - '0') : 1000;
                cursor++;
            }
            if (exponent > 400) exponent = 400;
            for (int i = 0; i < exponent; i++) {
                value = exponent_negative ? value / 10.0 : value * 10.0;
            }
            text = cursor;
        } else {
            text = exponent_start;
        }
    }
    if (end) *end = (char *)text;
    if (value > DBL_MAX || value < -DBL_MAX) errno = ERANGE;
    return negative ? -value : value;
}

const unsigned short **__ctype_b_loc(void) {
    static const unsigned short table[384];
    static const unsigned short *pointer = table;
    return &pointer;
}
