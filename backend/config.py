# -*- coding: UTF-8 -*-
# Configuration of DriveboardApp
#
# NOTE!
# -----
# To change config parameters create a file named
# config.name_of_config.json in the config directory
# (use --list-configs to find it)
# and write something like this:
#
# {
#     "network_port": 4411,
#     "serial_port": "COM3"
# }
#

import os
import sys
import glob
import json
import copy
import tempfile

from encodings import hex_codec  # explicit for pyinstaller
from encodings import ascii  # explicit for pyinstaller
from encodings import utf_8  # explicit for pyinstaller
from encodings import mac_roman  # explicit for pyinstaller


conf = {
    'appname': 'driveboardapp',
    'version': '20.12',
    'company_name': 'com.nortd.labs',
    'network_host': '',                 # '' for all nics
    'network_port': 4444,
    'serial_port': '',                  # set to '' for auto (req. firmware)
    'baudrate': 57600,
    'rootdir': None,                    # defined further down (../)
    'confdir': None,                    # defined further down
    'stordir': None,                    # defined further down
    'hardware': None,                   # defined further down
    'firmware': None,                   # defined further down
    'tolerance': 0.01,
    'workspace': [1220,610,0],
    'grid_mm': 100,
    'seekrate': 6000,
    'feedrate': 2000,
    'intensity': 0,
    'kerf': 0.3,
    'pxsize': 0.2,                      # size (mm) of beam for rastering
    'pulse_intensity': 10,
    'pulse_duration': 0.1,
    'max_jobs_in_list': 20,
    'usb_reset_hack': False,
    'print_serial_data': False,
    'raster_invert': False,             # Set to True for materials which etch light on dark (eg slate, black marble)
    'raster_levels': 128, 
    'raster_mode': 'Bidirectional',     # 'Forward', 'Reverse', 'Bidirectional'
    'raster_leadin': 10,
    'fill_mode': 'Bidirectional',       # 'Forward', 'Reverse', 'Bidirectional', 'NearestNeighbor'
    'fill_leadin': 10,
    'max_segment_length': 5.0,
    'users': {
        'laser': 'laser',
    },
    'enable_gzip': True,                # allow gzip upload of files / jobs
    'home_on_startup': False,
    'mill_mode': False,
    'mill_max_rpm': 18000,
    'alignment_host': None,
    'alignment_port': 80,
    'require_unit': False,
}
conf_defaults = copy.deepcopy(conf)

userconfigurable = {
    'network_host': "IP (NIC) to run server on. Leave '' for all.",
    'network_port': "Port to run server on.",
    'serial_port': "Serial port for Driveboard hardware.",
    'firmware': "Default firmware. Use designator matching the * in config.*.h",
    'workspace': "[x,y,z] dimensions of machine's work area in mm.",
    'grid_mm': "Visual grid of UI in mm.",
    'seekrate': "Default seek rate in mm/min.",
    'feedrate': "Default feed rate in mm/min.",
    'intensity': "Default intensity setting 0-100.",
    'kerf': "Typical kerf of a cut.",
    'pxsize': "Default kerf setting for rastering and filling.",
    'pulse_intensity': "Default pulse intensity setting 0-100.",
    'pulse_duration': "Default pulse duration in seconds.",
    'max_jobs_in_list': "Jobs to keep in the history list.",
    'print_serial_data': "Print all raw serial communications to the debug window.",
    'raster_invert': "If true, laser will set black pixels to 0%% intensity and white pixels to 100%% intensity.",
    'raster_levels': "Number of raster dithering levels, from 2 for complete on/off dithering, to 128 for a smooth image",
    'raster_mode': "Pathing to use when rastering: 'Forward', 'Reverse', or 'Bidirectional'.",
    'raster_leadin': "Leadin for raster fills in mm. Note: rastering may fail if this is 0",
    'fill_mode': "Pathing to use when engraving a fill area: 'Forward', 'Reverse', 'Bidirectional', or 'NearestNeighbor'.",
    'fill_leadin': "Leadin for vector fills in mm.",
    'users': "List of user cendentials for UI access.",
    'enable_gzip': "Enable gzip compression in job uploads.",
    'home_on_startup': "Automatically perform a homing cycle when the machine first connects.",
    'mill_mode': "Activate CNC mill mode.",
    'mill_max_rpm': "Maximum spindle RPM.",
    'alignment_host': "IP or hostname of alignment server.",
    'alignment_port': "Port of alignment server.",
    'require_unit': "Whether a physical unit (cm, mm, in) should be required to load SVG files.",
}


