# -*- coding: UTF-8 -*-
import os
import io
import sys
import time
import json
import copy
import base64
import threading
import itertools
import serial
import serial.tools.list_ports
import datetime
from config import conf, write_config_fields

if not conf['mill_mode']:
    try:
        from PIL import Image
    except ImportError:
        print("Pillow module missing, raster mode will fail.")

__author__  = 'Stefan Hechenberger <stefan@nortd.com>'


################ SENDING PROTOCOL
CMD_STOP = chr(1)
CMD_RESUME = chr(2)
CMD_STATUS = chr(3)
CMD_SUPERSTATUS = chr(4)
CMD_CHUNK_PROCESSED = chr(5)
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

################


################ RECEIVING PROTOCOL

# status: error flags
ERROR_SERIAL_STOP_REQUEST = '!'
ERROR_RX_BUFFER_OVERFLOW = '"'

ERROR_LIMIT_HIT_X1 = '$'
ERROR_LIMIT_HIT_X2 = '%'
ERROR_LIMIT_HIT_Y1 = '&'
ERROR_LIMIT_HIT_Y2 = '*'
ERROR_LIMIT_HIT_Z1 = '+'
ERROR_LIMIT_HIT_Z2 = '-'

ERROR_INVALID_MARKER = '#'
ERROR_INVALID_DATA = ':'
ERROR_INVALID_COMMAND = '<'
ERROR_INVALID_PARAMETER ='>'
ERROR_TRANSMISSION_ERROR ='='

# status: info flags
INFO_IDLE_YES = 'A'
INFO_DOOR_OPEN = 'B'
INFO_CHILLER_OFF = 'C'

# status: info params
INFO_POS_X = 'x'
INFO_POS_Y = 'y'
INFO_POS_Z = 'z'
INFO_VERSION = 'v'
INFO_BUFFER_UNDERRUN = 'w'
INFO_STACK_CLEARANCE = 'u'

INFO_HELLO = '~'

INFO_OFFSET_X = 'a'
INFO_OFFSET_Y = 'b'
INFO_OFFSET_Z = 'c'
# INFO_TARGET_X = 'd'
# INFO_TARGET_Y = 'e'
# INFO_TARGET_Z = 'f'
INFO_FEEDRATE = 'g'
INFO_INTENSITY = 'h'
INFO_DURATION = 'i'
INFO_PIXEL_WIDTH = 'j'
################


# reverse lookup for commands, for debugging
# NOTE: have to be in sync with above definitions
markers_tx = {
    chr(1): "CMD_STOP",
    chr(2): "CMD_RESUME",
    chr(3): "CMD_STATUS",
    chr(4): "CMD_SUPERSTATUS",
    chr(5): "CMD_CHUNK_PROCESSED",
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
    chr(16): "CMD_RASTER_DATA_START",
    chr(17): "CMD_RASTER_DATA_END",
    chr(6): "STATUS_END",

    # status: error flags
    '!': "ERROR_SERIAL_STOP_REQUEST",
    '"': "ERROR_RX_BUFFER_OVERFLOW",

    '$': "ERROR_LIMIT_HIT_X1",
    '%': "ERROR_LIMIT_HIT_X2",
    '&': "ERROR_LIMIT_HIT_Y1",
    '*': "ERROR_LIMIT_HIT_Y2",
    '+': "ERROR_LIMIT_HIT_Z1",
    '-': "ERROR_LIMIT_HIT_Z2",

    '#': "ERROR_INVALID_MARKER",
    ':': "ERROR_INVALID_DATA",
    '<': "ERROR_INVALID_COMMAND",
    '>': "ERROR_INVALID_PARAMETER",
    '=': "ERROR_TRANSMISSION_ERROR",

    # status: info flags
    'A': "INFO_IDLE_YES",
    'B': "INFO_DOOR_OPEN",
    'C': "INFO_CHILLER_OFF",

    # status: info params
    'x': "INFO_POS_X",
    'y': "INFO_POS_Y",
    'z': "INFO_POS_Z",
    'v': "INFO_VERSION",
    'w': "INFO_BUFFER_UNDERRUN",
    'u': "INFO_STACK_CLEARANCE",

    '~': "INFO_HELLO",

    'a': "INFO_OFFSET_X",
    'b': "INFO_OFFSET_Y",
    'c': "INFO_OFFSET_Z",
    # 'd': "INFO_TARGET_X",
    # 'e': "INFO_TARGET_Y",
    # 'f': "INFO_TARGET_Z",
    'g': "INFO_FEEDRATE",
    'h': "INFO_INTENSITY",
    'i': "INFO_DURATION",
    'j': "INFO_PIXEL_WIDTH",
}

SerialLoop = None
fallback_msg_thread = None


