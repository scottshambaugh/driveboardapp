"""Behavioral firmware test via simavr: boot + serial protocol round-trip.

Runs the real compiled firmware ELF inside a simulated atmega328p and checks it
emits INFO_HELLO on boot and answers a CMD_SUPERSTATUS request with a
STATUS_END-terminated frame. This is "test on compiled C" - it executes the
firmware, not just builds it. Safety-specific firmware behavior (interlocks,
limits, watchdog) lives in test_firmware_safety.py.

Self-skips unless avr-gcc and libsimavr-dev are installed.
"""

import firmware_sim as fw

pytestmark = fw.skip_unless_available


def test_firmware_boots_and_answers_superstatus():
    out, hello = fw.run(send=fw.double([fw.CMD_SUPERSTATUS]))
    assert hello, "firmware did not emit INFO_HELLO on boot"
    assert fw.INFO_HELLO in out
    assert fw.STATUS_END in out, "no STATUS_END frame after a status request"