### make some 'smart' default setting choices


### rootdir
# This is to be used with all relative file access.
# _MEIPASS is a special location for data files when creating
# standalone, single file python apps with pyInstaller.
# Standalone is created by calling from 'other' directory:
# python pyinstaller/pyinstaller.py --onefile app.spec
if hasattr(sys, "_MEIPASS"):
    conf['rootdir'] = sys._MEIPASS
else:
    # root is one up from this file
    conf['rootdir'] = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '../'))
#
###


### stordir
# This is to be used to store queue files and similar
conf['stordir'] = tempfile.mkdtemp('driveboardapp')
#
###

### confdir
# This is to be used to store the configuration file
if sys.platform == 'darwin':
    directory = os.path.join(os.path.expanduser('~'),
                             'Library', 'Application Support',
                             conf['company_name'], conf['appname'])
elif sys.platform == 'win32':
    directory = os.path.join(os.path.expandvars('%APPDATA%'),
                             conf['company_name'], conf['appname'])
else:
    directory = os.path.join(os.path.expanduser('~'), "." + conf['appname'])
if not os.path.exists(directory):
    os.makedirs(directory)
conf['confdir'] = directory
#
###


### auto-check hardware
#
conf['hardware'] = 'standard'
if sys.platform == "linux2":
    try:
        import RPi.GPIO
        conf['hardware'] = 'raspberrypi'
    except ImportError:
        # os.uname() on BBB:
        # ('Linux', 'lasersaur', '3.8.13-bone20',
        #  '#1 SMP Wed May 29 06:14:59 UTC 2013', 'armv7l')
        if os.uname()[4].startswith('arm'):
            conf['hardware'] = 'beaglebone'
#
###


if conf['hardware'] == 'standard':
    if not conf['firmware']:
        conf['firmware'] = 'driveboardusb'
elif conf['hardware'] == 'beaglebone':
    if not conf['firmware']:
        conf['firmware'] = 'driveboard1403'
    conf['serial_port'] = '/dev/ttyO1'
    # if running as root
    if os.geteuid() == 0:
        conf['network_port'] = 80

    # Beaglebone white specific
    if os.path.exists("/sys/kernel/debug/omap_mux/uart1_txd"):
        # we are not on the beaglebone black, setup uart1
        # echo 0 > /sys/kernel/debug/omap_mux/uart1_txd
        with open("/sys/kernel/debug/omap_mux/uart1_txd", "w") as fw:
            fw.write("%X" % (0))
        # echo 20 > /sys/kernel/debug/omap_mux/uart1_rxd
        with open("/sys/kernel/debug/omap_mux/uart1_rxd", "w") as fw:
            fw.write("%X" % ((1 << 5) | 0))

    ### if running on BBB/Ubuntu 14.04, setup pin muxing UART1
    pin24list = glob.glob("/sys/devices/ocp.*/P9_24_pinmux.*/state")
    for pin24 in pin24list:
        os.system("echo uart > %s" % (pin24))

    pin26list = glob.glob("/sys/devices/ocp.*/P9_26_pinmux.*/state")
    for pin26 in pin26list:
        os.system("echo uart > %s" % (pin26))


    ### Set up atmega328 reset control
    # The reset pin is connected to GPIO2_7 (2*32+7 = 71).
    # Setting it to low triggers a reset.
    # echo 71 > /sys/class/gpio/export

    ### if running on BBB/Ubuntu 14.04, setup pin muxing GPIO2_7 (pin 46)
    pin46list = glob.glob("/sys/devices/ocp.*/P8_46_pinmux.*/state")
    for pin46 in pin46list:
        os.system("echo gpio > %s" % (pin46))

    try:
        with open("/sys/class/gpio/export", "w") as fw:
            fw.write("%d" % (71))
    except IOError:
        # probably already exported
        pass
    # set the gpio pin to output
    # echo out > /sys/class/gpio/gpio71/direction
    with open("/sys/class/gpio/gpio71/direction", "w") as fw:
        fw.write("out")
    # set the gpio pin high
    # echo 1 > /sys/class/gpio/gpio71/value
    with open("/sys/class/gpio/gpio71/value", "w") as fw:
        fw.write("1")
        fw.flush()

    ### Set up atmega328 reset control - BeagleBone Black
    # The reset pin is connected to GPIO2_9 (2*32+9 = 73).
    # Setting it to low triggers a reset.
    # echo 73 > /sys/class/gpio/export

    ### if running on BBB/Ubuntu 14.04, setup pin muxing GPIO2_9 (pin 44)
    pin44list = glob.glob("/sys/devices/ocp.*/P8_44_pinmux.*/state")
    for pin44 in pin44list:
        os.system("echo gpio > %s" % (pin44))

    try:
        with open("/sys/class/gpio/export", "w") as fw:
            fw.write("%d" % (73))
    except IOError:
        # probably already exported
        pass
    # set the gpio pin to output
    # echo out > /sys/class/gpio/gpio73/direction
    with open("/sys/class/gpio/gpio73/direction", "w") as fw:
        fw.write("out")
    # set the gpio pin high
    # echo 1 > /sys/class/gpio/gpio73/value
    with open("/sys/class/gpio/gpio73/value", "w") as fw:
        fw.write("1")
        fw.flush()

    ### read stepper driver configure pin GPIO2_12 (2*32+12 = 76).
    # Low means Geckos, high means SMC11s

    ### if running on BBB/Ubuntu 14.04, setup pin muxing GPIO2_12 (pin 39)
    pin39list = glob.glob("/sys/devices/ocp.*/P8_39_pinmux.*/state")
    for pin39 in pin39list:
        os.system("echo gpio > %s" % (pin39))

    try:
        with open("/sys/class/gpio/export", "w") as fw:
            fw.write("%d" % (76))
    except IOError:
        # probably already exported
        pass
    # set the gpio pin to input
    with open("/sys/class/gpio/gpio76/direction", "w") as fw:
        fw.write("in")
    # set the gpio pin high
    with open("/sys/class/gpio/gpio76/value", "r") as fw:
        ret = fw.read()
        # print "Stepper driver configure pin is: " + str(ret)