class SerialLoopClass(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        
        self.verbose = False

        self.device = None
        self.tx_buffer = []
        self.tx_pos = 0

        # TX_CHUNK_SIZE - this is the number of bytes to be
        # written to the device in one go. It needs to match the device.
        self.TX_CHUNK_SIZE = 16
        self.RX_CHUNK_SIZE = 32
        self.FIRMBUF_SIZE = 256  # needs to match device firmware
        self.firmbuf_used = 0

        # used for calculating percentage done
        self.job_size = 0

        # status flags
        self._status = {}    # last complete status frame
        self._s = {}         # status fram currently assembling
        self.reset_status()
        self._paused = False

        self.request_stop = False
        self.request_resume = False
        self.request_status = 2       # 0: no request, 1: normal request, 2: super request

        self.pdata_count = 0
        self.pdata_nums = [128, 128, 128, 192]

        threading.Thread.__init__(self)
        self.stop_processing = False

        self.deamon = True  # kill thread when main thread exits

        # lock mechanism for chared data
        # see: http://effbot.org/zone/thread-synchronization.htm
        self.lock = threading.Lock()


    def reset_status(self):
        self._status = {
            'ready': False,                 # is hardware idle (and not stop mode)
            'serial': False,                # is serial connected
            'appver':conf['version'],
            'firmver': None,
            'paused': False,
            'pos':[0.0, 0.0, 0.0],
            'underruns': 0,                 # how many times machine is waiting for serial data
            'stackclear': 999999,           # minimal stack clearance (must stay above 0)
            'progress': 1.0,

            ### stop conditions
            # indicated when key present
            'stops': {},
            # possible keys:
            # x1, x2, y1, y2, z1, z2,
            # requested, buffer, marker, data, command, parameter, transmission

            'info':{},
            # possible keys: door, chiller

            ### super
            'offset': [0.0, 0.0, 0.0],
            # 'pos_target': [0.0, 0.0, 0.0],
            'feedrate': 0.0,
            'intensity': 0.0,
            'duration': 0.0,
            'pixelwidth': 0.0
        }
        self._s = copy.deepcopy(self._status)


    def send_command(self, command):
        self.tx_buffer.append(ord(command))
        self.job_size += 1


    def send_param(self, param, val):
        # num to be [-134217.728, 134217.727], [-2**27, 2**27-1]
        # three decimals are retained
        num = int(round(((val+134217.728)*1000)))
        char0 = (num&127)+128
        char1 = ((num&(127<<7))>>7)+128
        char2 = ((num&(127<<14))>>14)+128
        char3 = ((num&(127<<21))>>21)+128
        self.tx_buffer.append(char0)
        self.tx_buffer.append(char1)
        self.tx_buffer.append(char2)
        self.tx_buffer.append(char3)
        self.tx_buffer.append(ord(param))
        self.job_size += 5


    def send_raster_data(self, data, start, end):
        count = 2
        with self.lock:
            self.tx_buffer.append(ord(CMD_RASTER_DATA_START))
        for val in itertools.islice(data, start, end):
            with self.lock:
                self.tx_buffer.append(int(0.5*(255-val))+128)
            count += 1
        with self.lock:
            self.tx_buffer.append(ord(CMD_RASTER_DATA_END))
            self.job_size += count


    def run(self):
        """Main loop of the serial thread."""
        last_write = 0
        last_status_request = 0
        while True:
            if self.stop_processing:
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
                    except OSError:
                        print("ERROR: serial got disconnected 1.")
                        self.stop_processing = True
                        self._status['serial'] = False
                        self._status['ready'] = False
                    except ValueError:
                        print("ERROR: serial got disconnected 2.")
                        self.stop_processing = True
                        self._status['serial'] = False
                        self._status['ready']  = False
                else:
                    print("ERROR: serial got disconnected 3.")
                    self.stop_processing = True
                    self._status['serial'] = False
                    self._status['ready']  = False
                # status request
                if time.time()-last_status_request > 0.5:
                    if self._status['ready']:
                        self.request_status = 2  # ready -> super request
                    else:
                        self.request_status = 1  # processing -> normal request
                    last_status_request = time.time()
                # flush stdout, so print shows up timely
                sys.stdout.flush()
            time.sleep(0.004)


    def _serial_read(self):
        chunk = self.device.read(self.RX_CHUNK_SIZE)
        if self.verbose and chunk != b'':
            timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-4]
            print(timestamp + ' Receiving: ' + prettify_serial(chunk, markers=markers_rx))
        for data_num in chunk:
            data_char = chr(data_num)
            if data_num < 32:  ### flow
                if data_char == CMD_CHUNK_PROCESSED:
                    self.firmbuf_used -= self.TX_CHUNK_SIZE
                    if self.firmbuf_used < 0:
                        print("ERROR: firmware buffer tracking to low")
                elif data_char == STATUS_END:
                    # status frame complete, compile status
                    self._status, self._s = self._s, self._status  # flip
                    self._status['paused'] = self._paused
                    self._status['serial'] = bool(self.device)
                    if self.job_size == 0:
                        self._status['progress'] = 1.0
                    else:
                        self._status['progress'] = \
                          round(self.tx_pos/float(self.job_size),3)
                    self._s['stops'].clear()
                    self._s['info'].clear()
                    self._s['ready'] = False
                    self._s['underruns'] = self._status['underruns']
                    self._s['stackclear'] = self._status['stackclear']
            elif 31 < data_num < 65:  ### stop error markers
                # chr is in [!-@], process flag
                if data_char == ERROR_LIMIT_HIT_X1:
                    self._s['stops']['x1'] = True
                    # print "ERROR firmware: limit hit x1"
                elif data_char == ERROR_LIMIT_HIT_X2:
                    self._s['stops']['x2'] = True
                    # print "ERROR firmware: limit hit x2"
                elif data_char == ERROR_LIMIT_HIT_Y1:
                    self._s['stops']['y1'] = True
                    # print "ERROR firmware: limit hit y1"
                elif data_char == ERROR_LIMIT_HIT_Y2:
                    self._s['stops']['y2'] = True
                    # print "ERROR firmware: limit hit y2"
                elif data_char == ERROR_LIMIT_HIT_Z1:
                    self._s['stops']['z1'] = True
                    # print "ERROR firmware: limit hit z1"
                elif data_char == ERROR_LIMIT_HIT_Z2:
                    self._s['stops']['z2'] = True
                    # print "ERROR firmware: limit hit z2"
                elif data_char == ERROR_SERIAL_STOP_REQUEST:
                    self._s['stops']['requested'] = True
                    # print "ERROR firmware: stop request"
                    print("INFO firmware: stop request")
                elif data_char == ERROR_RX_BUFFER_OVERFLOW:
                    self._s['stops']['buffer'] = True
                    print("ERROR firmware: rx buffer overflow")
                elif data_char == ERROR_INVALID_MARKER:
                    self._s['stops']['marker'] = True
                    print("ERROR firmware: invalid marker")
                elif data_char == ERROR_INVALID_DATA:
                    self._s['stops']['data'] = True
                    print("ERROR firmware: invalid data")
                elif data_char == ERROR_INVALID_COMMAND:
                    self._s['stops']['command'] = True
                    print("ERROR firmware: invalid command")
                elif data_char == ERROR_INVALID_PARAMETER:
                    self._s['stops']['parameter'] = True
                    print("ERROR firmware: invalid parameter")
                elif data_char == ERROR_TRANSMISSION_ERROR:
                    self._s['stops']['transmission'] = True
                    print("ERROR firmware: transmission")
                else:
                    print("ERROR: invalid stop error marker")
                # in stop mode, print recent transmission, unless stop request, or limit
                if data_char != ERROR_SERIAL_STOP_REQUEST and \
                          data_char != ERROR_LIMIT_HIT_X1 and \
                          data_char != ERROR_LIMIT_HIT_X2 and \
                          data_char != ERROR_LIMIT_HIT_Y1 and \
                          data_char != ERROR_LIMIT_HIT_Y2 and \
                          data_char != ERROR_LIMIT_HIT_Z1 and \
                          data_char != ERROR_LIMIT_HIT_Z2:
                    recent_data = self.tx_buffer[max(0,self.tx_pos-128):self.tx_pos]
                    print("RECENT TX BUFFER:")
                    for data_num in recent_data:
                        data_char = chr(data_num)
                        if data_char in markers_tx:
                            print("\t%s" % (markers_tx[data_char]))
                        elif 127 < data_num < 256:
                            print("\t(data byte)")
                        else:
                            print("\t(invalid)")
                    print("----------------")
                # stop mode housekeeping
                self.tx_buffer = []
                self.tx_pos = 0
                self.job_size = 0
                self._paused = False
                self.device.flushOutput()
                self.pdata_count = 0
                self._s['ready'] = True # ready but in stop mode
            elif 64 < data_num < 91:  # info flags
                # data_char is in [A-Z], info flag
                if data_char == INFO_IDLE_YES:
                    if not self.tx_buffer:
                        self._s['ready'] = True
                elif data_char == INFO_DOOR_OPEN:
                    self._s['info']['door'] = True
                elif data_char == INFO_CHILLER_OFF:
                    self._s['info']['chiller'] = True
                else:
                    print("ERROR: invalid info flag")
                    sys.stdout.write('(',data_char,',',data_num,')')
                self.pdata_count = 0
            elif 96 < data_num < 123:  # parameter
                # data_char is in [a-z], process parameter
                num = ((((self.pdata_nums[3]-128)*2097152
                       + (self.pdata_nums[2]-128)*16384
                       + (self.pdata_nums[1]-128)*128
                       + (self.pdata_nums[0]-128) )- 134217728)/1000.0)
                if data_char == INFO_POS_X:
                    self._s['pos'][0] = num
                elif data_char == INFO_POS_Y:
                    self._s['pos'][1] = num
                elif data_char == INFO_POS_Z:
                    self._s['pos'][2] = num
                elif data_char == INFO_VERSION:
                    num = str(int(num)/100.0)
                    self._s['firmver'] = num
                elif data_char == INFO_BUFFER_UNDERRUN:
                    self._s['underruns'] = num
                # super status
                elif data_char == INFO_OFFSET_X:
                    self._s['offset'][0] = num
                elif data_char == INFO_OFFSET_Y:
                    self._s['offset'][1] = num
                elif data_char == INFO_OFFSET_Z:
                    self._s['offset'][2] = num
                elif data_char == INFO_FEEDRATE:
                    self._s['feedrate'] = num
                elif data_char == INFO_INTENSITY:
                    self._s['intensity'] = 100*num/255
                elif data_char == INFO_DURATION:
                    self._s['duration'] = num
                elif data_char == INFO_PIXEL_WIDTH:
                    self._s['pixelwidth'] = num
                elif data_char == INFO_STACK_CLEARANCE:
                    self._s['stackclear'] = num
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
        ### send buffer chunk
        if self.tx_buffer and len(self.tx_buffer) > self.tx_pos:
            if not self._paused:
                if (self.FIRMBUF_SIZE - self.firmbuf_used) > self.TX_CHUNK_SIZE:
                    try:
                        # to_send = ''.join(islice(self.tx_buffer, 0, self.TX_CHUNK_SIZE))
                        to_send = self.tx_buffer[self.tx_pos:self.tx_pos+self.TX_CHUNK_SIZE]
                        expectedSent = len(to_send)
                        if self.verbose:
                            timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-4]
                            print(timestamp + ' Sending: ' + prettify_serial(to_send, markers=markers_tx))

                        # by protocol duplicate every char
                        to_send_double = []
                        for n in to_send:
                            to_send_double.append(n)
                            to_send_double.append(n)
                        to_send = to_send_double
                        #
                        t_prewrite = time.time()
                        actuallySent = self.device.write(to_send)
                        if actuallySent != expectedSent*2:
                            print("ERROR: write did not complete")
                            assumedSent = 0
                        else:
                            assumedSent = expectedSent
                            self.firmbuf_used += assumedSent
                            if self.firmbuf_used > self.FIRMBUF_SIZE:
                                print("ERROR: firmware buffer tracking too high")
                        if time.time() - t_prewrite > 0.1:
                            print("WARN: write delay 1")
                    except serial.SerialTimeoutException:
                        assumedSent = 0
                        print("ERROR: writeTimeoutError 2")
                    self.tx_pos += assumedSent
        else:
            if self.tx_buffer:  # job finished sending
                self.job_size = 0
                self.tx_buffer = []
                self.tx_pos = 0


    def _send_char(self, char):
        try:
            t_prewrite = time.time()
            # self.device.write(char)
            # print "send_char: [%s,%s]" % (str(ord(char)),str(ord(char)))
            if self.verbose:
                timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-4]
                print(timestamp + ' Sending: ' + prettify_serial(ord(char), markers=markers_tx))
            self.device.write([ord(char),ord(char)])  # by protocol send twice
            if time.time() - t_prewrite > 0.1:
                pass
                # print "WARN: write delay 2"
        except serial.SerialTimeoutException:
            print("ERROR: writeTimeoutError 1")


