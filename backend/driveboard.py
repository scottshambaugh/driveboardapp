# -*- coding: UTF-8 -*-
import base64
import copy
import datetime
import io
import itertools
import json
import platform
import sys
import threading
import time

import serial
import serial.tools.list_ports
from config import conf, write_config_fields

if not conf["mill_mode"]:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow module missing, raster mode will fail.")

__author__ = "Stefan Hechenberger <stefan@nortd.com>"

################ SENDING PROTOCOL
CMD_STOP = chr(1)
CMD_RESUME = chr(2)
CMD_STATUS = chr(3)
CMD_SUPERSTATUS = chr(4)
CMD_CHUNK_PROCESSED = chr(5)
CMD_UNPAUSE = chr(7)
CMD_PAUSE = chr(8)
CMD_RASTER_DATA_START = chr(16)
CMD_RASTER_DATA_END = chr(17)
STATUS_END = chr(6)

CMD_NONE = "A"
CMD_LINE = "B"
CMD_DWELL = "C"
CMD_RASTER = "D"

CMD_REF_RELATIVE = "E"
CMD_REF_ABSOLUTE = "F"
CMD_REF_STORE = "G"
CMD_REF_RESTORE = "H"

CMD_HOMING = "I"
CMD_OFFSET_STORE = "J"
CMD_OFFSET_RESTORE = "K"

CMD_AIR_ENABLE = "L"
CMD_AIR_DISABLE = "M"
CMD_AUX_ENABLE = "N"
CMD_AUX_DISABLE = "O"

PARAM_TARGET_X = "x"
PARAM_TARGET_Y = "y"
PARAM_TARGET_Z = "z"
PARAM_FEEDRATE = "f"
PARAM_INTENSITY = "s"
PARAM_DURATION = "d"
PARAM_PIXEL_WIDTH = "p"
PARAM_OFFSET_X = "h"
PARAM_OFFSET_Y = "i"
PARAM_OFFSET_Z = "j"

# Bounds for the motion parameters. MAX_PARAM_VALUE is what the 28-bit wire
# encoding can carry, so anything beyond it is already meaningless. The feed
# rate floor keeps the planner off a zero step rate, and the dwell ceiling
# bounds how long the beam can sit burning in one spot.
MAX_PARAM_VALUE = 134217.727
MIN_FEEDRATE = 0.1  # mm/min
MAX_FEEDRATE = MAX_PARAM_VALUE  # mm/min
MAX_DWELL_SECONDS = 10.0

# How long an assist output stays on: one burn, one pass, or the whole job.
ASSIST_MODES = ("off", "feed", "pass", "job")

################


################ RECEIVING PROTOCOL

# status: error flags
ERROR_SERIAL_STOP_REQUEST = "!"
ERROR_RX_BUFFER_OVERFLOW = '"'

ERROR_LIMIT_HIT_X1 = "$"
ERROR_LIMIT_HIT_X2 = "%"
ERROR_LIMIT_HIT_Y1 = "&"
ERROR_LIMIT_HIT_Y2 = "*"
ERROR_LIMIT_HIT_Z1 = "+"
ERROR_LIMIT_HIT_Z2 = "-"

ERROR_INVALID_MARKER = "#"
ERROR_INVALID_DATA = ":"
ERROR_INVALID_COMMAND = "<"
ERROR_INVALID_PARAMETER = ">"
ERROR_TRANSMISSION_ERROR = "="
ERROR_SERIAL_WATCHDOG = ";"

# status: info flags
INFO_IDLE_YES = "A"
INFO_DOOR_OPEN = "B"
INFO_CHILLER_OFF = "C"
INFO_PAUSED = "D"

# status: info params
INFO_POS_X = "x"
INFO_POS_Y = "y"
INFO_POS_Z = "z"
INFO_VERSION = "v"
INFO_BUFFER_UNDERRUN = "w"
INFO_STACK_CLEARANCE = "u"

INFO_HELLO = "~"

INFO_OFFSET_X = "a"
INFO_OFFSET_Y = "b"
INFO_OFFSET_Z = "c"
# INFO_TARGET_X = 'd'
# INFO_TARGET_Y = 'e'
# INFO_TARGET_Z = 'f'
INFO_FEEDRATE = "g"
INFO_INTENSITY = "h"
INFO_DURATION = "i"
INFO_PIXEL_WIDTH = "j"
INFO_DEBUG = "k"
################

# reverse lookup for commands, for debugging
# NOTE: have to be in sync with above definitions
markers_tx = {
    chr(1): "CMD_STOP",
    chr(2): "CMD_RESUME",
    chr(3): "CMD_STATUS",
    chr(4): "CMD_SUPERSTATUS",
    chr(5): "CMD_CHUNK_PROCESSED",
    chr(7): "CMD_UNPAUSE",
    chr(8): "CMD_PAUSE",
    chr(16): "CMD_RASTER_DATA_START",
    chr(17): "CMD_RASTER_DATA_END",
    chr(6): "STATUS_END",
    "A": "CMD_NONE",
    "B": "CMD_LINE",
    "C": "CMD_DWELL",
    "D": "CMD_RASTER",
    "E": "CMD_REF_RELATIVE",
    "F": "CMD_REF_ABSOLUTE",
    "G": "CMD_REF_STORE",
    "H": "CMD_REF_RESTORE",
    "I": "CMD_HOMING",
    "J": "CMD_OFFSET_STORE",
    "K": "CMD_OFFSET_RESTORE",
    "L": "CMD_AIR_ENABLE",
    "M": "CMD_AIR_DISABLE",
    "N": "CMD_AUX_ENABLE",
    "O": "CMD_AUX_DISABLE",
    "x": "PARAM_TARGET_X",
    "y": "PARAM_TARGET_Y",
    "z": "PARAM_TARGET_Z",
    "f": "PARAM_FEEDRATE",
    "s": "PARAM_INTENSITY",
    "d": "PARAM_DURATION",
    "p": "PARAM_PIXEL_WIDTH",
    "h": "PARAM_OFFSET_X",
    "i": "PARAM_OFFSET_Y",
    "j": "PARAM_OFFSET_Z",
}

markers_rx = {
    chr(1): "CMD_STOP",
    chr(2): "CMD_RESUME",
    chr(3): "CMD_STATUS",
    chr(4): "CMD_SUPERSTATUS",
    chr(5): "CMD_CHUNK_PROCESSED",
    chr(7): "CMD_UNPAUSE",
    chr(8): "CMD_PAUSE",
    chr(16): "CMD_RASTER_DATA_START",
    chr(17): "CMD_RASTER_DATA_END",
    chr(6): "STATUS_END",
    # status: error flags
    "!": "ERROR_SERIAL_STOP_REQUEST",
    '"': "ERROR_RX_BUFFER_OVERFLOW",
    "$": "ERROR_LIMIT_HIT_X1",
    "%": "ERROR_LIMIT_HIT_X2",
    "&": "ERROR_LIMIT_HIT_Y1",
    "*": "ERROR_LIMIT_HIT_Y2",
    "+": "ERROR_LIMIT_HIT_Z1",
    "-": "ERROR_LIMIT_HIT_Z2",
    "#": "ERROR_INVALID_MARKER",
    ":": "ERROR_INVALID_DATA",
    "<": "ERROR_INVALID_COMMAND",
    ">": "ERROR_INVALID_PARAMETER",
    "=": "ERROR_TRANSMISSION_ERROR",
    ";": "ERROR_SERIAL_WATCHDOG",
    # status: info flags
    "A": "INFO_IDLE_YES",
    "B": "INFO_DOOR_OPEN",
    "C": "INFO_CHILLER_OFF",
    "D": "INFO_PAUSED",
    # status: info params
    "x": "INFO_POS_X",
    "y": "INFO_POS_Y",
    "z": "INFO_POS_Z",
    "v": "INFO_VERSION",
    "w": "INFO_BUFFER_UNDERRUN",
    "u": "INFO_STACK_CLEARANCE",
    "~": "INFO_HELLO",
    "a": "INFO_OFFSET_X",
    "b": "INFO_OFFSET_Y",
    "c": "INFO_OFFSET_Z",
    # 'd': "INFO_TARGET_X",
    # 'e': "INFO_TARGET_Y",
    # 'f': "INFO_TARGET_Z",
    "g": "INFO_FEEDRATE",
    "h": "INFO_INTENSITY",
    "i": "INFO_DURATION",
    "j": "INFO_PIXEL_WIDTH",
    "k": "INFO_DEBUG",
}

SerialLoop = None
fallback_msg_thread = None


