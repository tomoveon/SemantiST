/* Calls one calibration case compiled by RuSTy and prints its value.
 * Usage: rusty_driver {int|real|str|oscat} ID */
#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern int64_t CALIB_INT(int16_t id);
extern double CALIB_REAL(int16_t id);
extern void CALIB_STR(char *result, int16_t id);
extern void CALIB_OSCAT_STR(char *result, int16_t id);

static void print_string(const char *value) {
    putchar('\'');
    for (const unsigned char *p = (const unsigned char *)value; *p; p++) {
        if (isprint(*p) && *p != '\'' && *p != '$') {
            putchar(*p);
        } else {
            printf("$%02X", *p); /* IEC 61131-3 string escape */
        }
    }
    puts("'");
}

int main(int argc, char **argv) {
    if (argc != 3) {
        fprintf(stderr, "usage: %s {int|real|str|oscat} ID\n", argv[0]);
        return 2;
    }
    int16_t id = (int16_t)atoi(argv[2]);
    char result[1024] = {0};
    if (strcmp(argv[1], "int") == 0) {
        printf("%lld\n", (long long)CALIB_INT(id));
    } else if (strcmp(argv[1], "real") == 0) {
        printf("%.9g\n", CALIB_REAL(id));
    } else if (strcmp(argv[1], "str") == 0) {
        CALIB_STR(result, id);
        print_string(result);
    } else if (strcmp(argv[1], "oscat") == 0) {
        CALIB_OSCAT_STR(result, id);
        print_string(result);
    } else {
        return 2;
    }
    return 0;
}