###########################################################################
### API ###################################################################
###########################################################################

def prettify_serial(chunk, markers=markers_tx):
    string = ''
    if not hasattr(prettify_serial, "tx_pdata_nums"):
        prettify_serial.rx_pdata_nums = [128, 128, 128, 192]
        prettify_serial.rx_pdata_count = 0
        prettify_serial.tx_pdata_nums = [128, 128, 128, 192]
        prettify_serial.tx_pdata_count = 0
        prettify_serial.tx_rasterstream = False
        prettify_serial.tx_rastercount = 0
    
    if isinstance(chunk, int):
        chunk = [chunk] # make integer inputs iterable
    
    for i in range(len(chunk)):
        data = chunk[i]
        if data >= 128:
            string += str(data) + ' '
            if (markers == markers_tx) and prettify_serial.tx_rasterstream:
                prettify_serial.tx_rastercount += 1
            elif markers == markers_tx:
                prettify_serial.tx_pdata_nums[prettify_serial.tx_pdata_count] = data
                prettify_serial.tx_pdata_count += 1
            elif markers == markers_rx:
                prettify_serial.rx_pdata_nums[prettify_serial.rx_pdata_count] = data
                prettify_serial.rx_pdata_count += 1
        elif (data < 128):
            if (markers == markers_tx) and (markers[chr(data)] not in ["CMD_STATUS", "CMD_SUPERSTATUS"]):
                prettify_serial.tx_pdata_count = 0
                prettify_serial.tx_pdata_nums = [128, 128, 128, 192]
            else:
                prettify_serial.rx_pdata_count = 0
                prettify_serial.rx_pdata_nums = [128, 128, 128, 192]

            if markers[chr(data)] == 'CMD_RASTER_DATA_START':
                prettify_serial.tx_rasterstream = True
                prettify_serial.tx_rastercount = 0
            elif markers[chr(data)] == 'CMD_RASTER_DATA_END':
                prettify_serial.tx_rasterstream = False
                string += '(' + str(prettify_serial.tx_rastercount) + ') '

            string += markers[chr(data)] + ', '

        if prettify_serial.tx_pdata_count == 4:
            num = ((((prettify_serial.tx_pdata_nums[3]-128)*2097152
                + (prettify_serial.tx_pdata_nums[2]-128)*16384
                + (prettify_serial.tx_pdata_nums[1]-128)*128
                + (prettify_serial.tx_pdata_nums[0]-128) )- 134217728)/1000.0)
            prettify_serial.tx_pdata_count = 0
            prettify_serial.tx_pdata_nums = [128, 128, 128, 192]
            string += '(' + str(num) + ') '
        elif prettify_serial.rx_pdata_count == 4:
            num = ((((prettify_serial.rx_pdata_nums[3]-128)*2097152
                + (prettify_serial.rx_pdata_nums[2]-128)*16384
                + (prettify_serial.rx_pdata_nums[1]-128)*128
                + (prettify_serial.rx_pdata_nums[0]-128) )- 134217728)/1000.0)
            prettify_serial.rx_pdata_count = 0
            prettify_serial.rx_pdata_nums = [128, 128, 128, 192]
            string += '(' + str(num) + ') '

    if len(string) >= 2 and string[-2] == ',':
        string = string[:-2]
    elif len(string) >= 1 and string[-1] == ' ':
        string = string[:-1]

    return string