class SerialLoopClass(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)

        self.device = None
        self.tx_buffer = bytearray()
        self.tx_pos = 0

        # TX_CHUNK_SIZE - this is the number of bytes to be
        # written to the device in one go. It needs to match the device.
        self.TX_CHUNK_SIZE = 16
        self.RX_CHUNK_SIZE = 32
        self.FIRMBUF_SIZE = 256  # needs to match device firmware
        self.firmbuf_used = 0
        # How long the sender may sit gated with no send or ack before looking
        # for a lost-ack desync. Far longer than a chunk round-trip (~ms).
        self.FIRMBUF_STALL_TIMEOUT = 2.0  # seconds
        self.last_tx_progress = 0.0
        # Idle is reported only with an empty rx buffer, the one proof the
        # tally has drained.
        self.last_firmware_idle = 0.0

        # used for calculating percentage done
        self.job_size = 0

        # status flags
        self._status = {}  # last complete status frame
        self._s = {}  # status fram currently assembling
        self.reset_status()
        self._paused = False

        self.request_stop = False
        self.request_resume = False
        self.request_pause = False
        self.request_unpause = False
        self.request_status = 2  # 0: no request, 1: normal request, 2: super request

        self.pdata_count = 0
        self.pdata_nums = [128, 128, 128, 192]

        threading.Thread.__init__(self)
        self.stop_processing = False

        self.daemon = True  # kill thread when main thread exits

        # lock mechanism for chared data
        # see: http://effbot.org/zone/thread-synchronization.htm
        self.lock = threading.Lock()

    def reset_status(self):
        self._status = {
            "ready": False,  # is hardware idle (and not stop mode)
            "serial": False,  # is serial connected
            "appver": conf["version"],
            "firmver": None,
            "paused": False,
            "pos": [0.0, 0.0, 0.0],
            "underruns": 0,  # how many times machine is waiting for serial data
            "stackclear": 999999,  # minimal stack clearance (must stay above 0)
            "progress": 1.0,
            ### stop conditions
            # indicated when key present
            "stops": {},
            # possible keys:
            # x1, x2, y1, y2, z1, z2,
            # requested, buffer, marker, data, command, parameter, transmission,
            # watchdog
            "info": {},
            # possible keys: door, chiller
            ### super
            "offset": [0.0, 0.0, 0.0],
            # 'pos_target': [0.0, 0.0, 0.0],
            "feedrate": 0.0,
            "intensity": 0.0,
            "duration": 0.0,
            "pixelwidth": 0.0,
        }
        self._s = copy.deepcopy(self._status)

    def send_command(self, command):
        self.tx_buffer.append(ord(command))
        self.job_size += 1

    def send_param(self, param, val):
        # num to be [-134217.728, 134217.727], [-2**27, 2**27-1]
        # three decimals are retained
        num = int(round((val + 134217.728) * 1000))
        # saturate to the 28-bit protocol range; an out-of-range value would
        # otherwise wrap and command a wildly wrong position/feedrate
        if num < 0 or num > (1 << 28) - 1:
            print(f"WARN: param {param} value {val} out of range, clamping")
            num = max(0, min(num, (1 << 28) - 1))
        char0 = (num & 127) + 128
        char1 = ((num & (127 << 7)) >> 7) + 128
        char2 = ((num & (127 << 14)) >> 14) + 128
        char3 = ((num & (127 << 21)) >> 21) + 128
        self.tx_buffer.append(char0)
        self.tx_buffer.append(char1)
        self.tx_buffer.append(char2)
        self.tx_buffer.append(char3)
        self.tx_buffer.append(ord(param))
        self.job_size += 5

    def send_raster_data(self, data, start, end):
        # Build the whole chunk first, then splice it in under a single lock.
        # Locking per-pixel here meant thousands of acquire/release cycles per
        # raster line on the hot path.
        chunk = [ord(CMD_RASTER_DATA_START)]
        for val in itertools.islice(data, start, end):
            chunk.append(int((255 - val) / 2) + 128)
        chunk.append(ord(CMD_RASTER_DATA_END))
        with self.lock:
            self.tx_buffer.extend(chunk)
            self.job_size += len(chunk)

    def run(self):
        """Main loop of the serial thread."""
        # last_write = 0
        last_status_request = 0
        disable_computer_sleep()
        while True:
            if self.stop_processing:
                enable_computer_sleep()
                break
            with self.lock:
                # read/write
                if self.device:
                    try:
                        self._serial_read()
                        # (1/0.008)*16 = 2000 bytes/s
                        # for raster we need: 10(10000/60.0) = 1660 bytes/s
                        self._serial_write()
                        # if time.time()-last_write > 0.01:
                        #     sys.stdout.write('~')
                        # last_write = time.time()
                    except BaseException as e:
                        self.stop_processing = True
                        self._status["serial"] = False
                        self._status["ready"] = False
                        if isinstance(e, OSError):
                            print("ERROR: serial got disconnected 1.")
                        elif isinstance(e, ValueError):
                            print("ERROR: serial got disconnected 2.")
                        else:
                            print("ERROR: unknown serial error")
                            print(str(e))
                else:
                    self.stop_processing = True
                    self._status["serial"] = False
                    self._status["ready"] = False
                    print("ERROR: serial got disconnected 3.")
                # status request
                # Note: this also feeds the firmware serial watchdog (1s); at
                # 0.4s even a paused/idle host keeps it fed with 2.5x margin.
                if time.time() - last_status_request > 0.4:
                    if self._status["ready"]:
                        self.request_status = 2  # ready -> super request
                    else:
                        self.request_status = 1  # processing -> normal request
                    last_status_request = time.time()
                # flush stdout, so print shows up timely
                sys.stdout.flush()
            time.sleep(0.004)  # 250 Hz

    def _serial_read(self):
        chunk = self.device.read(self.RX_CHUNK_SIZE)
        if conf["print_serial_data"] and chunk != b"":
            timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-4]
            print(timestamp + " Receiving: " + prettify_serial(chunk, markers=markers_rx))
        for data_num in chunk:
            data_char = chr(data_num)
            if data_num < 32:  ### flow
                if data_char == CMD_CHUNK_PROCESSED:
                    self.firmbuf_used -= self.TX_CHUNK_SIZE
                    self.last_tx_progress = time.time()
                    if self.firmbuf_used < 0:
                        print("ERROR: firmware buffer tracking too low")
                elif data_char == STATUS_END:
                    # status frame complete, compile status
                    self._status, self._s = self._s, self._status  # flip
                    self._status["paused"] = self._paused
                    self._status["serial"] = bool(self.device)
                    if self.job_size == 0:
                        self._status["progress"] = 1.0
                    else:
                        self._status["progress"] = round(self.tx_pos / float(self.job_size), 3)
                    self._s["stops"].clear()
                    self._s["info"].clear()
                    self._s["ready"] = False
                    self._s["underruns"] = self._status["underruns"]
                    self._s["stackclear"] = self._status["stackclear"]
            elif 31 < data_num < 65:  ### stop error markers
                # chr is in [!-@], process flag
                if data_char == ERROR_LIMIT_HIT_X1:
                    self._s["stops"]["x1"] = True
                elif data_char == ERROR_LIMIT_HIT_X2:
                    self._s["stops"]["x2"] = True
                elif data_char == ERROR_LIMIT_HIT_Y1:
                    self._s["stops"]["y1"] = True
                elif data_char == ERROR_LIMIT_HIT_Y2:
                    self._s["stops"]["y2"] = True
                elif data_char == ERROR_LIMIT_HIT_Z1:
                    self._s["stops"]["z1"] = True
                elif data_char == ERROR_LIMIT_HIT_Z2:
                    self._s["stops"]["z2"] = True
                elif data_char == ERROR_SERIAL_STOP_REQUEST:
                    self._s["stops"]["requested"] = True
                    print("INFO firmware: stop request")
                elif data_char == ERROR_RX_BUFFER_OVERFLOW:
                    self._s["stops"]["buffer"] = True
                    print("ERROR firmware: rx buffer overflow")
                elif data_char == ERROR_INVALID_MARKER:
                    self._s["stops"]["marker"] = True
                    print("ERROR firmware: invalid marker")
                elif data_char == ERROR_INVALID_DATA:
                    self._s["stops"]["data"] = True
                    print("ERROR firmware: invalid data")
                elif data_char == ERROR_INVALID_COMMAND:
                    self._s["stops"]["command"] = True
                    print("ERROR firmware: invalid command")
                elif data_char == ERROR_INVALID_PARAMETER:
                    self._s["stops"]["parameter"] = True
                    print("ERROR firmware: invalid parameter")
                elif data_char == ERROR_TRANSMISSION_ERROR:
                    self._s["stops"]["transmission"] = True
                    print("ERROR firmware: transmission")
                elif data_char == ERROR_SERIAL_WATCHDOG:
                    self._s["stops"]["watchdog"] = True
                    print("ERROR firmware: serial watchdog (host comms lost)")
                else:
                    print("ERROR: invalid stop error marker")
                # in stop mode, print recent transmission, unless stop request, or limit
                if (
                    data_char != ERROR_SERIAL_STOP_REQUEST
                    and data_char != ERROR_LIMIT_HIT_X1
                    and data_char != ERROR_LIMIT_HIT_X2
                    and data_char != ERROR_LIMIT_HIT_Y1
                    and data_char != ERROR_LIMIT_HIT_Y2
                    and data_char != ERROR_LIMIT_HIT_Z1
                    and data_char != ERROR_LIMIT_HIT_Z2
                ):
                    recent_data = self.tx_buffer[max(0, self.tx_pos - 128) : self.tx_pos]
                    print("RECENT TX BUFFER:")
                    for data_num in recent_data:
                        data_char = chr(data_num)
                        if data_char in markers_tx:
                            print(f"\t{markers_tx[data_char]}")
                        elif 127 < data_num < 256:
                            print("\t(data byte)")
                        else:
                            print("\t(invalid)")
                    print("----------------")
                # stop mode housekeeping
                self.tx_buffer = bytearray()
                self.tx_pos = 0
                self.job_size = 0
                self._paused = False
                self.device.flushOutput()
                self.pdata_count = 0
                self._s["ready"] = True  # ready but in stop mode
            elif 64 < data_num < 91:  # info flags
                # data_char is in [A-Z], info flag
                if data_char == INFO_IDLE_YES:
                    # only claimed with an empty rx buffer, so this proves the
                    # firmware consumed everything sent
                    self.last_firmware_idle = time.time()
                    if not self.tx_buffer:
                        self._s["ready"] = True
                elif data_char == INFO_DOOR_OPEN:
                    self._s["info"]["door"] = True
                elif data_char == INFO_CHILLER_OFF:
                    self._s["info"]["chiller"] = True
                elif data_char == INFO_PAUSED:
                    # firmware froze (held position, beam off) — also proves it
                    # did not reset since the pause, so a resume is safe
                    self._s["info"]["paused"] = True
                else:
                    print("ERROR: invalid info flag")
                    sys.stdout.write(f"({data_char},{data_num})")
                self.pdata_count = 0
            elif 96 < data_num < 123:  # parameter
                # data_char is in [a-z], process parameter
                num = (
                    (
                        (self.pdata_nums[3] - 128) * 2097152
                        + (self.pdata_nums[2] - 128) * 16384
                        + (self.pdata_nums[1] - 128) * 128
                        + (self.pdata_nums[0] - 128)
                    )
                    - 134217728
                ) / 1000.0
                if data_char == INFO_POS_X:
                    self._s["pos"][0] = num
                elif data_char == INFO_POS_Y:
                    self._s["pos"][1] = num
                elif data_char == INFO_POS_Z:
                    self._s["pos"][2] = num
                elif data_char == INFO_VERSION:
                    num = str(int(num) / 100.0)
                    self._s["firmver"] = num
                elif data_char == INFO_BUFFER_UNDERRUN:
                    self._s["underruns"] = num
                elif data_char == INFO_DEBUG:
                    # available for custom debugging messaging
                    pass
                # super status
                elif data_char == INFO_OFFSET_X:
                    self._s["offset"][0] = num
                elif data_char == INFO_OFFSET_Y:
                    self._s["offset"][1] = num
                elif data_char == INFO_OFFSET_Z:
                    self._s["offset"][2] = num
                elif data_char == INFO_FEEDRATE:
                    self._s["feedrate"] = num
                elif data_char == INFO_INTENSITY:
                    self._s["intensity"] = 100 * num / 255
                elif data_char == INFO_DURATION:
                    self._s["duration"] = num
                elif data_char == INFO_PIXEL_WIDTH:
                    self._s["pixelwidth"] = num
                elif data_char == INFO_STACK_CLEARANCE:
                    self._s["stackclear"] = num
                else:
                    print("ERROR: invalid param")
                self.pdata_count = 0
                self.pdata_nums = [128, 128, 128, 192]
            elif data_num > 127:  ### data
                # data_char is in [128,255]
                if self.pdata_count < 4:
                    self.pdata_nums[self.pdata_count] = data_num
                    self.pdata_count += 1
                else:
                    print("ERROR: invalid data")
            else:
                print(data_num)
                print(data_char)
                print("ERROR: invalid marker")
                self.pdata_count = 0

    def _serial_write(self):
        ### sending super commands (handled in serial rx interrupt)
        if self.request_status == 1:
            self._send_char(CMD_STATUS)
            self.request_status = 0
        elif self.request_status == 2:
            self._send_char(CMD_SUPERSTATUS)
            self.request_status = 0

        if self.request_stop:
            self._send_char(CMD_STOP)
            self.request_stop = False

        if self.request_resume:
            self._send_char(CMD_RESUME)
            self.firmbuf_used = 0  # a resume resets the hardware's rx buffer
            self.request_resume = False
            self.reset_status()
            self.request_status = 2  # super request

        # pause/unpause freeze the controller in place (beam off) without
        # discarding its buffer, so motion resumes exactly where it left off.
        # Sent above the _paused gate so they apply even while paused. Unlike
        # CMD_RESUME, CMD_UNPAUSE does NOT reset the firmware rx buffer (retained
        # in-flight data keeps streaming), so firmbuf_used is left intact.
        if self.request_pause:
            self._send_char(CMD_PAUSE)
            self.request_pause = False
        if self.request_unpause:
            self._send_char(CMD_UNPAUSE)
            self.request_unpause = False
        ### send buffer chunk
        if self.tx_buffer and len(self.tx_buffer) > self.tx_pos:
            if not self._paused:
                if (self.FIRMBUF_SIZE - self.firmbuf_used) > self.TX_CHUNK_SIZE:
                    self.last_tx_progress = time.time()
                    assumedSent = 0  # bytes that made it out; stays 0 on failure
                    try:
                        # to_send = ''.join(islice(self.tx_buffer, 0, self.TX_CHUNK_SIZE))
                        to_send = self.tx_buffer[self.tx_pos : self.tx_pos + self.TX_CHUNK_SIZE]
                        expectedSent = len(to_send)
                        if conf["print_serial_data"]:
                            timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-4]
                            print(
                                timestamp
                                + " Sending: "
                                + prettify_serial(to_send, markers=markers_tx)
                            )

                        # by protocol duplicate every char
                        to_send_double = []
                        for n in to_send:
                            to_send_double.append(n)
                            to_send_double.append(n)
                        to_send = to_send_double
                        #
                        t_prewrite = time.time()
                        actuallySent = self.device.write(to_send)
                        if actuallySent != expectedSent * 2:
                            print("ERROR: write did not complete")
                        else:
                            assumedSent = expectedSent
                            self.firmbuf_used += assumedSent
                            if self.firmbuf_used > self.FIRMBUF_SIZE:
                                print("ERROR: firmware buffer tracking too high")
                        if time.time() - t_prewrite > 0.1:
                            print("WARN: write delay 1")
                    except serial.SerialTimeoutException:
                        # transient: don't advance, the chunk is retried next loop
                        print("ERROR: writeTimeoutError 2")

                    # only advance past bytes the device accepted; a genuine write
                    # error propagates to run() which stops processing
                    self.tx_pos += assumedSent
                elif time.time() - self.last_tx_progress > self.FIRMBUF_STALL_TIMEOUT:
                    # Either an ack was lost and the tally is stale, or the
                    # firmware is still chewing through what it has: a raster
                    # line is consumed a pixel at a time. Only an idle report
                    # tells the two apart, and resyncing on a guess overflows
                    # the controller's rx buffer.
                    if self.last_firmware_idle > self.last_tx_progress:
                        print("WARN: firmbuf tally desync, resyncing to resume send")
                        self.firmbuf_used = 0
                    self.last_tx_progress = time.time()
        else:
            if self.tx_buffer:  # job finished sending
                self.job_size = 0
                self.tx_buffer = bytearray()
                self.tx_pos = 0

    def _send_char(self, char):
        try:
            t_prewrite = time.time()
            if conf["print_serial_data"]:
                timestamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-4]
                print(timestamp + " Sending: " + prettify_serial(ord(char), markers=markers_tx))
            self.device.write([ord(char), ord(char)])  # by protocol send twice
            if time.time() - t_prewrite > 0.1:
                pass
        except serial.SerialTimeoutException:
            print("ERROR: writeTimeoutError 1")