elif conf['hardware'] == 'raspberrypi':
    if not conf['firmware']:
        conf['firmware'] = 'driveboard1403'
    conf['serial_port'] = '/dev/ttyAMA0'
    # if running as root
    if os.geteuid() == 0:
        conf['network_port'] = 80
    import RPi.GPIO as GPIO
    # GPIO.setwarnings(False) # surpress warnings
    GPIO.setmode(GPIO.BCM)  # use chip pin number
    pinSense = 7
    pinReset = 2
    pinExt1 = 3
    pinExt2 = 4
    pinExt3 = 17
    pinTX = 14
    pinRX = 15
    # read sens pin
    GPIO.setup(pinSense, GPIO.IN)
    isSMC11 = GPIO.input(pinSense)
    # atmega reset pin
    GPIO.setup(pinReset, GPIO.OUT)
    GPIO.output(pinReset, GPIO.HIGH)
    # no need to setup the serial pins
    # although /boot/cmdline.txt and /etc/inittab needs
    # to be edited to deactivate the serial terminal login
    # (basically anything related to ttyAMA0)


configpath = ''
def load(configname):
    if configname:
        path = os.path.join(conf['confdir'], 'config.'+configname+'.json')
    else:
        path = os.path.join(conf['confdir'], 'config.json')
    global configpath
    configpath = path
    #load
    if os.path.exists(path):
        # print "CONFIG: " + path
        # apply user config
        with open(path) as fp:
            try:
                userconf = json.load(fp)
                for k in list(userconfigurable.keys()):
                    if k in userconf:
                        conf[k] = userconf[k]
            except ValueError:
                print("ERROR: failed to read config file")
    else:
        if not configname:
            # special case: default config not present, create
            print("INFO: creating default config file")
            with open(path, "w") as fp:
                confout = {k:v for k,v in list(conf.items()) if k in userconfigurable}
                json.dump(confout, fp, indent=4)
        else:
            print("ERROR: invalid config specified")
            sys.exit()


def write_config_fields(subconfigdict):
    conftemp = None
    if os.path.exists(configpath):
        with open(configpath) as fp:
            conftemp = json.load(fp)
    else:
        conftemp = {}
    conftemp.update(subconfigdict)
    with open(configpath, "w") as fp:
        json.dump(conftemp, fp, indent=4)


def list_configs():
    print("Config files in " + conf['confdir'] + ":")
    tempdir = os.getcwd()
    os.chdir(conf['confdir'])
    cfiles = glob.glob('config.*.json')
    for cfile in cfiles:
        confname = cfile.split('.')[1]
        print("%s - (%s)" % (confname, cfile))
    os.chdir(tempdir)