def find_controller(baudrate=conf['baudrate'], verbose=True):
    iterator = sorted(serial.tools.list_ports.comports())
    # look for Arduinos
    arduinos = []
    for port, desc, hwid in iterator:
        if "uino" in desc:
            arduinos.append(port)
    # check these arduinos for driveboard firmware, take first
    for port in arduinos:
        try:
            s = serial.Serial(port=port, baudrate=baudrate, timeout=2.0)
            lasaur_hello = s.read(8)
            if lasaur_hello.find(ord(INFO_HELLO)) > -1:
                s.close()
                return port
            s.close()
        except serial.SerialException:
            pass
    # check all comports for driveboard firmware
    for port, desc, hwid in iterator:
        try:
            s = serial.Serial(port=port, baudrate=baudrate, timeout=2.0)
            lasaur_hello = s.read(8)
            if lasaur_hello.find(ord(INFO_HELLO)) > -1:
                s.close()
                return port
            s.close()
        except serial.SerialException:
            pass
    # handle the case Arduino without firmware
    if arduinos:
        return arduinos[0]
    # none found
    if verbose:
        print("ERROR: No controller found.")
    return None


def connect(port=conf['serial_port'], baudrate=conf['baudrate'], verbose=True):
    global SerialLoop
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
            if conf['usb_reset_hack']:
                import flash
                flash.usb_reset_hack()
            # connect
            SerialLoop.device = serial.Serial(port, baudrate, timeout=0, writeTimeout=4)
            if conf['hardware'] == 'standard':
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
                        print("Connected on serial port: %s" % (port))
                    break

            SerialLoop.start()  # this calls run() in a thread
        except serial.SerialException:
            SerialLoop = None
            if verbose:
                print("ERROR: Cannot connect serial on port: %s" % (port))
    else:
        if verbose:
            print("ERROR: disconnect first")