###########################################################################
### API ###################################################################
###########################################################################


def prettify_serial(chunk, markers=markers_tx):
    string = ""
    if not hasattr(prettify_serial, "tx_pdata_nums"):
        prettify_serial.rx_pdata_nums = [128, 128, 128, 192]
        prettify_serial.rx_pdata_count = 0
        prettify_serial.tx_pdata_nums = [128, 128, 128, 192]
        prettify_serial.tx_pdata_count = 0
        prettify_serial.tx_rasterstream = False
        prettify_serial.tx_rastercount = 0

    if isinstance(chunk, int):
        chunk = [chunk]  # make integer inputs iterable

    for i in range(len(chunk)):
        data = chunk[i]
        if data >= 128:
            string += str(data) + " "
            if (markers == markers_tx) and prettify_serial.tx_rasterstream:
                prettify_serial.tx_rastercount += 1
            elif markers == markers_tx:
                prettify_serial.tx_pdata_nums[prettify_serial.tx_pdata_count] = data
                prettify_serial.tx_pdata_count += 1
            elif markers == markers_rx:
                prettify_serial.rx_pdata_nums[prettify_serial.rx_pdata_count] = data
                prettify_serial.rx_pdata_count += 1
        elif data < 128:
            if (markers == markers_tx) and (
                markers[chr(data)] not in ["CMD_STATUS", "CMD_SUPERSTATUS"]
            ):
                prettify_serial.tx_pdata_count = 0
                prettify_serial.tx_pdata_nums = [128, 128, 128, 192]
            elif markers[chr(data)] not in ["CMD_CHUNK_PROCESSED"]:
                prettify_serial.rx_pdata_count = 0
                prettify_serial.rx_pdata_nums = [128, 128, 128, 192]

            if markers[chr(data)] == "CMD_RASTER_DATA_START":
                prettify_serial.tx_rasterstream = True
                prettify_serial.tx_rastercount = 0
            elif markers[chr(data)] == "CMD_RASTER_DATA_END":
                prettify_serial.tx_rasterstream = False
                string += "(" + str(prettify_serial.tx_rastercount) + ") "

            string += markers[chr(data)] + ", "

        if prettify_serial.tx_pdata_count == 4:
            num = (
                (
                    (prettify_serial.tx_pdata_nums[3] - 128) * 2097152
                    + (prettify_serial.tx_pdata_nums[2] - 128) * 16384
                    + (prettify_serial.tx_pdata_nums[1] - 128) * 128
                    + (prettify_serial.tx_pdata_nums[0] - 128)
                )
                - 134217728
            ) / 1000.0
            prettify_serial.tx_pdata_count = 0
            prettify_serial.tx_pdata_nums = [128, 128, 128, 192]
            string += "(" + str(num) + ") "
        elif prettify_serial.rx_pdata_count == 4:
            num = (
                (
                    (prettify_serial.rx_pdata_nums[3] - 128) * 2097152
                    + (prettify_serial.rx_pdata_nums[2] - 128) * 16384
                    + (prettify_serial.rx_pdata_nums[1] - 128) * 128
                    + (prettify_serial.rx_pdata_nums[0] - 128)
                )
                - 134217728
            ) / 1000.0
            prettify_serial.rx_pdata_count = 0
            prettify_serial.rx_pdata_nums = [128, 128, 128, 192]
            string += "(" + str(num) + ") "

    if len(string) >= 2 and string[-2] == ",":
        string = string[:-2]
    elif len(string) >= 1 and string[-1] == " ":
        string = string[:-1]

    return string


