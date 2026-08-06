/*
 * Generic simavr harness for DriveboardFirmware safety tests.
 *
 * Loads a firmware ELF into a simulated atmega328p and lets the caller:
 *   - drive input pins HIGH on PORTC (limit switches) and PORTD (door/chiller),
 *   - inject raw bytes on the UART RX line (one at a time, spaced out),
 *   - idle for a while before sending (to exercise the serial watchdog),
 * then prints every byte the firmware transmitted on UART0.
 *
 * The duplicate-transmission protocol (each command byte sent twice) is NOT
 * applied here — the Python caller builds the exact byte stream it wants,
 * which keeps this harness a dumb, general transport.
 *
 * Usage:
 *   simavr_runner <firmware.elf> [--portc=B,B] [--portd=B,B]
 *                 [--send=N,N,...] [--idle-cycles=N] [--run-cycles=N]
 *                 [--watch-symbol=NAME]
 *
 * --watch-symbol samples an SRAM symbol (e.g. pwm_duty) every cycle and reports
 * the maximum and final value seen, so internal safety state (like the laser
 * duty being forced to 0) can be asserted.
 *
 * Output (stdout):
 *   OUT: <decimal bytes the firmware transmitted, space separated>
 *   HELLO=<0|1>
 *   SYM <name> max=<n> final=<n>   (only if --watch-symbol given)
 *
 * Built and driven by backend/tests/firmware_sim.py; needs libsimavr-dev.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "sim_avr.h"
#include "sim_elf.h"
#include "avr_uart.h"
#include "avr_ioport.h"
#include "sim_irq.h"

#define INFO_HELLO 0x7E /* '~' emitted by serial_init() on boot */
#define CAP_MAX 65536
#define RX_GAP_CYCLES 4000 /* > one UART byte time at 57600 baud, 16 MHz */
#define MAX_SEND 256
#define MAX_PINS 16

static uint8_t cap[CAP_MAX];
static int cap_len = 0;
static int got_hello = 0;

static long step_edges = 0; /* rising edges on the watched step pin */
static int step_last = 0;

static void uart_tx_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    uint8_t byte = value & 0xff;
    if (byte == INFO_HELLO)
        got_hello = 1;
    if (cap_len < CAP_MAX)
        cap[cap_len++] = byte;
}

static void step_pin_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    int lvl = value & 1;
    if (lvl && !step_last)
        step_edges++;
    step_last = lvl;
}

/* Level of a watched output pin: whether it was ever driven high, and where it
 * was left. Used for the assist relays, which have no SRAM state of their own. */
static int pin_ever_high = 0, pin_final = 0;

static void level_pin_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    pin_final = value & 1;
    if (pin_final)
        pin_ever_high = 1;
}

struct pinset {
    char port;
    int bit;
};

static int parse_ints(const char *s, int *out, int max) {
    int n = 0;
    while (s && *s && n < max) {
        out[n++] = (int)strtol(s, (char **)&s, 10);
        if (*s == ',')
            s++;
        else
            break;
    }
    return n;
}