def connect_withfind(port=conf['serial_port'], baudrate=conf['baudrate'], verbose=True):
    connect(port=port, baudrate=baudrate, verbose=verbose)
    if not connected():
        # try finding driveboard
        if verbose:
            print("WARN: Cannot connect to configured serial port.")
            print("INFO: Trying to find port.")
        serialfindresult = find_controller(verbose=verbose)
        if serialfindresult:
            if verbose:
                print("INFO: Hardware found at %s." % serialfindresult)
            connect(port=serialfindresult, baudrate=baudrate, verbose=verbose)
            if not connected():  # special case arduino found, but no firmware
                yesno = input("Firmware appears to be missing. Want to flash-upload it (Y/N)? ")
                if yesno in ('Y', 'y'):
                    ret = flash(serial_port=serialfindresult)
                    if ret == 0:
                        connect(port=serialfindresult, baudrate=baudrate, verbose=verbose)
        if connected():
            if verbose:
                print("INFO: Connected at %s." % serialfindresult)
            conf['serial_port'] = serialfindresult
            write_config_fields({'serial_port':serialfindresult})
        else:
            if verbose:
                print("-----------------------------------------------------------------------------")
                print("How to configure:")
                print("https://github.com/nortd/driveboardapp/blob/master/docs/configure.md")
                print("-----------------------------------------------------------------------------")


def connected():
    global SerialLoop
    return SerialLoop and bool(SerialLoop.device)


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
    else:
        ret = False
    SerialLoop = None
    return ret


def flash(serial_port=conf['serial_port'], firmware=conf['firmware']):
    import flash
    reconnect = False
    if connected():
        close()
        reconnect = True
    ret = flash.flash_upload(serial_port=serial_port, firmware=firmware)
    if reconnect:
        connect()
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
        connect()


def status():
    """Get status."""
    if connected():
        global SerialLoop
        with SerialLoop.lock:
            stats = copy.deepcopy(SerialLoop._status)
            stats['serial'] = connected()  # make sure serial flag is up-to-date
        return stats
    else:
        return {'serial':False, 'ready':False}


def homing():
    """Run homing cycle."""
    global SerialLoop
    with SerialLoop.lock:
        if SerialLoop._status['ready'] or SerialLoop._status['stops']:
            SerialLoop.request_resume = True  # to recover from a stop mode
            SerialLoop.send_command(CMD_HOMING)
        else:
            print("WARN: ignoring homing command while job running")


def feedrate(val):
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_param(PARAM_FEEDRATE, val)