def find_controller(baudrate=conf["baudrate"], verbose=True):
    iterator = sorted(serial.tools.list_ports.comports())
    # look for Arduinos
    arduinos = []
    for port, desc, _ in iterator:
        if "uino" in desc:
            arduinos.append(port)
    # check these arduinos for driveboard firmware, take first
    for port in arduinos:
        try:
            with serial.Serial(port=port, baudrate=baudrate, timeout=2.0) as s:
                lasaur_hello = s.read(8)
                if lasaur_hello.find(ord(INFO_HELLO)) > -1:
                    return port
        except serial.SerialException:
            pass
    # check all comports for driveboard firmware
    for port, _desc, _hwid in iterator:
        try:
            with serial.Serial(port=port, baudrate=baudrate, timeout=2.0) as s:
                lasaur_hello = s.read(8)
                if lasaur_hello.find(ord(INFO_HELLO)) > -1:
                    return port
        except serial.SerialException:
            pass
    # handle the case Arduino without firmware
    if arduinos:
        return arduinos[0]
    # none found
    if verbose:
        print("ERROR: No controller found.")
    return None


def connect(port=None, baudrate=None, verbose=True):
    global SerialLoop
    # resolve config at call time, not import time (serial_port is set after
    # auto-detect on first connect)
    if port is None:
        port = conf["serial_port"]
    if baudrate is None:
        baudrate = conf["baudrate"]
    if not SerialLoop:
        SerialLoop = SerialLoopClass()

        # Create serial device with read timeout set to 0.
        # This results in the read() being non-blocking.
        # Write on the other hand uses a large timeout but should not be blocking
        # much because we ask it only to write TX_CHUNK_SIZE at a time.
        # BUG WARNING: the pyserial write function does not report how
        # many bytes were actually written if this is different from requested.
        # Work around: use a big enough timeout and a small enough chunk size.
        try:
            if conf["usb_reset_hack"]:
                import flash

                flash.usb_reset_hack()
            # reset the controller and wait for its hello
            SerialLoop.device = serial.Serial(port, baudrate, timeout=0, writeTimeout=4)
            if conf["hardware"] == "standard":
                # clear throat
                # Toggle DTR to reset Arduino
                SerialLoop.device.setDTR(False)
                time.sleep(1)
                SerialLoop.device.flushInput()
                SerialLoop.device.setDTR(True)
                # for good measure
                SerialLoop.device.flushOutput()
            else:
                import flash

                flash.reset_atmega()
                time.sleep(0.5)
                SerialLoop.device.flushInput()
                SerialLoop.device.flushOutput()

            start = time.time()
            while True:
                if time.time() - start > 2:
                    if verbose:
                        print("ERROR: Cannot get 'hello' from controller")
                    raise serial.SerialException
                data = SerialLoop.device.read(1)
                if data.find(ord(INFO_HELLO)) > -1:
                    if verbose:
                        print("Controller says Hello!")
                        print(f"Connected on serial port: {port}")
                    break

            SerialLoop.start()  # this calls run() in a thread
        except serial.SerialException:
            # handshake/open failed: close the port if we opened it (the thread
            # hasn't started yet) so we don't leak the OS serial handle
            if SerialLoop and SerialLoop.device:
                try:
                    SerialLoop.device.close()
                except Exception:
                    pass
            SerialLoop = None
            if verbose:
                print(f"ERROR: Cannot connect serial on port: {port}")
    else:
        if verbose:
            print("ERROR: disconnect first")


def connect_withfind(port=None, baudrate=None, verbose=True):
    if port is None:
        port = conf["serial_port"]
    if baudrate is None:
        baudrate = conf["baudrate"]
    connect(port=port, baudrate=baudrate, verbose=verbose)
    if not connected():
        # try finding driveboard
        if verbose:
            print("WARN: Cannot connect to configured serial port.")
            print("INFO: Trying to find port.")
        serialfindresult = find_controller(verbose=verbose)
        if serialfindresult:
            if verbose:
                print(f"INFO: Hardware found at {serialfindresult}.")
            connect(port=serialfindresult, baudrate=baudrate, verbose=verbose)
            if not connected():  # special case arduino found, but no firmware
                yesno = input("Firmware appears to be missing. Want to flash-upload it (Y/N)? ")
                if yesno in ("Y", "y"):
                    ret = flash(serial_port=serialfindresult)
                    if ret == 0:
                        connect(port=serialfindresult, baudrate=baudrate, verbose=verbose)
        if connected():
            if verbose:
                print(f"INFO: Connected at {serialfindresult}.")
            conf["serial_port"] = serialfindresult
            write_config_fields({"serial_port": serialfindresult})
        else:
            if verbose:
                print(
                    "-----------------------------------------------------------------------------"
                )
                print("How to configure:")
                print("https://github.com/nortd/driveboardapp/blob/main/docs/configure.md")
                print(
                    "-----------------------------------------------------------------------------"
                )


def connected():
    global SerialLoop
    # a dead serial thread (e.g. after a disconnect) means we are NOT connected,
    # even though the SerialLoop object and its (stale) device handle linger
    return bool(
        SerialLoop
        and SerialLoop.device
        and SerialLoop.is_alive()
        and not SerialLoop.stop_processing
    )


def reconnect():
    """Recover from a dropped serial link: tear down the dead serial loop (its
    thread has exited and can't be restarted) and reconnect. The controller
    resets on reconnect, so any in-flight job is lost and must be re-run.
    No-op when already connected."""
    global SerialLoop
    if connected():
        return
    if SerialLoop is not None:
        try:
            if SerialLoop.device:
                SerialLoop.device.close()
        except Exception:
            pass
        if SerialLoop.is_alive():
            SerialLoop.stop_processing = True
            SerialLoop.join()
        SerialLoop = None

    print(f"INFO: serial disconnected, attempting reconnect on {conf['serial_port']}")
    connect_withfind(verbose=False)
    if connected():
        print("INFO: serial reconnected")
    else:
        print("WARN: serial reconnect attempt failed")


def close():
    global SerialLoop
    if SerialLoop:
        if SerialLoop.device:
            SerialLoop.device.flushOutput()
            SerialLoop.device.flushInput()
            ret = True
        else:
            ret = False
        if SerialLoop.is_alive():
            SerialLoop.stop_processing = True
            SerialLoop.join()
        if SerialLoop.device:
            # close after the thread has stopped so it isn't using the port
            try:
                SerialLoop.device.close()
            except Exception:
                pass
    else:
        ret = False
    SerialLoop = None
    return ret


def flash(serial_port=None, firmware=None):
    import flash

    # read config at call time, not import time: the app only learns the serial
    # port once it connects/auto-detects, well after this module is imported
    if serial_port is None:
        serial_port = conf["serial_port"]
    if firmware is None:
        firmware = conf["firmware"]
    if not serial_port:
        print("ERROR: no serial port set; connect to the machine first")
        return 1
    reconnect = False
    if connected():
        close()  # release the port so avrdude can open it
        reconnect = True
    ret = flash.flash_upload(serial_port=serial_port, firmware=firmware)
    if reconnect:
        connect(port=serial_port)
    if ret != 0:
        print("ERROR: flash failed")
    return ret


def build():
    import build

    ret = build.build_all()
    if ret != 0:
        print("ERROR: build_all failed")
    return ret


def reset():
    import flash

    reconnect = False
    if connected():
        close()
        reconnect = True
    flash.reset_atmega()
    if reconnect:
        connect(port=conf["serial_port"])


def status():
    """Get status."""
    if connected():
        global SerialLoop
        with SerialLoop.lock:
            stats = copy.deepcopy(SerialLoop._status)
            stats["serial"] = connected()  # make sure serial flag is up-to-date
        return stats
    else:
        return {"serial": False, "ready": False}


def homing():
    """Run homing cycle."""
    global SerialLoop
    with SerialLoop.lock:
        if SerialLoop._status["ready"] or SerialLoop._status["stops"]:
            SerialLoop.request_resume = True  # to recover from a stop mode
            SerialLoop.send_command(CMD_HOMING)
        else:
            print("WARN: ignoring homing command while job running")


def _clamp_param(name, val, lo, hi):
    """Bound a motion parameter, reporting anything that had to be moved.

    These are the last gate before the wire. Jobs are checked earlier with a
    readable error, so a clamp here means something bypassed that check.
    """
    clamped = max(lo, min(val, hi))
    if clamped != val:
        print(f"WARN: {name} of {val} out of range [{lo}, {hi}], clamping to {clamped}")
    return clamped


def feedrate(val):
    global SerialLoop
    with SerialLoop.lock:
        # zero or negative reaches the planner as a zero or wrapped step rate
        val = _clamp_param("feedrate", val, MIN_FEEDRATE, MAX_FEEDRATE)
        SerialLoop.send_param(PARAM_FEEDRATE, val)


def intensity(val):
    global SerialLoop
    with SerialLoop.lock:
        val = max(min(255 * val / 100, 255), 0)
        SerialLoop.send_param(PARAM_INTENSITY, val)