static const char *opt_val(const char *arg, const char *key) {
    size_t klen = strlen(key);
    if (strncmp(arg, key, klen) == 0)
        return arg + klen;
    return NULL;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <firmware.elf> [opts]\n", argv[0]);
        return 2;
    }

    int portc_bits[MAX_PINS], portd_bits[MAX_PINS];
    int n_portc = 0, n_portd = 0;
    int send_bytes[MAX_SEND];
    int n_send = 0;
    long idle_cycles = 0;
    long run_cycles = 40000000L; /* ~2.5s at 16 MHz */
    const char *watch_name = NULL;
    int count_portb_bit = -1; /* count rising edges on this PORTB pin (step pin) */
    long portc_delay = 0;     /* apply PORTC pins this many cycles after hello */
    char watch_pin_port = 0;  /* watch the level of this output pin, e.g. D,4 */
    int watch_pin_bit = -1;

    for (int i = 2; i < argc; i++) {
        const char *v;
        if ((v = opt_val(argv[i], "--portc=")))
            n_portc = parse_ints(v, portc_bits, MAX_PINS);
        else if ((v = opt_val(argv[i], "--portd=")))
            n_portd = parse_ints(v, portd_bits, MAX_PINS);
        else if ((v = opt_val(argv[i], "--send=")))
            n_send = parse_ints(v, send_bytes, MAX_SEND);
        else if ((v = opt_val(argv[i], "--idle-cycles=")))
            idle_cycles = strtol(v, NULL, 10);
        else if ((v = opt_val(argv[i], "--run-cycles=")))
            run_cycles = strtol(v, NULL, 10);
        else if ((v = opt_val(argv[i], "--watch-symbol=")))
            watch_name = v;
        else if ((v = opt_val(argv[i], "--count-portb=")))
            count_portb_bit = (int)strtol(v, NULL, 10);
        else if ((v = opt_val(argv[i], "--portc-delay=")))
            portc_delay = strtol(v, NULL, 10);
        else if ((v = opt_val(argv[i], "--watch-pin="))) {
            /* form: <port letter>,<bit> e.g. D,4 */
            watch_pin_port = v[0];
            watch_pin_bit = (v[1] == ',') ? (int)strtol(v + 2, NULL, 10) : -1;
        }
        else
            fprintf(stderr, "WARN: ignoring unknown arg %s\n", argv[i]);
    }

    elf_firmware_t f;
    memset(&f, 0, sizeof(f));
    if (elf_read_firmware(argv[1], &f) != 0) {
        fprintf(stderr, "ERROR: cannot read ELF %s\n", argv[1]);
        return 2;
    }

    avr_t *avr = avr_make_mcu_by_name("atmega328p");
    if (!avr)
        avr = avr_make_mcu_by_name("atmega328");
    if (!avr) {
        fprintf(stderr, "ERROR: atmega328p core not in simavr\n");
        return 2;
    }
    avr_init(avr);
    avr->frequency = 16000000;
    avr_load_firmware(avr, &f);

    avr_irq_t *uart_tx = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'), UART_IRQ_OUTPUT);
    avr_irq_t *uart_rx = avr_io_getirq(avr, AVR_IOCTL_UART_GETIRQ('0'), UART_IRQ_INPUT);
    if (!uart_tx || !uart_rx) {
        fprintf(stderr, "ERROR: no UART0 irqs\n");
        return 2;
    }
    avr_irq_register_notify(uart_tx, uart_tx_hook, NULL);

    if (count_portb_bit >= 0)
        avr_irq_register_notify(
            avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('B'), count_portb_bit),
            step_pin_hook, NULL);

    if (watch_pin_bit >= 0)
        avr_irq_register_notify(
            avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ(watch_pin_port), watch_pin_bit),
            level_pin_hook, NULL);

    /* Resolve a watched SRAM symbol (e.g. pwm_duty) to its data-space address. */
    int watch_addr = -1;
    int watch_max = 0, watch_final = 0;
    if (watch_name) {
        for (uint32_t s = 0; s < f.symbolcount; s++) {
            if (strcmp(f.symbol[s]->symbol, watch_name) == 0) {
                watch_addr = f.symbol[s]->addr & 0xffff; /* strip 0x800000 data offset */
                break;
            }
        }
        if (watch_addr < 0) {
            fprintf(stderr, "ERROR: symbol %s not found\n", watch_name);
            return 2;
        }
    }

    int portd_applied = 0;
    int portc_applied = 0;
    int send_idx = 0;
    long next_send_cycle = -1;
    long hello_cycle = -1;

    for (long i = 0; i < run_cycles; i++) {
        int state = avr_run(avr);
        if (state == cpu_Done || state == cpu_Crashed) {
            fprintf(stderr, "ERROR: cpu stopped (state %d)\n", state);
            break;
        }
        if (watch_addr >= 0) {
            int v = avr->data[watch_addr];
            if (v > watch_max)
                watch_max = v;
            watch_final = v;
        }
        if (!got_hello)
            continue;
        if (hello_cycle < 0) {
            hello_cycle = i;
            next_send_cycle = i + idle_cycles;
        }
        /* PORTD (door/chiller) pins applied right after boot. */
        if (!portd_applied) {
            for (int p = 0; p < n_portd; p++)
                avr_raise_irq(
                    avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('D'), portd_bits[p]), 1);
            portd_applied = 1;
        }
        /* PORTC (limit) pins applied after the optional delay, so a limit can
         * be tripped mid-move rather than only at boot. */
        if (!portc_applied && i >= hello_cycle + portc_delay) {
            for (int p = 0; p < n_portc; p++)
                avr_raise_irq(
                    avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('C'), portc_bits[p]), 1);
            portc_applied = 1;
        }
        /* Feed the raw byte stream, one byte per RX_GAP window. */
        if (send_idx < n_send && i >= next_send_cycle) {
            avr_raise_irq(uart_rx, send_bytes[send_idx] & 0xff);
            send_idx++;
            next_send_cycle = i + RX_GAP_CYCLES;
        }
    }

    printf("OUT:");
    for (int i = 0; i < cap_len; i++)
        printf(" %d", cap[i]);
    printf("\n");
    printf("HELLO=%d\n", got_hello);
    if (watch_name)
        printf("SYM %s max=%d final=%d\n", watch_name, watch_max, watch_final);
    if (count_portb_bit >= 0)
        printf("STEPS=%ld\n", step_edges);
    if (watch_pin_bit >= 0)
        printf("PIN %c%d everhigh=%d final=%d\n", watch_pin_port, watch_pin_bit,
               pin_ever_high, pin_final);
    return 0;
}