def intensity(val):
    global SerialLoop
    with SerialLoop.lock:
        val = max(min(255*val/100, 255), 0)
        SerialLoop.send_param(PARAM_INTENSITY, val)


def duration(val):
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_param(PARAM_DURATION, val)


def pixelwidth(val):
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_param(PARAM_PIXEL_WIDTH, val)


def relative():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_REF_RELATIVE)


def absolute():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_REF_ABSOLUTE)


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
    """Moves in machine coordinates bypassing any offsets."""
    global SerialLoop
    with SerialLoop.lock:
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
            SerialLoop._paused = True


def unpause():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop._paused = False


def stop():
    """Force stop condition."""
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.tx_buffer = []
        SerialLoop.tx_pos = 0
        SerialLoop.job_size = 0
        SerialLoop.request_stop = True


def unstop():
    """Resume from stop condition."""
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.request_resume = True


def air_on():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AIR_ENABLE)


def air_off():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AIR_DISABLE)


def aux_on():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AUX_ENABLE)


def aux_off():
    global SerialLoop
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_AUX_DISABLE)

def pulse():
    print('pulse')
    global SerialLoop
    air_on()
    # aux_on()
    # turn the laser on for a short pulse
    intensity(50.0)
    duration(0.25)
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_DWELL)
    # keep air on for a bit longer
    intensity(0.0)
    duration(0.75)
    with SerialLoop.lock:
        SerialLoop.send_command(CMD_DWELL)
    air_off()
    # aux_off()


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
    jobdict = json.load(open(filepath))
    job(jobdict)