def duration(val):
    global SerialLoop
    with SerialLoop.lock:
        # a dwell holds the beam on in one spot for its whole duration
        val = _clamp_param("duration", val, 0.0, MAX_DWELL_SECONDS)
        SerialLoop.send_param(PARAM_DURATION, val)


def pixelwidth(val):
    global SerialLoop
    with SerialLoop.lock:
        # zero selects a plain line, negative walks the raster pixel index back
        val = _clamp_param("pixelwidth", val, 0.0, MAX_PARAM_VALUE)
        SerialLoop.send_param(PARAM_PIXEL_WIDTH, val)


def relative():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_REF_RELATIVE)


def absolute():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_REF_ABSOLUTE)


def target_in_workarea(x=None, y=None, z=None, machine_coords=False):
    """Whether an absolute move target stays within the work area.

    Coordinates are in offset coordinates unless machine_coords is set (e.g.
    supermove). None coordinates are ignored. Z is only bounded when the config
    gives it a non-zero work area, since a machine with no Z axis configured
    still has to be able to jog the focus.
    """
    global SerialLoop
    if machine_coords:
        x_off = y_off = z_off = 0.0
    else:
        with SerialLoop.lock:
            x_off = SerialLoop._status["offset"][0]
            y_off = SerialLoop._status["offset"][1]
            z_off = SerialLoop._status["offset"][2]
    if x is not None and not (-x_off <= x <= conf["workspace"][0] - x_off):
        return False
    if y is not None and not (-y_off <= y <= conf["workspace"][1] - y_off):
        return False
    if z is not None and conf["workspace"][2] and not (-z_off <= z <= conf["workspace"][2] - z_off):
        return False
    return True


def move(x=None, y=None, z=None):
    global SerialLoop
    with SerialLoop.lock:
        if x is not None:
            SerialLoop.send_param(PARAM_TARGET_X, x)
        if y is not None:
            SerialLoop.send_param(PARAM_TARGET_Y, y)
        if z is not None:
            SerialLoop.send_param(PARAM_TARGET_Z, z)
        SerialLoop.send_command(CMD_LINE)


def supermove(x=None, y=None, z=None):
    """Moves in machine coordinates bypassing any offsets.

    A positioning move, never a cut, so the beam goes off in the same locked
    sequence. Intensity persists in the controller between commands. Sent
    inline because intensity() would deadlock on the lock held here.
    """
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_param(PARAM_INTENSITY, 0)
        # clear offset
        SerialLoop.send_command(CMD_OFFSET_STORE)
        SerialLoop.send_command(CMD_REF_STORE)
        SerialLoop.send_command(CMD_REF_ABSOLUTE)
        if x is not None:
            SerialLoop.send_param(PARAM_OFFSET_X, 0)
        if y is not None:
            SerialLoop.send_param(PARAM_OFFSET_Y, 0)
        if z is not None:
            SerialLoop.send_param(PARAM_OFFSET_Z, 0)
        SerialLoop.send_command(CMD_REF_RESTORE)
        # move
        if x is not None:
            SerialLoop.send_param(PARAM_TARGET_X, x)
        if y is not None:
            SerialLoop.send_param(PARAM_TARGET_Y, y)
        if z is not None:
            SerialLoop.send_param(PARAM_TARGET_Z, z)
        SerialLoop.send_command(CMD_OFFSET_RESTORE)
        SerialLoop.send_command(CMD_LINE)


def rastermove(x, y, z=0.0):
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_param(PARAM_TARGET_X, x)
        SerialLoop.send_param(PARAM_TARGET_Y, y)
        SerialLoop.send_param(PARAM_TARGET_Z, z)
        SerialLoop.send_command(CMD_RASTER)


def rasterdata(data, start, end):
    # NOTE: no SerialLoop.lock
    # more granular locking in send_data
    SerialLoop.send_raster_data(data, start, end)


def pause():
    global SerialLoop
    with SerialLoop.lock:
        if SerialLoop.tx_buffer:
            SerialLoop._paused = True  # stop feeding the buffer
            SerialLoop.request_pause = True  # freeze the controller in place


def unpause():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.request_unpause = True  # release the freeze, resume motion
        SerialLoop._paused = False  # resume feeding the buffer


def stop():
    """Force stop condition."""
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.tx_buffer = bytearray()
        SerialLoop.tx_pos = 0
        SerialLoop.job_size = 0
        SerialLoop.request_stop = True
        SerialLoop._paused = False


def unstop():
    """Resume from stop condition."""
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.request_resume = True


def dwell():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_DWELL)


def air_on():
    global SerialLoop
    if SerialLoop is None:
        return
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AIR_ENABLE)


def air_off():
    global SerialLoop
    if SerialLoop is None:
        return
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AIR_DISABLE)


def aux_on():
    global SerialLoop
    if SerialLoop is None:
        return
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AUX_ENABLE)


def aux_off():
    global SerialLoop
    if SerialLoop is None:
        return
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AUX_DISABLE)


def pulse():
    print("Pulsing laser")
    air_on()

    # turn the laser on for a short pulse
    intensity(float(conf["pulse_intensity"]))
    duration(float(conf["pulse_duration"]))
    dwell()
    # dwell without firing for a second to keep air on
    intensity(0.0)
    duration(1)
    dwell()

    air_off()


def offset(x=None, y=None, z=None):
    """Sets an offset relative to present pos."""
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_REF_STORE)
        SerialLoop.send_command(CMD_REF_RELATIVE)
        if x is not None:
            SerialLoop.send_param(PARAM_OFFSET_X, x)
        if y is not None:
            SerialLoop.send_param(PARAM_OFFSET_Y, y)
        if z is not None:
            SerialLoop.send_param(PARAM_OFFSET_Z, z)
        SerialLoop.send_command(CMD_REF_RESTORE)


def absoffset(x=None, y=None, z=None):
    """Sets an offset in machine coordinates."""
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_REF_STORE)
        SerialLoop.send_command(CMD_REF_ABSOLUTE)
        if x is not None:
            SerialLoop.send_param(PARAM_OFFSET_X, x)
        if y is not None:
            SerialLoop.send_param(PARAM_OFFSET_Y, y)
        if z is not None:
            SerialLoop.send_param(PARAM_OFFSET_Z, z)
        SerialLoop.send_command(CMD_REF_RESTORE)


def jobfile(filepath):
    with open(filepath) as fp:
        jobdict = json.load(fp)
    job(jobdict)


def job(jobdict):
    job_stop_guard()
    if "head" in jobdict:
        if "kind" in jobdict["head"] and jobdict["head"]["kind"] == "mill":
            job_mill(jobdict)
        else:
            job_laser(jobdict)
    else:
        print("INFO: not a valid job, 'head' entry missing")


def job_stop_guard():
    """Refuse to queue a job while the controller is in a stop condition.

    A stop leaves the controller holding an rx buffer it only clears on resume,
    so a job queued now would sit unsent behind a stale tally. Clearing the
    stop is also where someone acknowledges why it happened.

    Raises ValueError naming the active stops.
    """
    global SerialLoop

    if SerialLoop is None:
        return
    with SerialLoop.lock:
        stops = sorted(SerialLoop._status["stops"])
    if stops:
        raise ValueError(f"cannot start a job while stopped ({', '.join(stops)}), clear it first")


def job_pass_params_validate(jobdict):
    """Check each pass' motion parameters against the ranges the machine accepts.

    A feed rate of zero or less gives the planner a zero or wrapped step rate,
    and a long dwell holds the beam on in one spot, so both are rejected here
    rather than silently clamped on the way to the wire.

    Raises a ValueError naming the offending pass and parameter.
    """
    bounds = {
        "feedrate": (MIN_FEEDRATE, MAX_FEEDRATE),
        "seekrate": (MIN_FEEDRATE, MAX_FEEDRATE),
        "intensity": (0.0, 100.0),
        "pxsize": (0.0, MAX_PARAM_VALUE),
        "pierce_time": (0.0, MAX_DWELL_SECONDS),
    }
    for passidx, pass_ in enumerate(jobdict["passes"]):
        for key, (lo, hi) in bounds.items():
            if key not in pass_:
                continue
            if pass_[key] is None or pass_[key] == "":
                # an svg cut setting tag leaves unset fields empty, which means
                # take the default rather than take zero
                del pass_[key]
                continue
            try:
                val = float(pass_[key])
            except (TypeError, ValueError):
                raise ValueError(
                    f"pass {passidx + 1}: {key} of {pass_[key]!r} is not a number"
                ) from None
            if not lo <= val <= hi:
                raise ValueError(f"pass {passidx + 1}: {key} of {val} outside [{lo}, {hi}]")
            # store the coerced number, svg tags arrive as strings
            pass_[key] = val
        for key in ("air_assist", "aux_assist"):
            # an unrecognised mode matches no branch and silently runs the job
            # with that assist off, which for air is a fire risk
            if key in pass_ and pass_[key] not in ASSIST_MODES:
                raise ValueError(
                    f"pass {passidx + 1}: {key} of {pass_[key]!r} is not one of {ASSIST_MODES}"
                )


def job_laser_validate(jobdict):
    """
    Validate that the defined passes stay within the work area.

    Raises a ValueError with a descriptive message if the job is not valid.
    """
    global SerialLoop

    job_pass_params_validate(jobdict)

    with SerialLoop.lock:
        x_off = SerialLoop._status["offset"][0]
        y_off = SerialLoop._status["offset"][1]
    x_lim = conf["workspace"][0] - x_off
    y_lim = conf["workspace"][1] - y_off

    def check_point(point, passidx, kind):
        # len(point) is not guaranteed to be 2
        x, y = point[0], point[1]
        err_str = ""
        if y < -y_off:
            err_str = "top "
        elif y > y_lim:
            err_str = "bottom "
        if x < -x_off:
            err_str += "left"
        elif x > x_lim:
            err_str += "right"
        if err_str != "":
            err_str = err_str.strip()
            # the frontend displays the first pass as "pass 1" so use passidx+1
            raise ValueError(f"pass {passidx + 1}: point in {kind} beyond {err_str} of work area")

    # loop passes
    for passidx, pass_ in enumerate(jobdict["passes"]):
        # set absolute/relative
        is_relative = pass_.get("relative", False)

        # loop pass' items
        for itemidx in pass_["items"]:
            item = jobdict["items"][itemidx]
            def_ = jobdict["defs"][item["def"]]
            kind = def_["kind"]

            if kind == "image":
                # an all-white or transparent margin is skipped by the engraver,
                # so only the part that actually burns has to fit
                try:
                    corners = _raster_engraved_box_mm(def_, pass_)
                    if corners is None:
                        continue  # nothing engraves, nothing to check
                except Exception as e:
                    print(f"WARN: could not read image pixels ({e}), checking full extent")
                    pos, size = def_["pos"], def_["size"]
                    corners = [pos, [pos[0] + size[0], pos[1] + size[1]]]

                # the image is aligned with the axes, so two opposite corners
                # are enough to tell whether it fits in the work area
                check_point(corners[0], passidx, kind)
                check_point(corners[1], passidx, kind)

            elif kind == "fill" or kind == "path":
                path = def_["data"]
                for polyline in path:
                    point = [0, 0]
                    for pos in polyline:
                        if is_relative:
                            point[0] += pos[0]
                            point[1] += pos[1]
                            check_point(point, passidx, kind)
                        else:
                            check_point(pos, passidx, kind)


def _emit_raster_segment(orientation, seekrate, feedrate_, intensity_):
    """Stream one raster segment: seek to lead-in, ramp in, raster move with
    pixel data, ramp out. `orientation` describes one engraving direction of a
    segment: absolute mm positions (leadin/start/end/leadout, line_y) and the
    pixel slice (data/lo/hi).

    start and end are the outer edges of the first and last pixel, so a run of
    n pixels travels n pixel widths. The controller latches a pixel every pixel
    width from the start of the move, so each pixel burns over its own extent,
    and even a lone pixel gets a move long enough for the planner to keep.
    """
    line_y = orientation["line_y"]
    intensity(0.0)  # intensity for seek and lead-in
    feedrate(seekrate)  # feedrate for seek
    move(orientation["leadin"], line_y)  # seek to lead-in start
    feedrate(feedrate_)  # feedrate for lead-in, raster, and lead-out
    move(orientation["start"], line_y)  # lead-in
    intensity(intensity_)  # intensity for raster move
    rastermove(orientation["end"], line_y)  # raster move
    rasterdata(orientation["data"], orientation["lo"], orientation["hi"])  # stream raster data
    intensity(0.0)  # intensity for lead-out
    move(orientation["leadout"], line_y)  # lead-out


def _raster_orientations(
    lo,
    hi,
    line_start,
    line_y,
    posx,
    pxsize_x,
    leadin,
    workspace_x,
    pxarray,
    pxarray_reversed,
    px_n,
):
    """Build the two engraving orientations (left-to-right and right-to-left)
    for a canonical raster run spanning pixel indices [lo, hi).

    start and end are pixel edges, see _emit_raster_segment.
    """
    left_x = posx + (lo - line_start) * pxsize_x
    right_x = posx + (hi - line_start) * pxsize_x
    leadin_left = max(left_x - leadin, 0)
    leadout_right = min(right_x + leadin, workspace_x)
    fwd = {
        "leadin": leadin_left,
        "start": left_x,
        "end": right_x,
        "leadout": leadout_right,
        "line_y": line_y,
        "data": pxarray,
        "lo": lo,
        "hi": hi,
    }
    rev = {
        "leadin": leadout_right,
        "start": right_x,
        "end": left_x,
        "leadout": leadin_left,
        "line_y": line_y,
        "data": pxarray_reversed,
        "lo": px_n - hi,
        "hi": px_n - lo,
    }
    return fwd, rev


def _emit_raster_nn(
    segments,
    start_x,
    start_y,
    posx,
    pxsize_x,
    leadin,
    workspace_x,
    pxarray,
    pxarray_reversed,
    px_n,
    seekrate,
    feedrate_,
    intensity_,
):
    """Emit collected raster segments in greedy nearest-neighbor order, choosing
    each segment's orientation by whichever end the head reaches first. Minimizes
    seek travel between segments (useful for sparse / large-whitespace images)."""
    from jobimport import kdtree

    if not segments:
        return
    orients = []
    tree = kdtree.Tree(2)
    for idx, (lo, hi, line_start, line_y) in enumerate(segments):
        fwd, rev = _raster_orientations(
            lo,
            hi,
            line_start,
            line_y,
            posx,
            pxsize_x,
            leadin,
            workspace_x,
            pxarray,
            pxarray_reversed,
            px_n,
        )
        nodes = (
            tree.insert([fwd["leadin"], line_y], (idx, 0)),
            tree.insert([rev["leadin"], line_y], (idx, 1)),
        )
        orients.append(((fwd, rev), nodes))
    current = [start_x, start_y]
    for _ in range(len(segments)):
        node, _distsq = tree.nearest(current, checkempty=True)
        if node is None:
            break
        idx, which = node.data
        (fwd, rev), (node_fwd, node_rev) = orients[idx]
        node_fwd.data = None  # consume both orientations of this segment
        node_rev.data = None
        orientation = fwd if which == 0 else rev
        _emit_raster_segment(orientation, seekrate, feedrate_, intensity_)
        current = [orientation["leadout"], orientation["line_y"]]


def _pass_pxsize(pass_):
    """The (x, y) raster pixel size of a pass in mm, with 2x horizontal
    resolution. Clamped to 0.01 mm so it can never divide by zero."""
    pxsize_y = max(float(pass_.get("pxsize", conf["pxsize"])), 0.01)
    return pxsize_y / 2.0, pxsize_y


def _raster_grayscale(data, px_w, px_h):
    """Decode the base64 image to a px_w x px_h grayscale PIL image, with
    transparency composited onto white and invert applied.
    0=black/full power, 255=white/no power."""
    # everything after the comma is the payload, so this works whatever the
    # mime type is, and a bare base64 string with no data URI prefix works too
    imgobj = Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[-1])))
    imgobj = imgobj.resize((px_w, px_h), resample=Image.BICUBIC)
    if imgobj.mode in ["PA", "LA", "RGBA", "La", "RBGa"]:
        imgobj = imgobj.convert("RGBA")
        imgbg = Image.new("RGBA", imgobj.size, (255, 255, 255))
        imgbg.paste(imgobj, imgobj)
        imgobj = imgbg.convert("L")
    else:
        imgobj = imgobj.convert("L")

    if conf["raster_invert"]:
        imgobj = imgobj.point(lambda px: 255 - px)
    return imgobj


def _raster_engraved_box(imgobj, px_w, px_h):
    """Pixel box (x0, y0, x1, y1) of what a grayscale raster will actually burn.

    The engraver skips pure white (255), both whole scanlines and the
    whitespace at either end of a line, so an all-white margin never moves the
    head and must not count towards the job's extent. The box is grown by a
    pixel on each side to cover dithering error diffused into that margin.
    Returns None if nothing will be engraved at all.
    """
    # getbbox() reports the extent of the non-zero pixels, so map white to zero
    box = imgobj.point(lambda px: 0 if px == 255 else 255).getbbox()
    if box is None:
        return None
    return (
        max(box[0] - 1, 0),
        max(box[1] - 1, 0),
        min(box[2] + 1, px_w),
        min(box[3] + 1, px_h),
    )


def _raster_engraved_box_mm(def_, pass_):
    """Absolute mm corners [[x0,y0], [x1,y1]] of the part of an image def that
    will actually be engraved in `pass_`, or None if nothing will be."""
    pos = def_["pos"]
    size = def_["size"]
    pxsize_x, pxsize_y = _pass_pxsize(pass_)
    px_w = int(size[0] / pxsize_x)
    px_h = int(size[1] / pxsize_y)
    if px_w <= 0 or px_h <= 0:
        return None
    box = _raster_engraved_box(_raster_grayscale(def_["data"], px_w, px_h), px_w, px_h)
    if box is None:
        return None
    return [
        [pos[0] + box[0] / px_w * size[0], pos[1] + box[1] / px_h * size[1]],
        [pos[0] + box[2] / px_w * size[0], pos[1] + box[3] / px_h * size[1]],
    ]


def _raster_load_pixels(data, px_w, px_h, n_raster_levels):
    """Decode the base64 image to a flat grayscale pixel list (0=black/full
    power, 255=white/no power), applying invert and dithering. Returns
    (pxarray, pxarray_reversed, px_n, engraved_box)."""
    imgobj = _raster_grayscale(data, px_w, px_h)
    engraved_box = _raster_engraved_box(imgobj, px_w, px_h)

    pxarray = list(imgobj.getdata())
    pxarray[:] = (value for value in pxarray if type(value) is not str)
    if n_raster_levels < 128:  # skip dithering if max resolution
        pxarray = raster_dither(px_w, px_h, pxarray, n_raster_levels)
    pxarray_reversed = pxarray[::-1]

    return pxarray, pxarray_reversed, len(pxarray), engraved_box