def job(jobdict):
    if 'head' in jobdict:
        if 'kind' in jobdict['head'] and jobdict['head']['kind'] == 'mill':
            job_mill(jobdict)
        else:
            job_laser(jobdict)
    else:
        print("INFO: not a valid job, 'head' entry missing")


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
              "items": [0],          # item by index
              "relative": True,      # optional, default: False
              "seekrate": 6000,      # optional, rate to first vertex
              "feedrate": 2000,      # optional, rate to other vertices
              "intensity": 100,      # optional, default: 0 (in percent)
              "seekzero": False,     # optional, default: True
              "pierce_time": 0,      # optional, default: 0
              "pxsize": [0.4],       # optional
              "air_assist": "pass",  # optional (feed, pass, off), default: pass
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

    if not 'defs' in jobdict or not 'items' in jobdict:
        print("ERROR: invalid job")
        return

    if not 'passes' in jobdict:
        print("NOTICE: no passes defined")
        return

    # reset valves
    air_off()
    # aux_off()

    # loop passes
    for pass_ in jobdict['passes']:
        if 'pxsize' in pass_:
            pxsize_y = float(pass_['pxsize'])
        else:
            pxsize_y = float(conf['pxsize'])
        pxsize_y = max(pxsize_y, 0.01)  # prevent div by 0
        intensity(0.0)
        pxsize_x = pxsize_y/2.0  # use 2x horiz resol.
        pixelwidth(pxsize_x)
        # assists on, beginning of pass if set to 'pass'
        if 'air_assist' in pass_:
            if pass_['air_assist'] == 'pass':
                air_on()
        else:
            air_on()    # also default this behavior
        # if 'aux_assist' in pass_ and pass_['aux_assist'] == 'pass':
        #     aux_on()
        # seekrate
        if 'seekrate' in pass_:
            seekrate = pass_['seekrate']
        else:
            seekrate = conf['seekrate']
        # feedrate
        if 'feedrate' in pass_:
            feedrate_ = pass_['feedrate']
        else:
            feedrate_ = conf['feedrate']
        # intensity
        if 'intensity' in pass_:
            intensity_ = pass_['intensity']
        else:
            intensity_ = 0.0
        # set absolute/relative
        if 'relative' not in pass_ or not pass_['relative']:
            absolute()
        else:
            relative()
        # loop pass' items
        for itemidx in pass_['items']:
            item = jobdict['items'][itemidx]
            def_ = jobdict['defs'][item['def']]
            kind = def_['kind']
            if kind == "image":
                pos = def_["pos"]
                size = def_["size"]
                data = def_["data"]  # in base64, format: jpg, png, gif
                px_w = int(size[0]/pxsize_x)
                px_h = int(size[1]/pxsize_y)
                raster_mode = conf['raster_mode']
                if raster_mode not in ['Forward', 'Reverse', 'Bidirectional']:
                    raster_mode = 'Bidirectional'
                    print("WARN: raster_mode not recognized. Please check your config file.")
                
                # create image obj, convert to grayscale, scale, loop through lines
                imgobj = Image.open(io.BytesIO(base64.b64decode(data[22:])))
                imgobj = imgobj.resize((px_w,px_h), resample=Image.BICUBIC)
                if imgobj.mode == 'RGBA':
                    imgbg = Image.new('RGBA', imgobj.size, (255, 255, 255))
                    imgbg.paste(imgobj, imgobj)
                    imgobj = imgbg.convert("L")
                else:
                    imgobj = imgobj.convert("L")

                # assists on, beginning of feed if set to 'feed'
                if 'air_assist' in pass_ and pass_['air_assist'] == 'feed':
                    air_on()
                # if 'aux_assist' in pass_ and pass_['aux_assist'] == 'feed':
                #     aux_on()

                # extract raw pixel data into one large list 
                # 0 = black / full power
                # 255 = white / transparent / no power
                pxarray = list(imgobj.getdata())
                pxarray[:] = (value for value in pxarray if type(value) is not str)
                pxarray_reversed = pxarray[::-1]
                px_n = len(pxarray)

                posx = pos[0] # left edge location [mm]
                posy = pos[1] # top edge location [mm]
                line_y = posy + 0.5*pxsize_y
                line_count = int(size[1]/pxsize_y)
                line_start = line_end = 0

                # calc leadin/out
                pos_leadin = posx - conf['raster_leadin']
                if pos_leadin < 0:
                    print("WARN: not enough leadin space")
                    pos_leadin = 0
                pos_leadout = posx + size[0] + conf['raster_leadin']
                if pos_leadout > conf['workspace'][0]:
                    print("WARN: not enough leadout space")
                    pos_leadout = conf['workspace'][0]
                
                # print("mm: %s|%s|%s  h:%s" % ( posx + 0.5*pxsize_x - pos_leadin, size[0], pos_leadout - (posx + size[0] - 0.5*pxsize_x), size[1]))
                # print("px: |%s|  raster_size:%s" % (px_w, pxsize_y))
                # print(len(pxarray))
                # print((px_w*px_h))

                # set direction
                if raster_mode == 'Reverse':
                    direction = -1 # 1 is forward, -1 is reverse
                else: # if 'Forward' or 'Bidirectional'
                    direction = 1 

                # we don't want to waste time at low speeds travelling over whitespace where there is no engraving going on
                # so, chop off all whitespace at the beginning and end of each line
                # additionally, break the line into segments so that large interior whitespaces can be travelled over quicker
                # the threshold for a "large" interior whitespace is 2x the raster_leadin distance so we can still lead in/out properly
                for i in range(line_count):
                    line_end += px_w
                    line = pxarray[line_start:line_end]
                    if not all(px == 255 for px in line): # skip completely white raster lines
                        whitespace_counter = 0
                        on_starting_edge = True
                        if direction == 1: # fwd
                            segment_start = line_start
                            segment_end = segment_start - 1 # will immediately increment
                        elif direction == -1: # rev
                            line = line[::-1]
                            segment_start = line_end
                            segment_end = segment_start + 1 # will immediately decrement

                        for j in range(len(line)):
                            segment_end += 1*direction
                            if line[j] == 255:
                                whitespace_counter += 1
                            elif on_starting_edge: 
                                # make the first non-white pixel our starting point
                                segment_start = segment_end
                                on_starting_edge = False
                                whitespace_counter = 0
                            elif whitespace_counter*pxsize_x <= 2*conf['raster_leadin']:
                                # if the interior whitespace is too small, ignore it and travel at normal speeds
                                whitespace_counter = 0
                            
                            segment_ended = False
                            if j == (len(line) - 1):
                                segment_ended = True
                            elif (whitespace_counter*pxsize_x > 2*conf['raster_leadin']) and (line[j+1] != 255) and not (on_starting_edge):
                                # we travel all the way to the end of the interior whitespace, and use whitespace_counter to figure out how far to backtrack
                                segment_ended = True

                            if segment_ended:
                                # calculate the limits for engraving and leading in/out for this segment
                                if direction == 1: # fwd
                                    segment_end = segment_end - whitespace_counter + 1 # cut off the ending whitespace
                                    pos_start = posx + (segment_start - line_start + 0.5)*pxsize_x
                                    pos_end = posx + (segment_end - line_start - 0.5)*pxsize_x
                                    pos_leadin = max(posx + (segment_start - line_start)*pxsize_x - conf['raster_leadin'], 0) # ensure we stay in the workspace
                                    pos_leadout = min(posx + (segment_end - line_start)*pxsize_x + conf['raster_leadin'], conf['workspace'][0]) # ensure we stay in the workspace
                                elif direction == -1: # rev
                                    segment_end = segment_end + whitespace_counter - 1 # cut off the ending whitespace
                                    pos_start = posx + (segment_start - line_start - 0.5)*pxsize_x
                                    pos_end = posx + (segment_end - line_start + 0.5)*pxsize_x
                                    pos_leadin = min(posx + (segment_start - line_start)*pxsize_x + conf['raster_leadin'], conf['workspace'][0]) # ensure we stay in the workspace
                                    pos_leadout = max(posx + (segment_end - line_start)*pxsize_x - conf['raster_leadin'], 0) # ensure we stay in the workspace
                                
                                # write out the movement and engraving info for the segment
                                intensity(0.0) # intensity for seek and lead-in
                                feedrate(seekrate) # feedrate for seek
                                move(pos_leadin, line_y) # seek to lead-in start              
                                feedrate(feedrate_) # feedrate for lead-in, raster, and lead-out
                                move(pos_start, line_y) # lead-in
                                intensity(intensity_) # intensity for raster move
                                rastermove(pos_end, line_y) # raster move
                                if direction == 1: # fwd
                                    rasterdata(pxarray, segment_start, segment_end) # stream raster data for above rastermove
                                elif direction == -1: # rev
                                    rasterdata(pxarray_reversed, px_n - segment_start, px_n - segment_end) # stream raster data for above rastermove
                                intensity(0.0) # intensity for lead-out
                                move(pos_leadout, line_y) # lead-out
                                
                                # prime for next segment
                                segment_start = segment_end + whitespace_counter*direction
                                segment_end = segment_start - 1*direction
                                segment_ended = False
                                whitespace_counter = 0

                    # prime for next line
                    if (raster_mode == 'Bidirectional') and (direction == 1): # fwd
                        direction = -1 # switch to rev
                    elif (raster_mode == 'Bidirectional') and (direction == -1): # rev
                        direction = 1 # switch to fwd
                    line_start = line_end
                    line_y += pxsize_y

                # assists off, end of feed if set to 'feed'
                if 'air_assist' in pass_ and pass_['air_assist'] == 'feed':
                    air_off()
                # if 'aux_assist' in pass_ and pass_['aux_assist'] == 'feed':
                #     aux_off()

            elif kind == "fill" or kind == "path":
                path = def_['data']
                for polyline in path:
                    if len(polyline) > 0:
                        # first vertex -> seek
                        feedrate(seekrate)
                        if 'seekzero' in pass_ and not pass_['seekzero']:
                            intensity(intensity_)
                        else:
                            intensity(0.0)
                        is_2d = len(polyline[0]) == 2
                        if is_2d:
                            move(polyline[0][0], polyline[0][1])
                        else:
                            move(polyline[0][0], polyline[0][1], polyline[0][2])
                        # remaining verteces -> feed
                        if len(polyline) > 1:
                            feedrate(feedrate_)
                            intensity(intensity_)
                            # turn on assists if set to 'feed'
                            # also air_assist defaults to 'feed'
                            if 'air_assist' in pass_ and pass_['air_assist'] == 'feed':
                                air_on()
                            # if 'aux_assist' in pass_ and pass_['aux_assist'] == 'feed':
                            #     aux_on()
                            # TODO dwell according to pierce time
                            if is_2d:
                                for i in range(1, len(polyline)):
                                    move(polyline[i][0], polyline[i][1])
                            else:
                                for i in range(1, len(polyline)):
                                    move(polyline[i][0], polyline[i][1], polyline[i][2])
                            # turn off assists if set to 'feed'
                            if 'air_assist' in pass_ and pass_['air_assist'] == 'feed':
                                air_off()
                            # if 'aux_assist' in pass_ and pass_['aux_assist'] == 'feed':
                            #     aux_off()

        # assists off, end of pass if set to 'pass'
        if 'air_assist' in pass_:
            if pass_['air_assist'] == 'pass':
                air_off()
        else:
            air_off()  # also default this behavior
        # if 'aux_assist' in pass_ and pass_['aux_assist'] == 'pass':
        #     aux_off()

    # leave machine in absolute mode
    absolute()

    # return to origin
    feedrate(conf['seekrate'])
    intensity(0.0)
    if 'head' in jobdict and \
       'noreturn' in jobdict['head'] and \
       jobdict['head']['noreturn']:
        pass
    else:
        move(0, 0, 0)


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
    if (not 'head' in jobdict) or \
       (not 'kind' in jobdict['head']) or \
       (jobdict['head']['kind'] != 'mill'):
        print("NOTICE: not a mill job")
        return

    if not 'defs' in jobdict:
        print("ERROR: invalid job")
        return
    # prime job
    air_off()
    aux_off()
    absolute()
    intensity(0.0)
    seekrate = conf['seekrate']
    feedrate_ = conf['feedrate']
    feedrate(seekrate)
    feedrate_active = seekrate
    # run job
    for def_ in jobdict['defs']:
        path = def_['data']
        for item in path:
            if item[0] == 'G0':
                if feedrate_active != seekrate:
                    feedrate(seekrate)
                    feedrate_active = seekrate
                move(item[1][0],item[1][1],item[1][2])
            elif item[0] == 'G1':
                if feedrate_active != feedrate_:
                    feedrate(feedrate_)
                    feedrate_active = feedrate_
                move(item[1][0],item[1][1],item[1][2])
            elif item[0] == 'F':
                feedrate_ = item[1]
            elif item[0] == 'S':
                #convert RPMs to 0-100%
                ipct = item[1]*(100.0/conf['mill_max_rpm'])
                intensity(ipct)
            elif item[0] == 'MIST':
                if item[1] == True:
                    air_on()
                elif item[1] == False:
                    air_off()
            elif item[0] == 'FLOOD':
                if item[1] == True:
                    aux_on()
                elif item[1] == False:
                    aux_off()
    # finalize job
    air_off()
    aux_off()
    absolute()
    feedrate(conf['seekrate'])
    intensity(0.0)
    supermove(z=0)
    supermove(x=0, y=0)


if __name__ == "__main__":
    # run like this to profile: python -m cProfile driveboard.py
    connect()
    if connected():
        testjob()
        time.sleep(0.5)
        while not status()['ready']:
            time.sleep(1)
            sys.stdout.write('.')
        close()