def _raster_line_segments(line, line_start, line_end, direction, pxsize_x, raster_leadin):
    """Walk one scanline and yield the (segment_start, segment_end) index pairs
    of engraved runs: trim leading/trailing whitespace and split on interior
    whitespace wider than 2*raster_leadin (so the head can fast-seek the gaps).
    Indices follow the direction convention; segment_end is already trimmed."""
    whitespace_counter = 0
    on_starting_edge = True
    if direction == 1:  # fwd
        segment_start = line_start
        segment_end = segment_start - 1  # will immediately increment
    else:  # rev
        line = line[::-1]
        segment_start = line_end
        segment_end = segment_start + 1  # will immediately decrement

    for j in range(len(line)):
        segment_end += 1 * direction
        if line[j] == 255:
            whitespace_counter += 1
        elif on_starting_edge:
            # make the first non-white pixel our starting point
            segment_start = segment_end
            on_starting_edge = False
            whitespace_counter = 0
        elif whitespace_counter * pxsize_x <= 2 * raster_leadin:
            # if the interior whitespace is too small, ignore it and travel at normal speeds
            whitespace_counter = 0

        segment_ended = False
        if j == (len(line) - 1):
            segment_ended = True
        elif (
            (whitespace_counter * pxsize_x > 2 * raster_leadin)
            and (line[j + 1] != 255)
            and not (on_starting_edge)
        ):
            # travel to the end of the interior whitespace, backtrack via whitespace_counter
            segment_ended = True

        if segment_ended:
            if direction == 1:  # fwd
                segment_end = segment_end - whitespace_counter + 1  # cut off ending whitespace
            else:  # rev
                segment_end = segment_end + whitespace_counter - 1  # cut off ending whitespace
            yield segment_start, segment_end
            # prime for next segment
            segment_start = segment_end + whitespace_counter * direction
            segment_end = segment_start - 1 * direction
            whitespace_counter = 0


def _job_laser_image(def_, pass_, pxsize_x, pxsize_y, seekrate, feedrate_, intensity_):
    pos = def_["pos"]
    size = def_["size"]
    data = def_["data"]  # in base64, format: jpg, png, gif
    px_w = int(size[0] / pxsize_x)
    px_h = int(size[1] / pxsize_y)

    # note that 0-255 pixel data is halved for serial protocol, so we only get 128 levels max
    n_raster_levels = max(min(round(conf["raster_levels"]), 128), 2)
    if n_raster_levels != conf["raster_levels"]:
        print(
            f"WARN: config raster_levels={conf['raster_levels']} invalid, set to {n_raster_levels}"
        )
    raster_mode = conf["raster_mode"]
    if raster_mode not in ["Forward", "Reverse", "Bidirectional", "NearestNeighbor"]:
        raster_mode = "Bidirectional"
        print("WARN: raster_mode not recognized. Please check your config file.")

    pxarray, pxarray_reversed, px_n, engraved_box = _raster_load_pixels(
        data, px_w, px_h, n_raster_levels
    )

    # assists on, beginning of feed if set to 'feed'
    _feed_assists(pass_, True)

    posx = pos[0]  # left edge location [mm]
    posy = pos[1]  # top edge location [mm]
    line_y = posy + 0.5 * pxsize_y
    line_count = int(size[1] / pxsize_y)
    line_start = line_end = 0

    # warn if there isn't room for the lead-in / lead-out moves. Only the
    # engraved columns matter, as the head never enters an all-white margin.
    if engraved_box is not None:
        engraved_left = posx + engraved_box[0] * pxsize_x
        engraved_right = posx + engraved_box[2] * pxsize_x
        if engraved_left - conf["raster_leadin"] < 0:
            print("WARN: not enough leadin space")
        if engraved_right + conf["raster_leadin"] > conf["workspace"][0]:
            print("WARN: not enough leadout space")

    # set direction
    if raster_mode == "Reverse":
        direction = -1  # 1 is forward, -1 is reverse
    else:  # 'Forward', 'Bidirectional', or 'NearestNeighbor'
        direction = 1

    # NearestNeighbor collects segments (found in forward direction) and emits
    # them reordered after the scanline loop
    nn_segments = []

    for _ in range(line_count):
        line_end += px_w
        line = pxarray[line_start:line_end]
        if not all(px == 255 for px in line):  # skip completely white raster lines
            for segment_start, segment_end in _raster_line_segments(
                line, line_start, line_end, direction, pxsize_x, conf["raster_leadin"]
            ):
                # limits for engraving and leading in/out for this segment.
                # Both ends are pixel edges, and reverse indices count down, so
                # one expression gives the leading edge in either direction.
                pos_start = posx + (segment_start - line_start) * pxsize_x
                pos_end = posx + (segment_end - line_start) * pxsize_x
                if direction == 1:  # fwd
                    pos_leadin = max(pos_start - conf["raster_leadin"], 0)
                    pos_leadout = min(pos_end + conf["raster_leadin"], conf["workspace"][0])
                    orientation = {
                        "leadin": pos_leadin,
                        "start": pos_start,
                        "end": pos_end,
                        "leadout": pos_leadout,
                        "line_y": line_y,
                        "data": pxarray,
                        "lo": segment_start,
                        "hi": segment_end,
                    }
                else:  # rev
                    pos_leadin = min(pos_start + conf["raster_leadin"], conf["workspace"][0])
                    pos_leadout = max(pos_end - conf["raster_leadin"], 0)
                    orientation = {
                        "leadin": pos_leadin,
                        "start": pos_start,
                        "end": pos_end,
                        "leadout": pos_leadout,
                        "line_y": line_y,
                        "data": pxarray_reversed,
                        "lo": px_n - segment_start,
                        "hi": px_n - segment_end,
                    }

                if raster_mode == "NearestNeighbor":
                    nn_segments.append((segment_start, segment_end, line_start, line_y))
                else:
                    _emit_raster_segment(orientation, seekrate, feedrate_, intensity_)

        # prime for next line
        if (raster_mode == "Bidirectional") and (direction == 1):  # fwd
            direction = -1  # switch to rev
        elif (raster_mode == "Bidirectional") and (direction == -1):  # rev
            direction = 1  # switch to fwd
        line_start = line_end
        line_y += pxsize_y

    if raster_mode == "NearestNeighbor":
        _emit_raster_nn(
            nn_segments,
            posx,
            posy,
            posx,
            pxsize_x,
            conf["raster_leadin"],
            conf["workspace"][0],
            pxarray,
            pxarray_reversed,
            px_n,
            seekrate,
            feedrate_,
            intensity_,
        )

    # left on for the next item, the pass end switches it off


def _switch_assists(passes, scope, on):
    """Turn the assists that any of `passes` runs at `scope` on or off.

    Scope is how long an assist stays on: 'feed' while burning, 'pass' for a
    whole pass, 'job' for every pass in the job. Air and aux are independent
    outputs and each picks its own scope.
    """
    for key, turn_on, turn_off in (
        ("air_assist", air_on, air_off),
        ("aux_assist", aux_on, aux_off),
    ):
        if any(pass_.get(key) == scope for pass_ in passes):
            (turn_on if on else turn_off)()


# whether the 'feed' scope assists are currently running, see _feed_assists
_feed_assists_on = False


def _feed_assists(pass_, on):
    """Switch the 'feed' scope assists, holding them on across contiguous burns.

    Only the seek to the next contour separates one burn from the next, so
    switching per contour would cycle the relay hundreds of times on a job of
    small shapes and give the gas no time to come up. This turns them on at the
    first burn and leaves them until the pass ends.
    """
    global _feed_assists_on
    if on != _feed_assists_on:
        _switch_assists([pass_], "feed", on)
        _feed_assists_on = on


def _job_laser_path(def_, pass_, seekrate, feedrate_, intensity_):
    path = def_["data"]
    pierce_time = pass_["pierce_time"]
    for polyline in path:
        if len(polyline) > 0:
            # first vertex -> seek
            feedrate(seekrate)
            if not pass_["seekzero"]:
                intensity(intensity_)
            else:
                intensity(0.0)
            is_2d = len(polyline[0]) == 2
            if is_2d:
                move(polyline[0][0], polyline[0][1])
            else:
                move(polyline[0][0], polyline[0][1], polyline[0][2])
            # a lone vertex with no pierce only seeks, so it needs no assist
            burns = pierce_time > 0 or len(polyline) > 1
            # turn on assists if set to 'feed', ahead of the pierce where the
            # gas clears the melt
            if burns:
                _feed_assists(pass_, True)
            # burn through in place first, otherwise a thick material is still
            # being penetrated as the head sets off
            if pierce_time > 0:
                intensity(intensity_)
                duration(pierce_time)
                dwell()
            # remaining vertices -> feed
            if len(polyline) > 1:
                feedrate(feedrate_)
                intensity(intensity_)
                if is_2d:
                    for i in range(1, len(polyline)):
                        move(polyline[i][0], polyline[i][1])
                else:
                    for i in range(1, len(polyline)):
                        move(polyline[i][0], polyline[i][1], polyline[i][2])
            # left on for the next contour, the pass end switches it off


def job_laser(jobdict):
    """Queue a .dba laser job.
    A job dictionary can define vector and raster passes.
    Unlike gcode it's not procedural but declarative.
    The job dict looks like this:
    ###########################################################################
    {
      "head": {
        "noreturn": True,          # do not return to origin, default: False
        "optimized": 0.08,         # optional, tolerance to which it was optimized, default: 0 (not optimized)
      },
      "passes": [
        {
          "items": [0],            # item by index
          "relative": True,        # optional, default: False
          "seekrate": 6000,        # optional, rate to first vertex
          "feedrate": 2000,        # optional, rate to other vertices
          "intensity": 100,        # optional, default: 0 (in percent)
          "seekzero": False,       # optional, default: True
          "pierce_time": 0,        # optional, default: conf pierce_time
          "pxsize": [0.4],         # optional
          "air_assist": "pass",    # optional (off, feed, pass, job), default: pass
          "aux_assist": "off",     # optional (off, feed, pass, job), default: off
        }
      ],
      "items": [
        {"def":0, "translate":[0,0,0], "color":"#BADA55"}
      ],
      "defs": [
        {"kind":"path", "data":[[[0,10,0]]]},
        {"kind":"fill", "data":[[[0,10,0]]], "pxsize":0.4},
        {"kind":"image", "data":<data in base64>, "pos":[0,0], "size":[300,200]},
        {"kind":"mill", "data":[('G0',(x,y,z)), ('F', 1000), ('G1', (x,y,z))]},
      ],
      "stats":{"items":[{"bbox":[x1,y1,x2,y2], "len":100}], "all":{}}
    }
    ###########################################################################
    """

    if "defs" not in jobdict or "items" not in jobdict:
        print("ERROR: invalid job")
        return

    if "passes" not in jobdict:
        print("NOTICE: no passes defined")
        return

    # raises an exception if the job is not valid
    job_laser_validate(jobdict)

    # reset valves, including the feed scope tally a previous job may have left
    global _feed_assists_on
    _feed_assists_on = False
    air_off()
    aux_off()

    # assists on for the whole job if any pass asks for it
    _switch_assists(jobdict["passes"], "job", True)

    # loop passes
    for pass_ in jobdict["passes"]:
        requested_pxsize = float(pass_.setdefault("pxsize", conf["pxsize"]))
        pxsize_x, pxsize_y = _pass_pxsize(pass_)  # x is 2x horiz resolution
        if requested_pxsize != pxsize_y:
            print(f"WARN: pxsize of {requested_pxsize} mm/px is too small. Setting to {pxsize_y}")
        intensity(0.0)
        pixelwidth(pxsize_x)
        # assists on, beginning of pass if set to 'pass'
        pass_.setdefault("air_assist", "pass")
        pass_.setdefault("aux_assist", "off")
        _switch_assists([pass_], "pass", True)
        pass_.setdefault("seekzero", True)
        pass_.setdefault("pierce_time", conf["pierce_time"])
        seekrate = pass_.setdefault("seekrate", conf["seekrate"])
        feedrate_ = pass_.setdefault("feedrate", conf["feedrate"])
        intensity_ = pass_.setdefault("intensity", 0.0)
        # set absolute/relative
        if not pass_.setdefault("relative", False):
            absolute()
        else:
            relative()
        # loop pass' items
        for itemidx in pass_["items"]:
            item = jobdict["items"][itemidx]
            def_ = jobdict["defs"][item["def"]]
            kind = def_["kind"]
            if kind == "image":
                _job_laser_image(def_, pass_, pxsize_x, pxsize_y, seekrate, feedrate_, intensity_)
            elif kind == "fill" or kind == "path":
                _job_laser_path(def_, pass_, seekrate, feedrate_, intensity_)

        # assists off, end of pass for both 'feed' and 'pass'
        _feed_assists(pass_, False)
        _switch_assists([pass_], "pass", False)

    # assists off, end of job if set to 'job'
    _switch_assists(jobdict["passes"], "job", False)

    # leave machine in absolute mode
    absolute()

    # return to origin
    feedrate(conf["seekrate"])
    intensity(0.0)
    if "head" in jobdict and "noreturn" in jobdict["head"] and jobdict["head"]["noreturn"]:
        pass
    else:
        move(0, 0, 0)


def job_mill_validate(jobdict):
    """Validate that a mill job's G0/G1 targets stay within the work area.

    Mill jobs run in absolute (machine) coordinates, so each axis target is
    bounded to [0, workspace] on that axis. Raises ValueError with a descriptive
    message on the first out-of-bounds move.

    NOTE: the Z bound is conf["workspace"][2]; a mill must configure a non-zero Z
    work-area dimension for any Z move to validate.
    """
    limits = conf["workspace"]
    axes = ("x", "y", "z")

    for defidx, def_ in enumerate(jobdict["defs"]):
        for item in def_["data"]:
            if item[0] in ("G0", "G1"):
                target = item[1]
                for i, axis in enumerate(axes):
                    val = target[i]
                    if val < 0 or val > limits[i]:
                        raise ValueError(
                            f"def {defidx}: {item[0]} {axis}={val} beyond "
                            f"[0, {limits[i]}] of work area"
                        )


def job_mill(jobdict):
    """Queue a .dba mill job.
    A typical mill job dict looks like this:
    ###########################################################################
    {
      "head": {
          "kind": "mill",          # specify a mill job
       },
      "defs": [
        {"data":[('G0',(x,y,z)), ('F', 1000), ('G1', (x,y,z))]},
      ],
    }
    ###########################################################################
    """
    # check job
    if (
        ("head" not in jobdict)
        or ("kind" not in jobdict["head"])
        or (jobdict["head"]["kind"] != "mill")
    ):
        print("NOTICE: not a mill job")
        return

    if "defs" not in jobdict:
        print("ERROR: invalid job")
        return
    # raises ValueError if any move falls outside the work area
    job_mill_validate(jobdict)
    # prime job
    air_off()
    aux_off()
    absolute()
    intensity(0.0)
    seekrate = conf["seekrate"]
    feedrate_ = conf["feedrate"]
    feedrate(seekrate)
    feedrate_active = seekrate
    # run job
    for def_ in jobdict["defs"]:
        path = def_["data"]
        for item in path:
            if item[0] == "G0":
                if feedrate_active != seekrate:
                    feedrate(seekrate)
                    feedrate_active = seekrate
                move(item[1][0], item[1][1], item[1][2])
            elif item[0] == "G1":
                if feedrate_active != feedrate_:
                    feedrate(feedrate_)
                    feedrate_active = feedrate_
                move(item[1][0], item[1][1], item[1][2])
            elif item[0] == "F":
                feedrate_ = item[1]
            elif item[0] == "S":
                # convert RPMs to 0-100%
                ipct = item[1] * (100.0 / conf["mill_max_rpm"])
                intensity(ipct)
            elif item[0] == "MIST":
                if item[1]:
                    air_on()
                elif not item[1]:
                    air_off()
            elif item[0] == "FLOOD":
                if item[1]:
                    aux_on()
                elif not item[1]:
                    aux_off()
    # finalize job
    air_off()
    aux_off()
    absolute()
    feedrate(conf["seekrate"])
    intensity(0.0)
    supermove(z=0)
    supermove(x=0, y=0)


# Floyd-Steinberg dithering algorithm for raster data
"""
Floyd-Steinberg dithering coefficients (1/16):
-------------------
|     |  X  |  7  |
-------------------
|  1  |  5  |  3  |
-------------------
"""


def raster_dither(px_w, px_h, pxarray, n_levels=2):
    pxarray_dithered = pxarray.copy()
    levels = [255 * x / (n_levels - 1) for x in range(0, n_levels)]
    cutoffs = [x + 255 / (n_levels - 1) / 2 for x in levels]

    for i in range(len(pxarray)):
        # clamp accumulated diffusion error into range so a cutoff always matches
        value = min(255, max(0, pxarray_dithered[i]))
        residual = 0
        for j, cutoff in enumerate(cutoffs):
            if value <= cutoff:
                residual = value - levels[j]
                pxarray_dithered[i] = levels[j]
                break
        row = i // px_w
        col = i % px_w
        if col != px_w - 1:
            pxarray_dithered[i + 1] += residual * 7 / 16
        if row != px_h - 1:
            if col != 0:
                pxarray_dithered[i + px_w - 1] += residual * 1 / 16
            pxarray_dithered[i + px_w] += residual * 5 / 16
            if col != px_w - 1:
                pxarray_dithered[i + px_w + 1] += residual * 3 / 16

    return pxarray_dithered


# Functions to keep the computer from sleeping in the middle of a long job
# https://stackoverflow.com/questions/57647034/prevent-sleep-mode-python-wakelock-on-python
def disable_computer_sleep():
    system = platform.system()
    if system == "Windows":
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    elif system == "Linux":
        import subprocess

        args = [
            "sleep.target",
            "suspend.target",
            "hibernate.target",
            "hybrid-sleep.target",
        ]
        try:
            # masking units needs root/polkit; on unprivileged hosts (e.g.
            # Crostini) systemctl prints "Interactive authentication required".
            # Swallow its stderr and continue with a single friendly note.
            result = subprocess.run(["systemctl", "mask", *args], stderr=subprocess.DEVNULL)
            if result.returncode != 0:
                print("INFO: couldn't disable system sleep (needs root); continuing")
        except Exception:
            print("INFO: couldn't disable system sleep; continuing")
    else:  # if system == 'Darwin':
        print(f"Display disabling not implemented in {system}")


def enable_computer_sleep():
    system = platform.system()
    if system == "Windows":
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    elif system == "Linux":
        import subprocess

        args = [
            "sleep.target",
            "suspend.target",
            "hibernate.target",
            "hybrid-sleep.target",
        ]
        try:
            result = subprocess.run(["systemctl", "unmask", *args], stderr=subprocess.DEVNULL)
            if result.returncode != 0:
                print("INFO: couldn't re-enable system sleep (needs root); continuing")
        except Exception:
            print("INFO: couldn't re-enable system sleep; continuing")
    else:  # if system == 'Darwin':
        print(f"Display disabling not implemented in {system}")


if __name__ == "__main__":
    # run like this to profile: python -m cProfile driveboard.py
    connect()
    if connected():
        while not status()["ready"]:
            time.sleep(1)
            sys.stdout.write(".")
        close()
