#!/usr/bin/env python3
"""
Raspberry Pi HQ Camera Application - FIXED VERSION
Ottimizzato per display 3.5" 480x320 con touch
"""

import os
import sys
import time

_T0 = time.monotonic()

# Under systemd stdout is a pipe, so Python block-buffered it and none of the
# startup timings below reached the journal until ~8 KB had piled up.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# --- Environment must be set before SDL / gpiozero are imported ---------------
# SDL_AUDIODRIVER: pygame.init() would otherwise probe ALSA, and on a headless
# Pi with no sound card that probe blocks for a long time before failing.
# GPIOZERO_PIN_FACTORY: without it gpiozero tries every backend in turn
# (lgpio -> rpigpio -> pigpio -> native), each attempt costing an import.
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('SDL_NOMOUSE', '1')
os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'lgpio')

# pygame imports every optional module eagerly, and some pull large shared
# library trees off the SD card: mixer -> SDL_mixer and its codecs, imageext ->
# SDL_image with png/jpeg/tiff/webp/avif. None of them is used here (no audio;
# icons and photos are decoded by PIL). pygame treats an ImportError for these
# as "module unavailable", and a None entry in sys.modules makes that import
# fail at once instead of loading them: ~1 s less on a cold boot.
for _name in ("pygame.mixer", "pygame.mixer_music", "pygame.sndarray",
              "pygame.scrap", "pygame.fastevent", "pygame.imageext"):
    sys.modules.setdefault(_name, None)

import math
import threading
from datetime import datetime
from pathlib import Path
import glob
import queue
import signal
import socket
import json
import subprocess
import struct
import fcntl
import mmap

# --- Overlap the two expensive import trees ----------------------------------
# picamera2 (which drags in libcamera + numpy) and pygame/SDL each cost a
# second or more on a Pi Zero 2W and neither depends on the other.  Importing
# picamera2 on a worker thread lets it proceed while the main thread imports
# pygame, so the boot pays roughly the slower of the two instead of the sum.
# Picamera2 itself is imported by the camera thread (see _init_camera), so
# the main thread can put the UI on screen while libcamera is still loading.
def _preload_camera_stack():
    try:
        import picamera2  # noqa: F401
    except Exception:
        pass  # real error is surfaced by the import in _init_camera


_preload_thread = threading.Thread(target=_preload_camera_stack, daemon=True)
_preload_thread.start()

import pygame
from pygame import gfxdraw  # anti-aliased circles for the menus
# Already loaded by pygame.surfarray, so this costs nothing extra.
import numpy as np

# PIL is only needed for gallery thumbnails and the remote-preview JPEG, i.e.
# never for the first frame.  Importing it on demand keeps it off the boot path;
# the background initializer warms it so the first real use is already cached.
_PIL_IMAGE = None


def _pil_image():
    """Return PIL.Image, importing it on first use."""
    global _PIL_IMAGE
    if _PIL_IMAGE is None:
        from PIL import Image
        _PIL_IMAGE = Image
    return _PIL_IMAGE


# Battery Management
class BatteryManager:
    def __init__(self):
        self.percentage = 0
        self.available = False
        self.last_update = 0
        # Imported here: the manager is built by the background initializer,
        # so smbus stays off the path to the first frame.
        try:
            import smbus
        except ImportError:
            print("Warning: python3-smbus not installed, battery indicator will not work")
            smbus = None
        if smbus is not None:
            try:
                self.bus = smbus.SMBus(1)
                # Test communication
                self.bus.read_byte(0x62)
                
                # CW2015 Quick Start (Wake Up)
                # Write 0x30 to register 0x0A (MODE_CONFIG) to restart fuel gauge
                try:
                    self.bus.write_byte_data(0x62, 0x0A, 0x30)
                    time.sleep(0.2) # Wait for wake up
                    print("✓ CW2015 Quick Start command sent")
                except Exception as e:
                    print(f"Warning: Could not send Quick Start: {e}")
                
                self.available = True
                self.percentage = 0
                self.voltage = 0
                self.update()
                print("✓ UPS-Lite Battery Manager initialized")
            except Exception as e:
                print(f"UPS-Lite not found or I2C error: {e}")
    def update(self):
        """Read battery status from CW2015 chip (UPS Lite V1.3)"""
        if not self.available:
            return
        
        try:
            # CW2015 Fuel Gauge Chip
            # I2C Address: 0x62
            # SOC Register: 0x04-0x05 (16-bit)
            # Voltage Register: 0x02-0x03 (16-bit)
            
            # Read SOC (State of Charge) - Register 0x04-0x05
            # Format: Upper 8 bits = integer percentage (0-100%)
            #         Lower 8 bits = fractional (1/256 %)
            soc_high = self.bus.read_byte_data(0x62, 0x04)  # Integer part
            soc_low = self.bus.read_byte_data(0x62, 0x05)   # Fractional part
            
            # Calculate percentage: integer + fractional/256
            self.percentage = soc_high + (soc_low / 256.0)
            
            # Read Voltage - Register 0x02-0x03
            # CW2015 voltage format: 14-bit value, LSB = 305uV
            vcell_high = self.bus.read_byte_data(0x62, 0x02)
            vcell_low = self.bus.read_byte_data(0x62, 0x03)
            vcell_raw = (vcell_high << 8) | vcell_low
            
            # Voltage calculation for CW2015: raw * 0.305mV / 1000 = Volts
            self.voltage = (vcell_raw * 0.305) / 1000.0
            
            # Clamp percentage 0-100
            if self.percentage > 100: 
                self.percentage = 100
            elif self.percentage < 0: 
                self.percentage = 0
            
            if DEBUG_MODE:
                print(f"🔋 CW2015 BATTERY:")
                print(f"   SOC: {soc_high}% + {soc_low}/256 = {self.percentage:.1f}%")
                print(f"   VCELL: 0x{vcell_raw:04x} = {self.voltage:.3f}V")

        except Exception as e:
            print(f"Battery update error: {e}")

# evdev (raw touch input) is imported by the background initializer; it is
# not needed to draw the first frame.
evdev = None

# Configuration
DEBUG_MODE = False  # Set to True only for diagnostics
PHOTOS_DIR = Path.home() / "photos"
GPIO_BUTTON_PIN = 26
BUTTON_DEBOUNCE = 0.3
UDP_PORT = 12345
# How long to retry the camera open before giving up, counted from when
# picamera2 has finished importing. The service starts before udev has
# necessarily loaded the camera drivers, so /dev/media* may not exist yet; on
# hardware that's actually connected, Picamera2() succeeds within a few tries.
CAMERA_INIT_TIMEOUT = 10.0
# Touch panel, GPIO chip and I2C bus can likewise still be appearing (or be
# waiting for udev to hand them to the input/gpio/i2c groups) when the
# background initializer first looks; it keeps retrying for this long.
DEVICE_WAIT = 20.0
# A preview frame normally arrives every 33 ms (250 ms at the slowest manual
# shutter). Waiting longer than this means the sensor has stopped streaming.
FRAME_TIMEOUT = 2.0
# While stalled, how often to restart the camera, and how long to wait for a
# frame after each restart (the UI is blocked for that long).
CAMERA_RESTART_INTERVAL = 10.0
CAMERA_RESTART_WAIT = 1.0
# Full-resolution still capture, including the mode switch.
STILL_TIMEOUT = 10.0
SHARED_MEM_PREVIEW = "/tmp/camera_preview.jpg"
SHARED_MEM_STATUS = "/tmp/camera_status.json"
# The web remote (photo_server.py) checks in every few seconds; preview JPEGs
# are encoded only while it does, at up to REMOTE_FPS.
REMOTE_TIMEOUT = 10.0
REMOTE_FPS = 8
# camera_status.json carries the settings the web pages show.
STATUS_INTERVAL = 0.5
SERVER_PORT = 8080
# Hotspot profile created by setup.sh (Step 7). SSID and password are read
# back from NetworkManager when possible; these are setup.sh's defaults.
HOTSPOT_CONNECTION = "RaspiCam_Hotspot"
HOTSPOT_SSID = "RaspiCam"
HOTSPOT_PASSWORD = "raspicam_admin"
HOTSPOT_IP = "10.42.0.1"
# Standby: the CPU governor used while asleep, and the green LED blinking
# slowly so the camera does not look switched off.
CPUFREQ_GOVERNOR = "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
ACT_LED = "/sys/class/leds/ACT"
# What the app records while asleep, so that a run that dies in standby (killed,
# crashed) can be undone by the next one. A file in /tmp: it survives an app
# restart, not a reboot.
STANDBY_STATE = "/tmp/camera_standby.json"
# Without a camera the interface stays up and says so. Every CAMERA_RECHECK
# seconds the app looks for one; libcamera only sees a sensor that exists when
# its first Picamera2() is created, so when one shows up the app starts over
# (at most MAX_RELAUNCH times per boot: a camera that is registered but will
# not start must not make a loop; the count is cleared once a camera starts).
CAMERA_RECHECK = 3.0
RELAUNCH_FILE = "/tmp/camera_relaunch"
MAX_RELAUNCH = 2

# Display output (see FramebufferOutput). CAMERA_DISPLAY=sdl forces the old
# SDL/KMSDRM path.
FBDEV_PATH = os.environ.get("CAMERA_FBDEV", "/dev/fb0")
# The fb node exists once vc4 has probed, but is root-only until udev hands it
# to the video group. Waiting is still cheaper than the SDL fallback.
FBDEV_WAIT = 10.0
FBIOGET_VSCREENINFO = 0x4600
KDSETMODE = 0x4B3A
KD_TEXT, KD_GRAPHICS = 0, 1

# Colors (legacy names kept: they are passed around as button tints)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (128, 128, 128)
LIGHT_GRAY = (200, 200, 200)
DARK_GRAY = (50, 50, 50)
GREEN = (0, 200, 0)
BLUE = (0, 120, 200)
RED = (200, 0, 0)
YELLOW = (255, 255, 0)

# --- Modern UI palette ------------------------------------------------------
# Neutral surfaces carry a slight blue cast so they read as "deliberately dark"
# next to the camera preview instead of as dead pixels. One accent (amber, the
# traditional exposure-warning hue) marks every active/toggled state.
SURFACE_0  = (11, 12, 15)     # sidebars, deepest chrome
SURFACE_1  = (23, 25, 31)     # popup body
SURFACE_2  = (34, 37, 45)     # raised: keycaps, list rows, control cards
SURFACE_3  = (49, 53, 63)     # pressed / emphasised
HAIRLINE   = (60, 65, 77)     # 1px separators and outlines
TEXT_HI    = (245, 246, 248)  # primary values
TEXT_MID   = (154, 161, 174)  # labels
TEXT_LOW   = (104, 111, 125)  # inactive
ACCENT     = (255, 176, 32)   # active toggles, live AE readback
SUCCESS    = (48, 199, 123)
DANGER     = (238, 68, 68)
INFO       = (56, 132, 232)

# pygame's built-in font is a low-resolution bitmap face. FreeSans ships with
# Raspberry Pi OS and is the only real sans available; sizes are scaled by
# FONT_SCALE because a TrueType face at size N renders considerably larger than
# Font(None, N) — measured on-device, 0.78 keeps string widths within ~3% of the
# old font so nothing overflows the existing (unchanged) button rects.
FONT_REGULAR = "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"
FONT_SCALE = 0.78


def load_font(size, bold=False):
    """Return a UI font, falling back to pygame's default face if FreeSans is absent."""
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return pygame.font.Font(path, max(10, int(size * FONT_SCALE)))
    except Exception:
        return pygame.font.Font(None, size)


def ui_font(px, bold=False):
    """FreeSans at a pixel size, for the menus (load_font() sizes are in the old font's units)."""
    try:
        return pygame.font.Font(FONT_BOLD if bold else FONT_REGULAR, px)
    except Exception:
        return pygame.font.Font(None, int(px / FONT_SCALE))

# ISO and Shutter Speed options
ISO_VALUES = ["Auto", 100, 200, 400, 800, 1600, 3200, 6400]
SHUTTER_SPEEDS = [
    ("Auto", 0),
    ("1/2000", 500),
    ("1/1000", 1000),
    ("1/800", 1250),
    ("1/500", 2000),
    ("1/250", 4000),
    ("1/125", 8000),
    ("1/60", 16666),
    ("1/30", 33333),
    ("1/15", 66667),
    ("1/10", 100000),
    ("1/8", 125000),
    ("1/4", 250000)
]

# --- Focus assist ---------------------------------------------------------------
# IMX477 pixel array: ScalerCrop rectangles and still captures use these units.
SENSOR_SIZE = (4056, 3040)
# Magnifier steps. The preview comes from the 2x2-binned 2028x1520 mode, so at
# 3x one preview pixel is about one binned sensor pixel; 6x enlarges further.
ZOOM_LEVELS = (1, 3, 6)
PEAK_COLORS = (("Rosso", (255, 48, 48)), ("Giallo", (255, 214, 10)),
               ("Ciano", (40, 210, 255)), ("Bianco", (255, 255, 255)))
# Sensitivity: base threshold on the peaking response (see FocusPeaker). It is
# raised automatically to PEAK_NOISE_FACTOR times the noise floor at high ISO.
PEAK_LEVELS = (("Bassa", 120), ("Media", 84), ("Alta", 56))
PEAK_NOISE_FACTOR = 4.0
# The peaking filter runs on its own core at up to this rate; the viewfinder
# paints the latest result on every frame.
PEAK_INTERVAL = 1 / 15
# Remote commands that need a camera (ignored while there is none)
CAMERA_COMMANDS = frozenset(("CAPTURE", "ISO_UP", "ISO_DOWN", "ISO", "SHUTTER_UP", "SHUTTER_DOWN",
                             "SHUTTER", "BW", "PEAK", "ZOOM", "ZOOM_AT"))


class FramebufferOutput:
    """Present frames by copying them straight into the Linux framebuffer.

    SDL's KMSDRM backend always goes through EGL, i.e. Mesa, whose single
    driver library links LLVM: on a Pi Zero 2W that is ~70 MB read off the SD
    card and ~9 s before the first set_mode() returns on a cold boot. The UI is
    plain 2D software rendering anyway, so it is drawn on a surface from SDL's
    "dummy" driver and copied into /dev/fb0 (the KMS driver's fbdev emulation),
    which costs ~0.1 s to set up and 2-3 ms per frame.
    """

    def __init__(self, fd, mm, size, bpp, masks, stride, offset):
        self.fd = fd
        self.mm = mm
        self.size = size
        self.bpp = bpp
        self.masks = masks
        self.stride = stride
        self.offset = offset
        self._convert = None
        self._console = False

    @classmethod
    def open(cls, path, wait):
        """Map the framebuffer, or return None if it is unusable (caller falls back to SDL)."""
        name_file = f"/sys/class/graphics/{os.path.basename(path)}/name"
        deadline = time.monotonic() + wait
        while True:
            try:
                # Early in the boot fb0 is still the firmware's "simple"
                # framebuffer; the KMS driver replaces it when it loads, and
                # anything mapped before that would no longer be on screen.
                with open(name_file) as f:
                    if f.read().strip() == "simple" and time.monotonic() < deadline:
                        raise OSError("firmware framebuffer, KMS driver not loaded yet")
                fd = os.open(path, os.O_RDWR)
                break
            except OSError as e:
                if time.monotonic() >= deadline:
                    print(f"Framebuffer {path} unavailable ({e}), using SDL")
                    return None
                time.sleep(0.1)
        try:
            # struct fb_var_screeninfo is 40 __u32 fields.
            var = struct.unpack("40I", fcntl.ioctl(fd, FBIOGET_VSCREENINFO, bytes(160)))
            xres, yres, _, yres_virtual, xoffset, yoffset, bpp = var[:7]
            # red / green / blue bitfields: (offset, length) at 8, 11, 14
            masks = tuple(((1 << var[i + 1]) - 1) << var[i] for i in (8, 11, 14))
            with open(f"/sys/class/graphics/{os.path.basename(path)}/stride") as f:
                stride = int(f.read())
            if bpp not in (16, 32) or not xres or not yres:
                raise ValueError(f"unsupported mode {xres}x{yres} {bpp}bpp")
            mm = mmap.mmap(fd, stride * yres_virtual, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception as e:
            os.close(fd)
            print(f"Framebuffer {path} not usable ({e}), using SDL")
            return None
        offset = yoffset * stride + xoffset * bpp // 8
        return cls(fd, mm, (xres, yres), bpp, masks, stride, offset)

    def describe(self):
        return f"{self.size[0]}x{self.size[1]} {self.bpp}bpp"

    def bind(self, screen):
        """Prepare to present `screen`, converting only if its format differs."""
        same = (screen.get_bytesize() * 8 == self.bpp
                and tuple(screen.get_masks()[:3]) == self.masks)
        if not same:
            # e.g. a 16 bpp RGB565 console: SDL converts on blit (~3 ms/frame).
            self._convert = pygame.Surface(self.size, 0, self.bpp, self.masks + (0,))
        self._claim_console()

    def _claim_console(self):
        """Stop the text console drawing its cursor or kernel messages over the UI.

        Under systemd stdin is the VT (TTYPath=/dev/tty1); TTYReset puts it
        back into text mode if the app dies without cleanup().
        """
        try:
            fcntl.ioctl(0, KDSETMODE, KD_GRAPHICS)
            self._console = True
        except OSError:
            pass  # not on a VT (e.g. started over SSH): nothing to hide

    def show(self, screen):
        src = screen
        if self._convert is not None:
            self._convert.blit(screen, (0, 0))
            src = self._convert
        pitch = src.get_pitch()
        height = self.size[1]
        # get_buffer() locks the surface until the proxy is released, which
        # happens when this function returns.
        with memoryview(src.get_buffer()) as pixels:
            if pitch == self.stride:
                n = pitch * height
                self.mm[self.offset:self.offset + n] = pixels[:n]
            else:
                row = self.size[0] * self.bpp // 8
                for y in range(height):
                    dst = self.offset + y * self.stride
                    self.mm[dst:dst + row] = pixels[y * pitch:y * pitch + row]

    def close(self):
        if self._console:
            self._console = False
            try:
                fcntl.ioctl(0, KDSETMODE, KD_TEXT)
            except OSError:
                pass
        if self.mm is not None:
            self.mm.close()
            os.close(self.fd)
            self.mm = None


# Media controller devices (by /sys/bus/media/devices/*/model) that libcamera's
# Raspberry Pi pipeline handlers need: Pi Zero..4, then Pi 5.
CAMERA_PIPELINES = (("unicam", "bcm2835-isp"), ("rp1-cfe", "pispbe"))


def _camera_devices_ready():
    """True once a camera pipeline, sensor included, is registered and usable.

    libcamera enumerates cameras once, when the first Picamera2() creates its
    camera manager, and does not pick up a sensor that binds later: the media
    device is registered at driver probe, the sensor joins it afterwards, and
    no new device event follows. Early in the boot that left the app with no
    camera at all until systemd restarted it. Sensor subdevs are named
    "<sensor> <i2c bus>-<address>", e.g. "imx477 10-001a".
    """
    def usable(node):
        return os.access(node, os.R_OK | os.W_OK)  # udev has chgrp'd it to video

    models = {}
    try:
        for media in os.listdir("/sys/bus/media/devices"):
            with open(f"/sys/bus/media/devices/{media}/model") as f:
                models.setdefault(f.read().strip(), []).append(f"/dev/{media}")
        sensors = []
        for dev in os.listdir("/sys/class/video4linux"):
            if dev.startswith("v4l-subdev"):
                with open(f"/sys/class/video4linux/{dev}/name") as f:
                    name = f.read().strip()
                bus_addr = name.rpartition(" ")[2]
                if "-" in bus_addr and bus_addr.split("-")[0].isdigit():
                    sensors.append(f"/dev/{dev}")
    except OSError:
        return False
    for pipeline in CAMERA_PIPELINES:
        if all(model in models for model in pipeline):
            nodes = [node for model in pipeline for node in models[model]]
            return bool(sensors) and all(usable(n) for n in nodes + sensors)
    return False


def _uptime():
    """Seconds since the kernel started (to relate app timings to the boot)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return float("nan")




# --- QR codes -------------------------------------------------------------------
# The system menu shows QR codes for the remote page and, with the hotspot on,
# for joining its network. Minimal encoder: byte mode, error correction level
# M, versions 1-10 (up to 213 bytes), standard mask selection.

_QR_BLOCKS_M = {  # version: (EC codewords per block, ((blocks, data codewords), ...))
    1: (10, ((1, 16),)), 2: (16, ((1, 28),)), 3: (26, ((1, 44),)),
    4: (18, ((2, 32),)), 5: (24, ((2, 43),)), 6: (16, ((4, 27),)),
    7: (18, ((4, 31),)), 8: (22, ((2, 38), (2, 39))),
    9: (22, ((3, 36), (2, 37))), 10: (26, ((4, 43), (1, 44))),
}
_QR_ALIGN = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
             7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50)}
_QR_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_gf = 1
for _i in range(255):
    _GF_EXP[_i] = _gf
    _GF_LOG[_gf] = _i
    _gf <<= 1
    if _gf & 0x100:
        _gf ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_remainder(data, n):
    """Reed-Solomon error correction codewords for one block."""
    gen = [1]
    for i in range(n):
        gen = [a ^ _gf_mul(b, _GF_EXP[i]) for a, b in zip(gen + [0], [0] + gen)]
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(1, n + 1):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


def _qr_penalty(m):
    """ISO 18004 mask penalty (rules N1-N4)."""
    size = len(m)
    cols = [list(c) for c in zip(*m)]
    score = 0
    finder = [True, False, True, True, True, False, True, False, False, False, False]
    finder_rev = finder[::-1]
    for line in m + cols:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, prev = 1, v
        if run >= 5:
            score += run - 2
        for i in range(size - 10):
            seg = line[i:i + 11]
            if seg == finder or seg == finder_rev:
                score += 40
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def qr_matrix(text):
    """QR code for `text` as rows of booleans (True = dark), without quiet zone."""
    data = text.encode("utf-8")
    for version in range(1, 11):
        ec_len, groups = _QR_BLOCKS_M[version]
        capacity = sum(n * k for n, k in groups)
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= 8 * capacity:
            break
    else:
        raise ValueError("text too long for a QR code")

    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)
    put(0, min(4, 8 * capacity - len(bits)))
    put(0, -len(bits) % 8)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    codewords += [(0xEC, 0x11)[i % 2] for i in range(capacity - len(codewords))]

    blocks, pos = [], 0
    for count, length in groups:
        for _ in range(count):
            blocks.append(codewords[pos:pos + length])
            pos += length
    ecc = [_rs_remainder(b, ec_len) for b in blocks]
    stream = []
    for i in range(max(len(b) for b in blocks)):
        stream += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        stream += [e[i] for e in ecc]

    size = 17 + 4 * version
    dark = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def set_fixed(r, c, value):
        dark[r][c] = bool(value)
        fixed[r][c] = True

    # Finder patterns and their separators
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    ring = max(abs(dr - 3), abs(dc - 3))
                    set_fixed(r, c, ring not in (2, 4))
    # Alignment patterns, except where they would overlap a finder
    align = _QR_ALIGN[version]
    for r in align:
        for c in align:
            if (r, c) in ((6, 6), (6, align[-1]), (align[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_fixed(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    # Timing patterns
    for i in range(size):
        if not fixed[6][i]:
            set_fixed(6, i, i % 2 == 0)
        if not fixed[i][6]:
            set_fixed(i, 6, i % 2 == 0)
    # Format information (written after masking) and the dark module
    for i in range(9):
        fixed[8][i] = fixed[i][8] = True
    for i in range(8):
        fixed[8][size - 1 - i] = fixed[size - 1 - i][8] = True
    set_fixed(size - 8, 8, True)
    # Version information
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = version << 12 | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            set_fixed(b, a, (vbits >> i) & 1)
            set_fixed(a, b, (vbits >> i) & 1)

    # Data, in two-module columns zigzagging up and down from the bottom right
    data_bits = [(cw >> i) & 1 for cw in stream for i in range(7, -1, -1)]
    k = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        for i in range(size):
            r = size - 1 - i if upward else i
            for c in (col, col - 1):
                if not fixed[r][c]:
                    dark[r][c] = k < len(data_bits) and data_bits[k] == 1
                    k += 1
        upward = not upward
        col -= 2

    def masked(mask_id):
        m = [row[:] for row in dark]
        fn = _QR_MASKS[mask_id]
        for r in range(size):
            for c in range(size):
                if not fixed[r][c] and fn(r, c):
                    m[r][c] = not m[r][c]
        # Format bits: level M (00) and the mask, BCH(15,5), XOR 0x5412
        rem = mask_id
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        fbits = (mask_id << 10 | rem) ^ 0x5412

        def bit(i):
            return (fbits >> i) & 1 == 1

        for i in range(6):
            m[i][8] = bit(i)
        m[7][8] = bit(6)
        m[8][8] = bit(7)
        m[8][7] = bit(8)
        for i in range(9, 15):
            m[8][14 - i] = bit(i)
        for i in range(8):
            m[8][size - 1 - i] = bit(i)
        for i in range(8, 15):
            m[size - 15 + i][8] = bit(i)
        m[size - 8][8] = True
        return m

    candidates = [masked(i) for i in range(8)]
    return min(candidates, key=_qr_penalty)


# --- Focus peaking -----------------------------------------------------------------
class FocusPeaker:
    """Focus peaking and focus meter, computed off the main thread.

    The viewfinder hands over the green channel of a preview frame and paints
    the latest result into the next frames, so peaking costs the main loop
    ~2 ms instead of the ~70 ms the old in-line filter took on a Pi Zero 2W.
    numpy releases the GIL in its loops, so the filter runs on another core.

    Filter: second derivative along x and along y, each taken on an image
    smoothed with [1 2 1] across the derivative. Unlike a gradient it barely
    responds to soft, high-contrast outlines, so out-of-focus edges stay dark,
    and the smoothing keeps sensor noise from lighting up at high ISO. The
    threshold is the sensitivity level or PEAK_NOISE_FACTOR times the median
    response (the noise floor), whichever is higher: the old fixed 97th
    percentile lit 3% of the frame even with nothing in focus.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._job = None
        self._running = True
        self.busy = False
        self.generation = 0
        self.result = None   # (generation, frame shape, flat pixel indices)
        self.score = None    # focus meter for the last frame processed
        self._noise = None
        self._buffers = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, frame, threshold):
        """Queue a frame (BGR or RGB, HxWx3); replaces any frame still waiting."""
        green = np.ascontiguousarray(frame[:, :, 1])
        with self._cond:
            self._job = (green, threshold, self.generation)
            self.busy = True
            self._cond.notify()

    def reset(self):
        """Drop results: the view changed (zoom, peaking toggled)."""
        with self._cond:
            self.generation += 1
            self._job = None
        self.result = None
        self.score = None
        self._noise = None

    def stop(self):
        with self._cond:
            self._running = False
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while self._running and self._job is None:
                    self.busy = False
                    self._cond.wait()
                if not self._running:
                    return
                green, threshold, generation = self._job
                self._job = None
            try:
                indices, score = self._process(green, threshold)
            except Exception as e:
                print(f"Focus peaking error: {e}")
                continue
            if generation == self.generation:
                self.result = (generation, green.shape, indices)
                self.score = score

    def _process(self, g, threshold):
        h, w = g.shape
        if self._buffers is None or self._buffers[0].shape != (h, w):
            # Reused from frame to frame: fresh 600 KB arrays on every call
            # made the filter ~40% slower on a Pi Zero 2W.
            self._buffers = (np.empty((h, w), np.int16), np.empty((h - 2, w), np.int16),
                             np.empty((h, w - 2), np.int16), np.empty((h - 2, w - 2), np.int16),
                             np.empty((h - 2, w - 2), np.int16), np.empty((h - 2, w - 2), bool),
                             np.empty((h - 2, w - 2), bool))
        gi, sv, sh, resp, dyy, mask, thick = self._buffers
        gi[...] = g
        # d2/dx2 of the image smoothed vertically with [1 2 1]
        np.add(gi[:-2], gi[2:], out=sv)
        sv += gi[1:-1]
        sv += gi[1:-1]
        np.add(sv[:, 1:-1], sv[:, 1:-1], out=resp)
        resp -= sv[:, :-2]
        resp -= sv[:, 2:]
        np.abs(resp, out=resp)
        # d2/dy2 of the image smoothed horizontally
        np.add(gi[:, :-2], gi[:, 2:], out=sh)
        sh += gi[:, 1:-1]
        sh += gi[:, 1:-1]
        np.add(sh[1:-1], sh[1:-1], out=dyy)
        dyy -= sh[:-2]
        dyy -= sh[2:]
        np.abs(dyy, out=dyy)
        resp += dyy                      # (h-2, w-2), at most 8160: fits int16

        noise = float(np.median(resp[::4, ::4]))
        self._noise = noise if self._noise is None else 0.7 * self._noise + 0.3 * noise
        np.greater(resp, max(threshold, PEAK_NOISE_FACTOR * self._noise), out=mask)
        # One pixel thicker, so lines survive the nearest-neighbour downscale
        thick[...] = mask
        thick[:, 1:] |= mask[:, :-1]
        thick[1:] |= mask[:-1]
        rows, cols = np.divmod(np.flatnonzero(thick), w - 2)
        indices = (rows + 1) * w + (cols + 1)

        # Focus meter: mean response above twice the noise floor, centre of
        # the view (the magnifier recentres it on the tapped point).
        rh, rw = resp.shape
        roi = resp[rh // 4: rh - rh // 4, rw // 4: rw - rw // 4]
        floor = 2.0 * self._noise
        score = float(np.mean(np.maximum(roi, floor))) - floor
        return indices, score


class RemotePreviewWriter:
    """Encodes the web remote's preview JPEG on a worker thread.

    PIL releases the GIL while it encodes, so the ~20 ms per frame no longer
    comes out of the viewfinder's 33 ms budget. Frames that arrive while one is
    being encoded replace each other.
    """

    def __init__(self, path, quality=80):
        self.path = path
        self.quality = quality
        self._cond = threading.Condition()
        self._frame = None
        self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, frame):
        """Queue a BGR preview frame; the caller must not modify it afterwards."""
        with self._cond:
            self._frame = frame
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._running = False
            self._cond.notify()

    def _run(self):
        tmp = f"{self.path}.tmp"
        while True:
            with self._cond:
                while self._running and self._frame is None:
                    self._cond.wait()
                if not self._running:
                    return
                frame, self._frame = self._frame, None
            try:
                h, w = frame.shape[:2]
                img = _pil_image().frombuffer("RGB", (w, h), np.ascontiguousarray(frame),
                                              "raw", "BGR", 0, 1)
                img.save(tmp, "JPEG", quality=self.quality)
                os.replace(tmp, self.path)
            except Exception as e:
                print(f"Remote preview error: {e}")


# --- System helpers ------------------------------------------------------------------
def _nmcli_fields(line):
    """Split a line of `nmcli -t` output: ':' separates fields, '\\:' is a literal colon."""
    fields, cur, escaped = [], [], False
    for ch in line:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def _run_quiet(cmd, timeout=5):
    """Run a command, returning (returncode, stdout); never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception as e:
        return -1, str(e)


def _local_ip():
    """Address of the interface carrying the default route, or None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write_sysfs(writes):
    """Write sysfs attributes [(path, value)], through sudo for root-only ones.

    Everything that needs root goes into a single `sudo bash` call (bash and
    tee are what setup.sh's sudoers entry allows), so standby and wake cost one
    process start instead of one per attribute.
    """
    pending = []
    for path, value in writes:
        try:
            with open(path, "w") as f:
                f.write(str(value))
        except PermissionError:
            pending.append((path, str(value)))
        except OSError as e:
            print(f"sysfs {path}: {e}")
    if not pending:
        return
    script = 'while [ $# -gt 1 ]; do printf %s "$2" > "$1" || r=1; shift 2; done; exit ${r:-0}'
    args = [x for pair in pending for x in pair]
    code, out = _run_quiet(["sudo", "-n", "/bin/bash", "-c", script, "bash", *args])
    if code != 0:
        print(f"sysfs write failed ({code}): {out.strip()}")


# --- Vector icons ---------------------------------------------------------------------
# Drawn 4x oversize and smooth-scaled down (see CameraApp._icon), which gives
# the menus anti-aliased glyphs; pygame's own primitives are not anti-aliased.

def _arc_band(surf, color, center, radius, width, a0, a1):
    """Thick arc as a polygon; angles in degrees, counter-clockwise from +x.

    pygame.draw.arc leaves gaps between its 1px passes at large widths.
    """
    cx, cy = center
    steps = max(6, int(abs(a1 - a0) / 5))
    r_out, r_in = radius + width / 2, radius - width / 2
    outer, inner = [], []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        ca, sa = math.cos(a), -math.sin(a)
        outer.append((cx + r_out * ca, cy + r_out * sa))
        inner.append((cx + r_in * ca, cy + r_in * sa))
    pygame.draw.polygon(surf, color, outer + inner[::-1])
    for a in (a0, a1):
        ra = math.radians(a)
        pygame.draw.circle(surf, color, (cx + radius * math.cos(ra), cy - radius * math.sin(ra)),
                           width / 2)


def _stroke(surf, color, points, width):
    """Polyline with round joins and caps."""
    pygame.draw.lines(surf, color, False, points, int(width))
    for p in points:
        pygame.draw.circle(surf, color, p, width / 2)


def _draw_icon_shape(surf, name, s, color, level=4):
    """Draw icon `name` filling an s x s canvas (s is the oversampled size)."""
    c = s / 2
    w = max(2, s * 0.085)
    dim = (*color[:3], 70)
    if name == "dot":
        pygame.draw.circle(surf, color, (c, c), c)
    elif name == "ring":
        pygame.draw.circle(surf, color, (c, c), c - 1, max(2, int(s * .05)))
    elif name == "close":
        _stroke(surf, color, [(s * .3, s * .3), (s * .7, s * .7)], w)
        _stroke(surf, color, [(s * .7, s * .3), (s * .3, s * .7)], w)
    elif name == "back":
        _stroke(surf, color, [(s * .6, s * .24), (s * .34, s * .5), (s * .6, s * .76)], w)
    elif name == "next":
        _stroke(surf, color, [(s * .4, s * .24), (s * .66, s * .5), (s * .4, s * .76)], w)
    elif name == "up":
        _stroke(surf, color, [(s * .24, s * .6), (s * .5, s * .34), (s * .76, s * .6)], w)
    elif name == "down":
        _stroke(surf, color, [(s * .24, s * .4), (s * .5, s * .66), (s * .76, s * .4)], w)
    elif name in ("refresh", "reboot"):
        _arc_band(surf, color, (c, c), s * .3, w, 110, 400)
        a = math.radians(110)
        tip = (c + s * .3 * math.cos(a), c - s * .3 * math.sin(a))
        head = s * .16
        pygame.draw.polygon(surf, color, [
            (tip[0] + head * .9, tip[1] - head * .55),
            (tip[0] - head * .25, tip[1] - head * .05),
            (tip[0] + head * .5, tip[1] + head * .85)])
        if name == "reboot":
            pygame.draw.circle(surf, color, (c, c), w * .8)
    elif name == "globe":
        r = s * .36
        pygame.draw.circle(surf, color, (c, c), r, int(w))
        pygame.draw.ellipse(surf, color, (c - r * .45, c - r, r * .9, r * 2), int(w))
        pygame.draw.line(surf, color, (c - r, c), (c + r, c), int(w))
        pygame.draw.line(surf, color, (c - r * .82, c - r * .5), (c + r * .82, c - r * .5), int(w * .8))
        pygame.draw.line(surf, color, (c - r * .82, c + r * .5), (c + r * .82, c + r * .5), int(w * .8))
    elif name == "hotspot":
        pygame.draw.circle(surf, color, (c, c), s * .085)
        for r in (s * .2, s * .34):
            _arc_band(surf, color, (c, c), r, w, -40, 40)
            _arc_band(surf, color, (c, c), r, w, 140, 220)
    elif name == "wifi":
        base = (c, s * .78)
        pygame.draw.circle(surf, color if level > 0 else dim, base, s * .075)
        for i, r in enumerate((s * .23, s * .39, s * .55)):
            _arc_band(surf, color if level > i + 1 else dim, base, r, w, 48, 132)
    elif name == "moon":
        pygame.draw.circle(surf, color, (c, c), s * .34)
        pygame.draw.circle(surf, (0, 0, 0, 0), (c + s * .17, c - s * .13), s * .29)
    elif name == "power":
        _arc_band(surf, color, (c, c), s * .3, w, 125, 415)
        _stroke(surf, color, [(c, s * .14), (c, s * .46)], w)
    elif name == "lock":
        body = pygame.Rect(s * .24, s * .46, s * .52, s * .38)
        pygame.draw.rect(surf, color, body, border_radius=int(s * .08))
        _arc_band(surf, color, (c, s * .46), s * .16, w * .9, 0, 180)
        _stroke(surf, color, [(c - s * .16, s * .46), (c - s * .16, s * .5)], w * .9)
        _stroke(surf, color, [(c + s * .16, s * .46), (c + s * .16, s * .5)], w * .9)
    elif name in ("eye", "eye_off"):
        _arc_band(surf, color, (c, c + s * .3), s * .42, w, 48, 132)
        _arc_band(surf, color, (c, c - s * .3), s * .42, w, 228, 312)
        pygame.draw.circle(surf, color, (c, c), s * .11)
        if name == "eye_off":
            _stroke(surf, color, [(s * .2, s * .2), (s * .8, s * .8)], w)
    elif name == "check":
        _stroke(surf, color, [(s * .24, s * .52), (s * .42, s * .7), (s * .76, s * .32)], w * 1.1)
    elif name == "trash":
        _stroke(surf, color, [(s * .2, s * .28), (s * .8, s * .28)], w)
        _stroke(surf, color, [(s * .4, s * .28), (s * .42, s * .17), (s * .58, s * .17), (s * .6, s * .28)], w * .8)
        _stroke(surf, color, [(s * .28, s * .34), (s * .32, s * .82), (s * .68, s * .82), (s * .72, s * .34)], w)
        _stroke(surf, color, [(s * .44, s * .42), (s * .45, s * .72)], w * .7)
        _stroke(surf, color, [(s * .56, s * .42), (s * .55, s * .72)], w * .7)
    elif name == "phone":
        body = pygame.Rect(s * .3, s * .14, s * .4, s * .72)
        pygame.draw.rect(surf, color, body, int(w), border_radius=int(s * .09))
        _stroke(surf, color, [(s * .44, s * .76), (s * .56, s * .76)], w * .8)
    elif name == "shift":
        pts = [(c, s * .18), (s * .8, s * .5), (s * .62, s * .5), (s * .62, s * .8),
               (s * .38, s * .8), (s * .38, s * .5), (s * .2, s * .5)]
        if level:
            pygame.draw.polygon(surf, color, pts)
        else:
            pygame.draw.polygon(surf, color, pts, int(w * .8))
        if level > 1:  # caps lock: bar under the arrow
            _stroke(surf, color, [(s * .38, s * .9), (s * .62, s * .9)], w * .8)
    elif name == "backspace":
        pts = [(s * .1, c), (s * .32, s * .24), (s * .88, s * .24), (s * .88, s * .76), (s * .32, s * .76)]
        pygame.draw.polygon(surf, color, pts, int(w * .8))
        _stroke(surf, color, [(s * .47, s * .38), (s * .71, s * .62)], w * .8)
        _stroke(surf, color, [(s * .71, s * .38), (s * .47, s * .62)], w * .8)
    elif name == "camera":
        body = pygame.Rect(s * .12, s * .3, s * .76, s * .52)
        pygame.draw.rect(surf, color, body, int(w), border_radius=int(s * .1))
        pygame.draw.rect(surf, color, (s * .36, s * .2, s * .28, s * .12), border_radius=int(s * .04))
        pygame.draw.circle(surf, color, (c, s * .56), s * .14, int(w))
    elif name == "camera_off":
        body = pygame.Rect(s * .12, s * .3, s * .76, s * .52)
        pygame.draw.rect(surf, color, body, int(w), border_radius=int(s * .1))
        pygame.draw.rect(surf, color, (s * .36, s * .2, s * .28, s * .12), border_radius=int(s * .04))
        pygame.draw.circle(surf, color, (c, s * .56), s * .14, int(w))
        _stroke(surf, color, [(s * .16, s * .14), (s * .84, s * .88)], w * 1.2)


class CameraApp:
    def __init__(self):
        """Initialize the camera application"""
        PHOTOS_DIR.mkdir(exist_ok=True)

        # Camera config - Preview ottimizzato per BOOT VELOCE
        self.photo_width, self.photo_height = SENSOR_SIZE
        # Use higher resolution for better quality, will scale to fit display
        self.preview_width = 640
        self.preview_height = 480

        # Bring the camera up on a worker thread.  Constructing Picamera2 and
        # starting the sensor is the single longest step in the boot and it does
        # not touch SDL, so it runs while this thread sets up the display, fonts,
        # icons and UI geometry.  run() waits on _camera_ready before the first
        # frame.
        self.camera = None
        self.camera_error = None
        self._camera_ready = threading.Event()
        # Set while the sensor has stopped delivering frames (see
        # _capture_preview_frame); None when streaming normally.
        self.camera_stalled_since = None
        self._last_camera_restart = 0.0
        self._first_frame = threading.Event()
        # No camera at start-up: the interface stays up and says so (see run())
        self.camera_absent = False         # no sensor is registered at all
        self._last_camera_check = 0.0
        self.relaunch = False              # a camera showed up late: main() starts over
        self._nocam_frame = None           # (signature, rotated surface)
        threading.Thread(target=self._init_camera, daemon=True).start()

        print("Initializing display...")

        # Frames go straight to the kernel framebuffer when possible; SDL then
        # only needs its "dummy" driver instead of KMSDRM + Mesa (see
        # FramebufferOutput).
        self.fb = None
        if os.environ.get("CAMERA_DISPLAY", "fbdev") != "sdl":
            self.fb = FramebufferOutput.open(FBDEV_PATH, FBDEV_WAIT)
        if self.fb is not None:
            os.environ["SDL_VIDEODRIVER"] = "dummy"
            self._recover_from_standby()

        # Only the subsystems actually used.  pygame.init() would also spin up
        # mixer/joystick, which cost time and are never touched here.
        pygame.display.init()
        pygame.font.init()

        # Get display info
        try:
            if self.fb is not None:
                self.width, self.height = self.fb.size
                self.screen = pygame.display.set_mode((self.width, self.height))
                self.fb.bind(self.screen)
                print(f"✓ Display initialized on {FBDEV_PATH} ({self.fb.describe()})")
            else:
                info = pygame.display.Info()
                self.width = info.current_w if info.current_w > 0 else 480
                self.height = info.current_h if info.current_h > 0 else 320

                try:
                    self.screen = pygame.display.set_mode((self.width, self.height), pygame.FULLSCREEN)
                    print(f"✓ Display initialized in fullscreen mode ({self.width}x{self.height})")
                except:
                    self.screen = pygame.display.set_mode((self.width, self.height))
                    print(f"✓ Display initialized in windowed mode ({self.width}x{self.height})")

            pygame.display.set_caption("Pi Camera")
            self.headless = False

        except Exception as e:
            print(f"Display init failed: {e}")
            if self.fb is not None:
                self.fb.close()
                self.fb = None
            self.headless = True
            self.width = 480
            self.height = 320
            os.environ['SDL_VIDEODRIVER'] = 'dummy'
            pygame.display.quit()
            pygame.display.init()
            self.screen = pygame.display.set_mode((self.width, self.height))

        pygame.mouse.set_visible(False)

        # Put something on the panel straight away.  The camera is still coming
        # up on the worker thread, and an early splash makes the wait visible
        # instead of leaving the boot console on screen.
        self._draw_splash()

        # CALIBRAZIONE TOUCH per display 3.5" LCD-wiki
        # Valori misurati con evtest dal tuo display
        self.touch_cal = {
            'x_min': 280,
            'x_max': 3820,
            'y_min': 290,
            'y_max': 3910,
            'swap_xy': True,
            'invert_x': True,
            'invert_y': True
        }

        # UI Scaling - MOLTO PIÙ GRANDE per 3.5"
        # Per 480x320, usiamo scale 1.2 per ingrandire tutto. Decided by the
        # height: the 3.5" MPI3508 is driven at 720x480, the same 3:2 shape as
        # its 480x320 panel (640x480 came out stretched).
        if self.height <= 480:
            self.scale = 1.2  # Pulsanti grandi
            self.font_size = 28
            self.small_font_size = 22
        else:
            self.scale = min(self.width / 1920, self.height / 1080)
            self.font_size = int(32 * self.scale)
            self.small_font_size = int(24 * self.scale)

        self.button_height = int(60 * self.scale)
        self.button_width = int(150 * self.scale)
        self.margin = int(10 * self.scale)

        self.font = load_font(self.font_size)
        self.small_font = load_font(self.small_font_size)
        # Bold for numeric values, micro for tracked-out uppercase labels.
        self.value_font = load_font(self.font_size, bold=True)
        self.small_bold_font = load_font(self.small_font_size, bold=True)
        self.micro_font = load_font(int(self.small_font_size * 0.8))
        # Menu typography, in pixels
        self.f_title = ui_font(28, bold=True)
        self.f_heading = ui_font(23, bold=True)
        self.f_body = ui_font(19)
        self.f_body_b = ui_font(19, bold=True)
        self.f_caption = ui_font(15)
        self.f_caption_b = ui_font(15, bold=True)
        self.f_key = ui_font(22, bold=True)

        print(f"DEBUG: Screen size: {self.width}x{self.height}, Scale: {self.scale}")

        # Camera settings
        self.current_iso_index = 0
        self.current_shutter_index = 0

        # GPIO button is attached by the background initializer - creating it
        # loads a pin-factory backend and is not needed to draw the first frame.
        self.capture_pending = False
        self.button = None

        # App state
        self.running = True
        self.mode = "camera"
        self.gallery_index = 0
        self.photos = []
        self.last_capture_time = 0
        self.last_capture_name = None
        self._capturing = False
        self.standby_mode = False
        self._standby_restore = []    # sysfs (path, value) pairs to put back on wake
        self.remote_active = False
        self.remote_last_heartbeat = 0
        self.grayscale_mode = False
        self.live_exposure_us = None     # Live AE readback (microseconds)
        self.live_gain = None            # Live AE readback (linear gain)

        # Focus assist: peaking overlay, magnifier and focus meter
        self.focus_peaking_mode = False
        self.peak_color_index = 0
        self.peak_level_index = 1
        self.zoom_level = 1
        self.zoom_center = (0.5, 0.5)    # in the full sensor frame, 0..1
        self.peaker = None               # FocusPeaker, started on first use
        self._last_peak_submit = 0.0
        self._meter_value = 0.0
        self._meter_peak = 0.0

        # Menus: one overlay at a time over the camera or the gallery
        self.overlay = None              # None, "menu", "wifi", "password", "confirm", "shutdown"
        self._confirm = None             # what the "confirm" overlay asks
        self._backdrop = None            # frosted snapshot behind the overlay
        self._hits = []                  # [(rect, action)] of the last overlay render
        self._overlay_frame = None       # (signature, rotated surface)
        self._flash = None               # (rect, until): highlight on the tapped control
        self._toast = None               # (text, tone, until)
        self._busy = {}                  # background actions in progress: key -> label
        self.net = {"server": None, "hotspot": None, "wifi": None, "ip": None}
        self._net_wake = threading.Event()
        self._net_thread = None
        self._hotspot_credentials = None
        self.hostname = socket.gethostname()
        self.wifi_networks = []          # Available networks
        self.wifi_saved = set()          # SSIDs NetworkManager has a profile for
        self.wifi_page = 0
        self.wifi_scanning = False
        self._connecting_ssid = None
        self._overlay_request = None     # screen switch asked for by a worker thread
        self._shutdown_text = "Spegnimento…"
        self.password_input = ""         # Current password being typed
        self.password_network = None     # Network waiting for password
        self.password_visible = False
        self._last_key_time = 0.0
        self.keyboard_shift = 0          # 0 off, 1 next letter, 2 caps lock
        self._last_shift_tap = 0.0
        self.keyboard_symbols = False

        # Gallery: decoded photos (path -> PIL image + caption), small LRU
        self._gallery_cache = {}
        self._gallery_lock = threading.Lock()
        self._gallery_loading = set()
        self._gallery_frame = None       # (signature, rotated surface)
        self._gallery_logical = None     # last gallery frame, unrotated (menu backdrop)
        self._scrims = None

        # Rendered-glyph caches for HUD text redrawn every frame
        self._label_cache = {}
        self._text_cache = {}
        self._icon_cache = {}
        self._dim_cache = {}
        self._qr_cache = {}
        self._toolbar_cache = {}

        # Preview surface cache & perf optimizations
        self.current_preview_surface = None
        self.last_preview_time = 0
        # Pre-allocated surface (set in setup_ui after preview_rect is known)
        self._preview_surf = None          # reusable pygame.Surface for the preview area
        self._preview_scaled_size = None   # (new_w, new_h) pre-calculated at setup time
        self._preview_offset = None        # (x_off, y_off) pre-calculated at setup time
        self._last_presented = None
        # FPS debug counter
        self._fps_frames = 0
        self._fps_last_t = time.monotonic()
        # Web remote: preview JPEG encoder (started on first use) and status file
        self.remote_writer = None
        self._last_remote_frame = 0.0
        self._last_status_write = 0.0

        # Prepare UDP listener. Non-blocking from the start: if the bind fails
        # (port taken), recvfrom() on a blocking socket would hang the UI.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        try:
            # Bind to all interfaces to be sure
            self.sock.bind(('0.0.0.0', UDP_PORT))
            print(f"UDP listener bound to 0.0.0.0:{UDP_PORT}")
        except Exception as e:
            print(f"UDP Bind Error: {e}")

        # Setup UI
        self.setup_ui()

        # Lazy load non-critical components in background
        print("Starting background initialization...")
        self.battery = None  # Will be initialized in background
        self.touch_device = None
        self.touch_thread = None
        self.touch_queue = queue.Queue()
        self.last_battery_update = 0
        threading.Thread(target=self._lazy_init, daemon=True).start()

    def _preview_config(self, camera):
        """Video configuration used for the live preview.

        IMPORTANTE: sensor output_size forza il modo full-frame 2x2 binned (IMX477).
        Senza questo, picamera2 sceglie automaticamente un modo croppato per output piccoli.

        Three buffers: picamera2 keeps the latest completed frame aside, so
        with two the sensor was often left without a free buffer and dropped
        frames (23 fps measured with peaking on, 30 with three).
        """
        return camera.create_video_configuration(
            main={"size": (self.preview_width, self.preview_height), "format": "RGB888"},
            sensor={"output_size": (2028, 1520)},
            buffer_count=3
        )

    def _init_camera(self):
        """Construct, configure and start the camera (runs on a worker thread).

        The service starts early in the boot, so /dev/media* may not have been
        created by udev yet.  Retrying briefly is much cheaper than ordering the
        unit after udev settles or letting systemd restart the whole app.
        """
        attempt = 0
        try:
            # Imported here rather than at module level so the main thread can
            # bring the display up meanwhile; this waits only for whatever the
            # preload thread has not finished importing yet.
            try:
                from picamera2 import Picamera2
            except Exception as e:
                self.camera_error = e
                print(f"picamera2 unavailable: {e}")
                return
            deadline = time.monotonic() + CAMERA_INIT_TIMEOUT
            # The first Picamera2() fixes the camera list for the process (see
            # _camera_devices_ready), so wait for the sensor before making it.
            while not _camera_devices_ready() and time.monotonic() < deadline:
                time.sleep(0.1)
            while True:
                attempt += 1
                try:
                    camera = Picamera2()
                    camera.configure(self._preview_config(camera))
                    camera.start()
                    # ScalerCrop: garantisce che nessun ritaglio aggiuntivo venga
                    # applicato. Usiamo l'intero sensore 4056x3040.
                    try:
                        camera.set_controls({"ScalerCrop": (0, 0, *SENSOR_SIZE)})
                    except Exception as e:
                        print(f"ScalerCrop warning: {e}")
                    self.camera = camera
                    print(f"✓ Camera started in {time.monotonic() - _T0:.2f}s "
                          f"(attempt {attempt})")
                    return
                except Exception as e:
                    if time.monotonic() >= deadline:
                        self.camera_error = e
                        print(f"Camera start failed after {attempt} attempts: {e}")
                        return
                    time.sleep(0.4)
        finally:
            # Unblocks run() on both success and definitive failure.
            self._camera_ready.set()

    def _draw_splash(self, message="Avvio fotocamera..."):
        """Paint a minimal splash so the panel is not showing the boot console."""
        try:
            self.screen.fill(BLACK)
            font = pygame.font.Font(None, 28)
            text = font.render(message, True, WHITE)
            self.screen.blit(text, text.get_rect(center=(self.width // 2, self.height // 2)))
            self._present()
        except Exception:
            pass

    def _present(self, surface=None):
        """Show a composed frame: framebuffer copy, or SDL flip as a fallback."""
        surface = surface or self.screen
        if self.fb is not None:
            self.fb.show(surface)
        else:
            if surface is not self.screen:
                self.screen.blit(surface, (0, 0))
            pygame.display.flip()
        self._last_presented = surface

    def _present_logical(self, surface):
        """Present a surface drawn the right way up (the panel is mounted upside down)."""
        self._present(pygame.transform.rotate(surface, 180))

    def _lazy_init(self):
        """Initialize non-critical components in background for faster boot"""
        # None of this is usable before run() enters its loop, which happens
        # only once the camera is up; importing evdev/gpiozero earlier (~2 s on
        # a cold boot) just competes with picamera2, and then with the first
        # frame, for the SD card and the GIL. The second wait is bounded so a
        # camera that never delivers a frame does not leave touch dead.
        self._camera_ready.wait(timeout=CAMERA_INIT_TIMEOUT + 50)
        if self.camera is not None:
            self._first_frame.wait(timeout=3.0)

        # The service now starts while udev may still be loading drivers and
        # assigning device groups, so a device missing on the first look is
        # retried until DEVICE_WAIT runs out instead of being lost for the
        # whole session. Touch comes first: with the framebuffer output it is
        # the only input there is.
        print("Background init: touch input, GPIO button, battery manager...")
        pending = [self.init_touch_input, self._init_button, self._init_battery]
        pending = [init for init in pending if not init()]

        print("Background init: Loading photos...")
        self.load_photos()

        # Warm PIL here so the first gallery open / remote frame does not pay
        # the import cost inline.
        _pil_image()

        deadline = time.monotonic() + DEVICE_WAIT
        while pending and time.monotonic() < deadline:
            time.sleep(0.5)
            pending = [init for init in pending if not init()]
        for init in pending:
            print(f"Background init: {init.__name__} gave up after {DEVICE_WAIT:.0f}s")
        if self.init_touch_input in pending:
            print("No touch device found, using pygame events")

        print(f"✓ Background initialization complete ({time.monotonic() - _T0:.2f}s)")

    def _init_button(self):
        """Attach the GPIO shutter button. Returns False to be retried.

        GPIOZERO_PIN_FACTORY is pinned to lgpio to skip backend probing; if that
        backend is unavailable we fall back to gpiozero's own search.
        """
        try:
            from gpiozero import Button
        except Exception as e:
            print(f"GPIO unavailable: {e}")
            return True  # nothing to retry

        errors = []
        for factory in (os.environ.get('GPIOZERO_PIN_FACTORY'), None):
            try:
                if factory is None:
                    os.environ.pop('GPIOZERO_PIN_FACTORY', None)
                self.button = Button(GPIO_BUTTON_PIN, pull_up=True,
                                     bounce_time=BUTTON_DEBOUNCE)
                self.button.when_pressed = self.trigger_capture
                print(f"GPIO button on pin {GPIO_BUTTON_PIN}")
                return True
            except Exception as e:
                errors.append(f"factory={factory or 'auto'}: {e}")
        # Logged once, not on every retry
        if not getattr(self, "_button_error_logged", False):
            self._button_error_logged = True
            print(f"GPIO button init failed, retrying ({'; '.join(errors)})")
        return False

    def _init_battery(self):
        """Create the battery manager once the I2C bus is usable. False = retry.

        /dev/i2c-1 appears when systemd-modules-load loads i2c-dev and becomes
        accessible once udev gives it to the i2c group.
        """
        if not os.access("/dev/i2c-1", os.R_OK | os.W_OK):
            return False
        self.battery = BatteryManager()
        return True

    def trigger_capture(self):
        """GPIO button callback"""
        self.capture_pending = True

    def init_touch_input(self):
        """Inizializza input touch diretto via evdev. Returns False to be retried."""
        global evdev
        if evdev is None:
            try:
                import evdev as _evdev
            except ImportError:
                print("Warning: python3-evdev not installed, touch might not work correctly")
                print("Install with: sudo apt-get install python3-evdev")
                return True  # nothing to retry
            evdev = _evdev
        try:
            # Trova il dispositivo touch. list_devices() skips nodes we cannot
            # open yet, i.e. ones udev has not given to the input group.
            for path in evdev.list_devices():
                try:
                    device = evdev.InputDevice(path)
                except OSError:
                    continue  # went away, or not accessible yet
                if 'ADS7846' in device.name or 'touch' in device.name.lower():
                    self.touch_device = device
                    print(f"Touch device found: {device.name}")
                    break
                device.close()

            if not self.touch_device:
                return False

            # Thread per leggere eventi touch
            self.touch_thread = threading.Thread(target=self.touch_reader_thread, daemon=True)
            self.touch_thread.start()
            print("Touch reader thread started")
        except Exception as e:
            print(f"Touch init error: {e}")
        return True

    def touch_reader_thread(self):
        """Thread per leggere eventi touch raw"""
        raw_x = 0
        raw_y = 0
        touch_active = False

        try:
            for event in self.touch_device.read_loop():
                if event.type == evdev.ecodes.EV_ABS:
                    if event.code == evdev.ecodes.ABS_X:
                        raw_x = event.value
                    elif event.code == evdev.ecodes.ABS_Y:
                        raw_y = event.value

                elif event.type == evdev.ecodes.EV_KEY:
                    if event.code == evdev.ecodes.BTN_TOUCH:
                        if event.value == 1:  # Touch press
                            touch_active = True
                        elif event.value == 0:  # Touch release
                            if touch_active:
                                # Calibra e metti in coda
                                cal_x, cal_y = self.calibrate_touch(raw_x, raw_y)
                                self.touch_queue.put(('click', cal_x, cal_y))
                                touch_active = False

        except Exception as e:
            print(f"Touch reader error: {e}")

    # ---------------- web remote ----------------

    def udp_check(self):
        """Handle commands from the web remote (photo_server.py relays them over UDP)."""
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
            except BlockingIOError:
                break
            except OSError as e:
                print(f"UDP Error: {e}")
                break
            try:
                self._handle_command(data.decode("utf-8", "replace").strip())
            except Exception as e:
                print(f"UDP command error: {e}")

    def _handle_command(self, cmd):
        name, _, arg = cmd.partition(" ")
        now = time.monotonic()
        if name in ("START_REMOTE", "HEARTBEAT", "STREAM"):
            # STREAM: an MJPEG client is connected and needs preview frames.
            if not self.remote_active:
                print(f"Remote connected ({name})")
            self.remote_active = True
            self.remote_last_heartbeat = now
            return
        print(f"UDP Command: {cmd}")
        if self.camera is None and name in CAMERA_COMMANDS:
            return
        if name == "STOP_REMOTE":
            self.remote_active = False
        elif name == "WAKE":
            self.wake_from_standby()
        elif name == "STANDBY":
            self._close_overlay()
            self.enter_standby()
        elif name == "CAPTURE":
            self.capture_pending = True
        elif name in ("ISO_UP", "ISO_DOWN", "ISO"):
            step = {"ISO_UP": 1, "ISO_DOWN": -1}.get(name)
            if step:
                self.current_iso_index = (self.current_iso_index + step) % len(ISO_VALUES)
            elif arg.isdigit() and int(arg) < len(ISO_VALUES):
                self.current_iso_index = int(arg)
            self.apply_camera_settings()
        elif name in ("SHUTTER_UP", "SHUTTER_DOWN", "SHUTTER"):
            step = {"SHUTTER_UP": 1, "SHUTTER_DOWN": -1}.get(name)
            if step:
                self.current_shutter_index = (self.current_shutter_index + step) % len(SHUTTER_SPEEDS)
            elif arg.isdigit() and int(arg) < len(SHUTTER_SPEEDS):
                self.current_shutter_index = int(arg)
            self.apply_camera_settings()
        elif name == "BW":
            self.grayscale_mode = (arg == "1") if arg in ("0", "1") else not self.grayscale_mode
            self.apply_camera_settings()
        elif name == "PEAK":
            self.set_focus_assist((arg == "1") if arg in ("0", "1") else not self.focus_peaking_mode)
        elif name == "ZOOM":
            if arg.isdigit() and int(arg) in ZOOM_LEVELS:
                self._set_zoom(int(arg))
            else:
                self._cycle_zoom()
        elif name == "ZOOM_AT":
            try:
                u, v = (float(p) for p in arg.split())
            except ValueError:
                return
            self._zoom_at(min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0))
        else:
            print(f"Unknown command: {cmd}")

    def _status_snapshot(self):
        if self.standby_mode:
            state = "standby"
        elif self.camera is None:
            state = "nocamera"
        elif self.camera_stalled_since is not None:
            state = "stalled"
        else:
            state = "active"
        battery = None
        if self.battery and self.battery.available:
            battery = int(self.battery.percentage)
        meter = None
        if self.focus_peaking_mode and self._meter_peak > 0:
            meter = round(min(1.0, self._meter_value / self._meter_peak), 3)
        return {
            "t": time.time(),
            "status": state,
            "mode": "remote" if self.remote_active else "local",
            "iso": str(ISO_VALUES[self.current_iso_index]),
            "iso_index": self.current_iso_index,
            "iso_values": [str(v) for v in ISO_VALUES],
            "shutter": SHUTTER_SPEEDS[self.current_shutter_index][0],
            "shutter_index": self.current_shutter_index,
            "shutter_values": [name for name, _ in SHUTTER_SPEEDS],
            "live_shutter": self._live_shutter_text(),
            "live_iso": int(self.live_gain * 100) if self.live_gain else None,
            "bw": self.grayscale_mode,
            "peaking": self.focus_peaking_mode,
            "zoom": self.zoom_level,
            "focus": meter,
            "battery": battery,
            "capturing": self._capturing,
            "last_capture": self.last_capture_name,
            "photos": len(self.photos),
        }

    def _write_status(self):
        """Publish the settings for photo_server.py (atomic replace, ~0.2 ms)."""
        self._last_status_write = time.monotonic()
        try:
            tmp = f"{SHARED_MEM_STATUS}.tmp"
            with open(tmp, "w") as f:
                json.dump(self._status_snapshot(), f)
            os.replace(tmp, SHARED_MEM_STATUS)
        except Exception as e:
            print(f"Status write error: {e}")

    def _feed_remote(self, frame):
        """Hand a preview frame to the JPEG encoder while a remote is watching."""
        if not self.remote_active:
            return
        now = time.monotonic()
        if now - self._last_remote_frame < 1.0 / REMOTE_FPS:
            return
        self._last_remote_frame = now
        if self.remote_writer is None:
            self.remote_writer = RemotePreviewWriter(SHARED_MEM_PREVIEW)
        self.remote_writer.submit(frame)

    # ---------------- layout ----------------

    def setup_ui(self):
        """Setup UI - SIDEBARS LAYOUT"""
        # Define sidebar width. On screens wider than 4:3 the sidebars take
        # the extra width, so the 4:3 image keeps letterbox bars of ~40 px for
        # the toasts above it and the focus toolbar below.
        self.sidebar_width = max(int(70 * self.scale),
                                 (self.width - int(self.height * 0.83) * 4 // 3) // 2)

        # Left sidebar rect
        self.left_bar_rect = pygame.Rect(0, 0, self.sidebar_width, self.height)

        # Right sidebar rect
        self.right_bar_rect = pygame.Rect(self.width - self.sidebar_width, 0, self.sidebar_width, self.height)

        # Preview Rect - Central area between sidebars
        # Available width for preview
        preview_w = self.width - (2 * self.sidebar_width)
        self.preview_rect = pygame.Rect(self.sidebar_width, 0, preview_w, self.height)

        print(f"DEBUG: Preview Rect: {self.preview_rect}")

        # --- Pre-allocate preview rendering resources ---
        # Calculate the fixed scale factor and scaled dimensions once
        cam_w, cam_h = self.preview_width, self.preview_height
        scale_x = self.preview_rect.width  / cam_w
        scale_y = self.preview_rect.height / cam_h
        sf = min(scale_x, scale_y)
        new_w = int(cam_w * sf)
        new_h = int(cam_h * sf)
        self._preview_scaled_size = (new_w, new_h)
        self._preview_offset = (
            (self.preview_rect.width  - new_w) // 2,
            (self.preview_rect.height - new_h) // 2,
        )
        # The image itself; the letterbox bars above and below it hold the
        # toasts and the focus-assist toolbar.
        self.image_rect = pygame.Rect(self.preview_rect.x + self._preview_offset[0],
                                      self.preview_rect.y + self._preview_offset[1],
                                      new_w, new_h)
        # Single reusable surface for the preview area (BLACK background)
        self._preview_surf = pygame.Surface(
            (self.preview_rect.width, self.preview_rect.height)
        )
        self._preview_surf.fill(BLACK)
        # Reusable full-screen compositing surface for draw_camera_ui()
        self._ui_surface = pygame.Surface((self.width, self.height))
        # Sidebar chrome is static, so it is rendered once here rather than
        # re-drawn 30x/second. Each is a subtle vertical gradient with a
        # hairline on the edge facing the preview.
        self._left_bar_surf = self._make_sidebar_surface(edge="right")
        self._right_bar_surf = self._make_sidebar_surface(edge="left")
        print(f"DEBUG: Preview scaled size: {self._preview_scaled_size}, offset: {self._preview_offset}")

        # Button Radius
        radius = 30
        self.btn_radius = radius

        # Centers for Left Sidebar Buttons
        # SHUTTER TOP (25%), ISO BOTTOM (75%)
        left_center_x = self.sidebar_width // 2

        self.shutter_center = (left_center_x, int(self.height * 0.25))
        self.iso_center = (left_center_x, int(self.height * 0.75))

        # Arrow Rects for Touch Detection
        arrow_size = int(40 * self.scale)
        half_arrow = arrow_size // 2

        # Shutter Arrows (Top)
        self.shutter_up_rect = pygame.Rect(self.shutter_center[0] - half_arrow, self.shutter_center[1] - 70, arrow_size, arrow_size)
        self.shutter_down_rect = pygame.Rect(self.shutter_center[0] - half_arrow, self.shutter_center[1] + 55, arrow_size, arrow_size)

        # ISO Arrows (Bottom)
        self.iso_up_rect = pygame.Rect(self.iso_center[0] - half_arrow, self.iso_center[1] - 70, arrow_size, arrow_size)
        self.iso_down_rect = pygame.Rect(self.iso_center[0] - half_arrow, self.iso_center[1] + 55, arrow_size, arrow_size)

        # Centers for Right Sidebar Buttons
        # Power, Gallery, B/W, Focus Peaking - 4 equally distributed
        right_center_x = self.width - (self.sidebar_width // 2)

        self.power_center   = (right_center_x, int(self.height * 0.125))
        self.gallery_center = (right_center_x, int(self.height * 0.375))
        self.bw_center      = (right_center_x, int(self.height * 0.625))
        self.fp_center      = (right_center_x, int(self.height * 0.875))  # Focus Peaking

        # Button Rects for Right Sidebar
        btn_touch_size = int(60 * self.scale)
        self.power_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.power_rect.center = self.power_center

        self.gallery_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.gallery_rect.center = self.gallery_center

        self.bw_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.bw_rect.center = self.bw_center

        self.fp_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.fp_rect.center = self.fp_center

        self._layout_focus_toolbar()

        # Gallery: round buttons over the photo
        self.back_btn_rect = pygame.Rect(14, 14, 56, 56)
        self.trash_btn_rect = pygame.Rect(self.width - 70, 14, 56, 56)

    def _layout_focus_toolbar(self):
        """Focus-assist toolbar: in the letterbox bar under the image, or over its bottom edge."""
        img = self.image_rect
        below = self.preview_rect.bottom - img.bottom
        self._toolbar_floating = below < 36
        if self._toolbar_floating:
            bar = pygame.Rect(img.x, img.bottom - 52, img.w, 52)
        else:
            bar = pygame.Rect(img.x, img.bottom, img.w, below)
        h = min(46, bar.h - 6)
        y = bar.centery - h // 2
        pad, gap = 8, 8
        zoom = pygame.Rect(bar.x + pad, y, 76, h)
        level = pygame.Rect(bar.right - pad - 62, y, 62, h)
        color = pygame.Rect(level.x - gap - 52, y, 52, h)
        meter = pygame.Rect(zoom.right + gap, y, color.x - gap - zoom.right - gap, h)
        self.toolbar = {"bar": bar, "zoom": zoom, "meter": meter, "color": color, "level": level}

    # ---------------- network & system (background) ----------------

    def _start_net_monitor(self):
        """Poll server / hotspot / Wi-Fi state while a menu is open.

        Probing from the drawing code meant spawning systemctl and nmcli in the
        middle of a frame; this thread refreshes self.net every few seconds,
        or at once after an action (self._net_wake).
        """
        if self._net_thread is not None and self._net_thread.is_alive():
            self._net_wake.set()
            return
        self._net_thread = threading.Thread(target=self._net_monitor, daemon=True)
        self._net_thread.start()

    def _net_monitor(self):
        while self.running and self.overlay is not None and not self.standby_mode:
            self._probe_network()
            self._net_wake.wait(timeout=3.0)
            self._net_wake.clear()

    def _probe_network(self):
        code, out = _run_quiet(['systemctl', 'is-active', 'photo-server'], timeout=3)
        server = out.strip() == "active"
        code, out = _run_quiet(['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'], timeout=3)
        hotspot, wifi = False, None
        for line in out.splitlines():
            fields = _nmcli_fields(line)
            if len(fields) < 2:
                continue
            if fields[0] == HOTSPOT_CONNECTION:
                hotspot = True
            elif fields[1] == "802-11-wireless" and wifi is None:
                wifi = fields[0]
        if wifi == "preconfigured":
            # Raspberry Pi Imager names the Wi-Fi it sets up "preconfigured"
            code, out = _run_quiet(['nmcli', '-t', '-g', '802-11-wireless.ssid',
                                    'con', 'show', 'preconfigured'], timeout=3)
            wifi = out.strip() or wifi
        if hotspot and self._hotspot_credentials is None:
            code, out = _run_quiet(['sudo', '-n', 'nmcli', '-s', '-t', '-g',
                                    '802-11-wireless.ssid,802-11-wireless-security.psk',
                                    'con', 'show', HOTSPOT_CONNECTION], timeout=3)
            lines = out.splitlines()
            if code == 0 and len(lines) >= 2 and lines[0]:
                self._hotspot_credentials = (lines[0], lines[1])
        self.net = {"server": server, "hotspot": hotspot, "wifi": wifi,
                    "ip": HOTSPOT_IP if hotspot else _local_ip()}

    def _run_background(self, key, label, fn):
        """Run a slow action (systemctl, nmcli) off the UI thread, shown as busy."""
        if key in self._busy:
            return
        self._busy[key] = label

        def worker():
            try:
                fn()
            except Exception as e:
                print(f"{key} failed: {e}")
                self._show_toast("Operazione non riuscita", "danger")
            finally:
                self._busy.pop(key, None)
                self._probe_network()

        threading.Thread(target=worker, daemon=True).start()

    def toggle_server(self):
        """Start or stop the web server (photo_server.py)."""
        start = not self.net.get("server")

        def run():
            code, out = _run_quiet(['sudo', 'systemctl', 'start' if start else 'stop',
                                    'photo-server'], timeout=20)
            if code == 0:
                self._show_toast("Server web avviato" if start else "Server web fermato",
                                 "success" if start else "info")
            else:
                self._show_toast("Il server web non risponde", "danger")
        self._run_background("server", "Avvio…" if start else "Arresto…", run)

    def toggle_hotspot(self):
        """Bring the hotspot profile up or down."""
        start = not self.net.get("hotspot")

        def run():
            code, out = _run_quiet(['sudo', 'nmcli', 'con', 'up' if start else 'down',
                                    HOTSPOT_CONNECTION], timeout=40)
            if code == 0:
                self._show_toast("Hotspot attivo" if start else "Hotspot spento",
                                 "success" if start else "info")
            else:
                self._show_toast("Hotspot non disponibile", "danger")
        self._run_background("hotspot", "Attivazione…" if start else "Spegnimento…", run)

    def scan_wifi_networks(self):
        """Scan for Wi-Fi networks in the background (results in self.wifi_networks)."""
        if self.wifi_scanning:
            return
        self.wifi_scanning = True

        def run():
            try:
                _run_quiet(['sudo', 'nmcli', 'device', 'wifi', 'rescan'], timeout=8)
                time.sleep(1)  # Wait for scan to complete
                code, out = _run_quiet(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY',
                                        'device', 'wifi', 'list'], timeout=8)
                best = {}
                for line in out.splitlines():
                    fields = _nmcli_fields(line)
                    if len(fields) < 3 or not fields[0]:
                        continue  # Skip hidden networks
                    signal = int(fields[1]) if fields[1].isdigit() else 0
                    net = {'ssid': fields[0], 'signal': signal,
                           'security': fields[2] if fields[2] not in ('', '--') else 'Open'}
                    # One row per SSID (strongest access point)
                    if net['ssid'] not in best or signal > best[net['ssid']]['signal']:
                        best[net['ssid']] = net
                networks = sorted(best.values(), key=lambda n: n['signal'], reverse=True)
                # Profiles NetworkManager already has: these connect without
                # asking for the password again (named after their SSID,
                # except Raspberry Pi Imager's "preconfigured").
                code, out = _run_quiet(['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'], timeout=5)
                saved = {f[0] for f in map(_nmcli_fields, out.splitlines())
                         if len(f) >= 2 and f[1] == "802-11-wireless" and f[0] != HOTSPOT_CONNECTION}
                if self.net.get("wifi"):
                    saved.add(self.net["wifi"])
                self.wifi_saved = saved
                self.wifi_networks = networks[:15]
                self.wifi_page = 0
                print(f"Found {len(self.wifi_networks)} WiFi networks")
            finally:
                self.wifi_scanning = False
        threading.Thread(target=run, daemon=True).start()

    def connect_to_wifi(self, ssid, password=None):
        """Connect to a Wi-Fi network in the background; reports with a toast."""
        def run():
            cmd = ['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid]
            if password:
                cmd += ['password', password]
            code, out = _run_quiet(cmd, timeout=30)
            if code == 0:
                print(f"✓ Connected to {ssid}")
                self._show_toast(f"Connessa a {ssid}", "success")
                self.password_input = ""
                # Back to the menu; screens are switched by the main loop
                self._overlay_request = "menu"
            else:
                print(f"Connection failed: {out.strip()}")
                self._show_toast("Connessione non riuscita: controlla la password"
                                 if password else "Connessione non riuscita", "danger")
        self._run_background("wifi", "Connessione…", run)

    def _show_toast(self, text, tone="info", seconds=2.6):
        self._toast = (text, tone, time.monotonic() + seconds)

    def delete_photo(self):
        """Delete current photo in gallery"""
        if not self.photos:
            return

        try:
            # Get current photo path
            photo_path = self.photos[self.gallery_index]

            # Delete file
            os.remove(photo_path)
            print(f"Deleted photo: {photo_path}")

            # Remove from list
            self.photos.pop(self.gallery_index)

            # Remove from cache if present
            with self._gallery_lock:
                self._gallery_cache.pop(photo_path, None)

            # Adjust index
            if self.gallery_index >= len(self.photos):
                self.gallery_index = len(self.photos) - 1
            if self.gallery_index < 0:
                self.gallery_index = 0
            self._show_toast("Foto eliminata", "info")

        except Exception as e:
            print(f"Error deleting photo: {e}")
            self._show_toast("Impossibile eliminare la foto", "danger")

    # ---------------- standby ----------------

    def _set_display_power(self, on):
        """Switch the video output (and any sysfs backlight) on or off.

        fbN/blank powers the CRTC down through the KMS driver, so the HDMI
        signal stops. Panels with a backlight device (DSI, SPI) are switched
        through it too. HDMI panels such as the MPI3508 keep their own
        backlight lit without a signal: only their key turns it off.
        """
        fb = os.path.basename(FBDEV_PATH)
        writes = [(f"/sys/class/graphics/{fb}/blank", 0 if on else 4)]
        writes += [(f"{bl}/bl_power", 0 if on else 4) for bl in glob.glob("/sys/class/backlight/*")]
        _write_sysfs(writes)

    @staticmethod
    def _save_standby_state(restore):
        try:
            with open(STANDBY_STATE, "w") as f:
                json.dump(restore, f)
        except OSError as e:
            print(f"Standby state not saved: {e}")

    @staticmethod
    def _clear_standby_state():
        try:
            os.remove(STANDBY_STATE)
        except OSError:
            pass

    def _recover_from_standby(self):
        """Undo a standby that the previous run did not get to end (killed, crashed).

        It leaves the output powered down, the CPU on the powersave governor
        and the LED blinking. Cheap to check (a file in /tmp and a sysfs read);
        sudo runs only when there is something to undo.
        """
        restore = None
        try:
            with open(STANDBY_STATE) as f:
                restore = [tuple(item) for item in json.load(f)]
        except (OSError, ValueError, TypeError):
            pass
        dark = any(_read_sysfs(p) == "Off" for p in glob.glob("/sys/class/drm/card*-HDMI-*/dpms"))
        if restore is None and not dark:
            return
        print("The previous run ended in standby: restoring display and CPU")
        _write_sysfs(restore or [])
        self._set_display_power(True)
        self._clear_standby_state()

    def enter_standby(self):
        """Display off, sensor stopped, CPU slowed down; network and web server stay up.

        Raspberry Pi kernels support no system sleep state (/sys/power/state is
        empty), so this is as deep as it goes. The web server and the hotspot
        keep running, so photos can still be downloaded and the page can wake
        the camera; the old version stopped the server and never restarted it.
        Wake with a touch, the shutter button or the web remote.
        """
        if self.standby_mode:
            return
        print("Entering standby...")
        self._close_overlay()
        self.standby_mode = True
        if self.camera is not None:
            try:
                # Pending requests would make stop() wait for a frame
                self.camera.cancel_all_and_flush()
                self.camera.stop()
                print("✓ Camera stopped")
            except Exception as e:
                print(f"Camera stop warning: {e}")

        # Black frame first, in case the output cannot be switched off
        self.screen.fill(BLACK)
        self._present()

        restore, writes = [], []
        governor = _read_sysfs(CPUFREQ_GOVERNOR)
        if governor and governor != "powersave":
            restore.append((CPUFREQ_GOVERNOR, governor))
            writes.append((CPUFREQ_GOVERNOR, "powersave"))
        trigger = _read_sysfs(f"{ACT_LED}/trigger")
        if trigger and "[" in trigger and "timer" in trigger:
            current = trigger.split("[", 1)[1].split("]", 1)[0]
            restore.append((f"{ACT_LED}/trigger", current))
            # The timer trigger creates delay_on/delay_off when it is selected
            writes.append((f"{ACT_LED}/trigger", "timer"))
        # Written before anything changes, in case this run does not live to wake up
        self._save_standby_state(restore)
        _write_sysfs(writes)
        if any(path.endswith("trigger") for path, _ in writes):
            _write_sysfs([(f"{ACT_LED}/delay_on", 80), (f"{ACT_LED}/delay_off", 2920)])
        self._standby_restore = restore
        self._set_display_power(False)
        self._write_status()
        print("💤 Standby (display off, sensor stopped)")

    def wake_from_standby(self):
        """Restore display and camera after standby."""
        if not self.standby_mode:
            return
        print("Waking from standby...")
        # CPU speed first, so the rest runs at full clock
        _write_sysfs(self._standby_restore)
        self._standby_restore = []
        self._clear_standby_state()
        self._set_display_power(True)
        if self.camera is not None:
            try:
                self._restart_preview()
                print("✓ Camera restarted")
            except Exception as e:
                print(f"Wake error: {e}")
                # The stall recovery in _capture_preview_frame keeps retrying
                self.camera_stalled_since = time.monotonic()
        self.standby_mode = False
        self._last_presented = None
        self._write_status()
        print("✓ Wake complete")

    # ---------------- camera control ----------------

    def _scaler_crop(self):
        """ScalerCrop for the magnifier: a 4:3 window around zoom_center."""
        sw, sh = SENSOR_SIZE
        if self.zoom_level <= 1:
            return (0, 0, sw, sh)
        w = int(sw / self.zoom_level) & ~1
        h = int(sh / self.zoom_level) & ~1
        cx, cy = self.zoom_center
        x = int(min(max(cx * sw - w / 2, 0), sw - w)) & ~1
        y = int(min(max(cy * sh - h / 2, 0), sh - h)) & ~1
        return (x, y, w, h)

    def apply_camera_settings(self):
        """Send ISO, shutter, B/W, anti-flicker and the magnifier crop in one call.

        A value of 0 for ExposureTime / AnalogueGain hands it back to the AE
        algorithm (picamera2 switches ExposureTimeMode / AnalogueGainMode). So
        a manual ISO with an automatic shutter now still adapts the exposure
        time; AeEnable=False used to freeze both.
        """
        if self.camera is None:
            return
        iso = ISO_VALUES[self.current_iso_index]
        shutter_name, shutter_us = SHUTTER_SPEEDS[self.current_shutter_index]
        controls = {
            "AnalogueGain": 0.0 if iso == "Auto" else iso / 100.0,
            "ExposureTime": shutter_us,
            "Saturation": 0.0 if self.grayscale_mode else 1.0,
            "ScalerCrop": self._scaler_crop(),
        }
        # Slow shutters need a longer frame time
        requested_fps = 1000000.0 / shutter_us if shutter_us > 0 else 30.0
        controls["FrameRate"] = (0.1, requested_fps) if requested_fps < 30.0 else (30.0, 30.0)
        # Anti-flicker 50 Hz (libcamera 0.5+ replaced AePowerLineFrequency)
        available = self.camera.camera_controls
        if "AeFlickerMode" in available and "AeFlickerPeriod" in available:
            controls["AeFlickerMode"] = 1    # FlickerManual
            controls["AeFlickerPeriod"] = 10000
        elif "AePowerLineFrequency" in available:
            controls["AePowerLineFrequency"] = 1
        unsupported = [k for k in controls if k != "FrameRate" and k not in available]
        for key in unsupported:
            controls.pop(key)
        try:
            self.camera.set_controls(controls)
        except Exception as e:
            print(f"Warning: could not apply camera settings: {e}")

    def _restart_preview(self, flush=False):
        """(Re)configure and start the preview stream with the current settings.

        configure() starts picamera2 over with an empty control list, so ISO,
        shutter, B/W and the magnifier crop are sent again after every start
        (B/W used to be lost after a capture).
        """
        if flush:
            # A timed-out capture job is still queued, and stop() would
            # wait behind it for a frame that never comes.
            self.camera.cancel_all_and_flush()
        self.camera.stop()
        self.camera.configure(self._preview_config(self.camera))
        self.camera.start()
        self.apply_camera_settings()

    def capture_photo(self):
        """Capture high resolution photo"""
        current_time = time.time()
        if current_time - self.last_capture_time < BUTTON_DEBOUNCE:
            return
        self.last_capture_time = current_time

        if self.camera is None:
            print("Capture skipped: no camera")
            self._show_toast("Fotocamera non rilevata", "danger")
            return

        if self.camera_stalled_since is not None:
            print("Capture skipped: the camera is not delivering frames")
            self._show_toast("Fotocamera non disponibile", "danger")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = PHOTOS_DIR / f"photo_{timestamp}.jpg"
        failed = False
        self._capturing = True
        self._write_status()

        try:
            print(f"Capturing to {filename}...")

            # Feedback: a brief white flash, then the frozen frame with a
            # "saving" badge for the second or so the capture takes.
            self.screen.fill(WHITE)
            self._present()
            time.sleep(0.06)
            self._present_logical(self._saving_frame())

            # Stop preview
            self.camera.stop()

            # Config alta risoluzione. Photos are always the full frame: the
            # magnifier is a focusing aid and is left on capture.
            self._set_zoom(1, apply=False)
            still_config = self.camera.create_still_configuration(
                main={"size": (self.photo_width, self.photo_height), "format": "RGB888"}
            )
            self.camera.configure(still_config)
            self.camera.start()

            self.apply_camera_settings()
            time.sleep(0.3)

            # Capture. Bounded so a sensor that stops streaming mid-capture
            # cannot freeze the UI (TimeoutError lands in the handler below).
            self.camera.capture_file(str(filename), wait=STILL_TIMEOUT)
            print(f"Photo saved: {filename}")
            self.last_capture_name = filename.name
            self._show_toast("Foto salvata", "success")

        except Exception as e:
            failed = True
            print(f"Capture error: {e}")
            import traceback
            traceback.print_exc()
            self._show_toast("Scatto non riuscito", "danger")

        # Restore preview
        try:
            self._restart_preview(flush=failed)
        except Exception as e:
            print(f"Preview restart failed: {e}")
            self.camera_stalled_since = time.monotonic()
        self._capturing = False
        self.load_photos()
        self._write_status()

    def load_photos(self):
        """Load photo list"""
        self.photos = sorted(PHOTOS_DIR.glob("*.jpg"), reverse=True)
        if self.gallery_index >= len(self.photos):
            self.gallery_index = max(0, len(self.photos) - 1)

    def _capture_preview_frame(self):
        """Next preview frame, or None while the sensor is not delivering any.

        A bare capture_array() waits forever. When the CSI link drops ("Camera
        frontend has timed out" in the journal) the whole UI froze with it,
        power menu included. With a bounded wait the loop keeps running, a
        warning is drawn over the last frame, and the camera is restarted every
        CAMERA_RESTART_INTERVAL until frames come back.
        """
        now = time.monotonic()
        wait = FRAME_TIMEOUT
        if self.camera_stalled_since is not None:
            if now - self._last_camera_restart < CAMERA_RESTART_INTERVAL:
                return None
            self._last_camera_restart = now
            self._restart_camera()
            wait = CAMERA_RESTART_WAIT
        try:
            # Exposure and gain come from the same request. The old separate
            # capture_metadata() thread took a frame every 0.4 s away from the
            # viewfinder (27.5 instead of 30 fps).
            request = self.camera.capture_request(wait=wait)
            try:
                frame = request.make_array("main")
                metadata = request.get_metadata()
            finally:
                request.release()
            self.live_exposure_us = metadata.get("ExposureTime")
            self.live_gain = metadata.get("AnalogueGain")
        except TimeoutError:
            if self.camera_stalled_since is None:
                self.camera_stalled_since = now
                self._last_camera_restart = now
                print(f"⚠ No frame from the camera for {FRAME_TIMEOUT:.0f}s: "
                      "check the ribbon cable")
            # Drop the timed-out request (and any metadata request queued
            # behind it) so they do not pile up while the sensor is silent.
            self.camera.cancel_all_and_flush()
            return None
        if self.camera_stalled_since is not None:
            print(f"✓ Camera recovered after {now - self.camera_stalled_since:.1f}s")
            self.camera_stalled_since = None
        return frame

    def _restart_camera(self):
        """Stop and restart streaming; the sensor is re-programmed on start."""
        try:
            self.camera.cancel_all_and_flush()
            self.camera.stop()
            self.camera.start()
            self.apply_camera_settings()
            print("Camera restarted")
        except Exception as e:
            print(f"Camera restart failed: {e}")

    # ---------------- focus assist ----------------

    def set_focus_assist(self, on):
        """Peaking overlay plus magnifier and focus meter (the PEAK button)."""
        self.focus_peaking_mode = on
        print(f"Focus peaking: {on}")
        if not on and self.zoom_level != 1:
            self._set_zoom(1)
        if self.peaker is not None:
            self.peaker.reset()
        self._meter_value = self._meter_peak = 0.0

    def _set_zoom(self, level, apply=True):
        if level == 1:
            self.zoom_center = (0.5, 0.5)
        if level == self.zoom_level:
            return
        self.zoom_level = level
        if self.peaker is not None:
            self.peaker.reset()
        self._meter_value = self._meter_peak = 0.0
        if apply:
            self.apply_camera_settings()

    def _cycle_zoom(self):
        i = ZOOM_LEVELS.index(self.zoom_level) if self.zoom_level in ZOOM_LEVELS else 0
        self._set_zoom(ZOOM_LEVELS[(i + 1) % len(ZOOM_LEVELS)])

    def _zoom_at(self, u, v):
        """Magnify around the point (u, v) of the current view (0..1 each)."""
        x, y, w, h = self._scaler_crop()
        sw, sh = SENSOR_SIZE
        self.zoom_center = ((x + u * w) / sw, (y + v * h) / sh)
        if self.zoom_level == 1:
            self._set_zoom(ZOOM_LEVELS[1])
        else:
            if self.peaker is not None:
                self.peaker.reset()
            self._meter_value = self._meter_peak = 0.0
            self.apply_camera_settings()

    def _paint_peaking(self, frame):
        """Hand the frame to the peaking worker and paint its latest result."""
        now = time.monotonic()
        if self.peaker is None:
            self.peaker = FocusPeaker()
        if not self.peaker.busy and now - self._last_peak_submit >= PEAK_INTERVAL:
            self._last_peak_submit = now
            self.peaker.submit(frame, PEAK_LEVELS[self.peak_level_index][1])
        result = self.peaker.result
        if result is not None and result[1] == frame.shape[:2]:
            r, g, b = PEAK_COLORS[self.peak_color_index][1]
            frame.reshape(-1, 3)[result[2]] = (b, g, r)  # frame is BGR
        score = self.peaker.score
        if score is not None:
            self._meter_value += 0.5 * (score - self._meter_value)
            # Peak hold that relaxes over a few seconds, so a new scene rescales
            self._meter_peak = max(self._meter_value, self._meter_peak * 0.996)

    def update_preview(self):
        """Capture e mostra preview – OTTIMIZZATO per bassa latenza"""
        try:
            # FPS debug: stampa ogni 5 secondi
            if DEBUG_MODE:
                self._fps_frames += 1
                _now = time.monotonic()
                if _now - self._fps_last_t >= 5.0:
                    fps = self._fps_frames / (_now - self._fps_last_t)
                    print(f"[Preview] FPS: {fps:.1f}")
                    self._fps_frames = 0
                    self._fps_last_t = _now

            # ── 1. Cattura frame (l'unica operazione bloccante) ──
            frame = self._capture_preview_frame()

            if frame is None or len(frame.shape) != 3:
                return

            # The frame is BGR (picamera2 "RGB888"). pygame reads that order
            # directly; the old frame[:, :, ::-1] copy cost ~15 ms per frame.
            if not frame.flags.c_contiguous:
                frame = np.ascontiguousarray(frame)

            # ── 2. Focus peaking, painted into the frame (the remote sees it too) ──
            if self.focus_peaking_mode:
                self._paint_peaking(frame)

            h, w = frame.shape[:2]
            new_w, new_h = self._preview_scaled_size
            x_off, y_off  = self._preview_offset

            # ── 3. Surface dal buffer numpy senza copie (buffer protocol) ──
            src_surf = pygame.image.frombuffer(frame, (w, h), 'BGR')

            # ── 4. Scale C-level (SDL) alle dimensioni pre-calcolate ──
            scaled_surf = pygame.transform.scale(src_surf, (new_w, new_h))

            # ── 5. Blit sulla surface pre-allocata (zero nuove allocazioni) ──
            self._preview_surf.blit(scaled_surf, (x_off, y_off))

            # ── 6. Pubblica la surface per draw_camera_ui ──
            self.current_preview_surface = self._preview_surf

            # ── 7. Web remote preview, encoded on a worker thread ──
            self._feed_remote(frame)

        except Exception as e:
            print(f"Update Preview Error: {e}")

    def _feed_remote_only(self):
        """While a menu covers the viewfinder, keep the web remote's preview going."""
        if not self.remote_active or self.camera is None or self.camera_stalled_since is not None:
            return
        if time.monotonic() - self._last_remote_frame < 1.0 / REMOTE_FPS:
            return
        try:
            frame = self.camera.capture_array("main", wait=FRAME_TIMEOUT)
        except Exception:
            return
        if frame is not None and len(frame.shape) == 3:
            self._feed_remote(np.ascontiguousarray(frame))

    # ---------------- camera HUD ----------------

    def _live_shutter_text(self):
        """Format the live AE exposure as a shutter fraction, or None if unknown."""
        us = self.live_exposure_us
        if not us:
            return None
        if us > 0:
            denom = int(1_000_000 / us)
            return f"1/{denom}" if denom > 1 else f"{us / 1_000_000:.2f}s"
        return "--"

    def _draw_control_group(self, surface, center, up_center, down_center,
                            label, is_auto, live_value, manual_value):
        """One sidebar control: chevron / label / value / chevron inside a card.

        Grouping the label and value on a raised card separates them from the
        preview; previously they floated directly on black.
        """
        cx, cy = center
        card = pygame.Rect(0, 0, self.sidebar_width - 12, 62)
        card.center = (cx, cy + 6)
        self.draw_card(surface, card, radius=10)

        self.draw_arrow(surface, up_center, "up")
        self.draw_label(surface, label, (cx, cy - 14))

        if is_auto and live_value:
            # "AUTO" as a quiet chip, with the live readback in accent below.
            self.draw_label(surface, "AUTO", (cx, cy + 4), color=TEXT_LOW)
            live_txt = self._text(self.small_bold_font, live_value, ACCENT)
            surface.blit(live_txt, live_txt.get_rect(center=(cx, cy + 20)))
        else:
            value_txt = self._text(self.value_font, manual_value, TEXT_HI)
            surface.blit(value_txt, value_txt.get_rect(center=(cx, cy + 12)))

        self.draw_arrow(surface, down_center, "down")

    def draw_camera_ui(self):
        """Camera HUD over the live preview, drawn the right way up (see _compose)."""
        # Surface riusata per l'intera UI: allocarne una nuova a ogni frame
        # costava una malloc + clear di ~600 KB 30 volte al secondo.
        ui_surface = self._ui_surface
        ui_surface.fill(BLACK)

        # 1. Preview (sfondo)
        if self.camera is None:
            self._draw_no_camera(ui_surface)
        elif self.current_preview_surface:
            ui_surface.blit(self.current_preview_surface, self.preview_rect)
        else:
            pygame.draw.rect(ui_surface, SURFACE_1, self.preview_rect)
            text = self.small_font.render("Caricamento…", True, TEXT_MID)
            text_rect = text.get_rect(center=self.preview_rect.center)
            ui_surface.blit(text, text_rect)

        # The image above is the last frame received; say so if it is stale.
        if self.camera_stalled_since is not None:
            self._draw_stall_warning(ui_surface)

        # 2. Sidebars (Left and Right) - pre-rendered gradient chrome
        ui_surface.blit(self._left_bar_surf, self.left_bar_rect)
        ui_surface.blit(self._right_bar_surf, self.right_bar_rect)

        # 3. SHUTTER Area (Left Top)
        self._draw_control_group(
            ui_surface,
            center=self.shutter_center,
            up_center=self.shutter_up_rect.center,
            down_center=self.shutter_down_rect.center,
            label="SHUTTER",
            is_auto=SHUTTER_SPEEDS[self.current_shutter_index][1] == 0,
            live_value=self._live_shutter_text(),
            manual_value=SHUTTER_SPEEDS[self.current_shutter_index][0],
        )

        # 4. ISO Area (Left Bottom)
        self._draw_control_group(
            ui_surface,
            center=self.iso_center,
            up_center=self.iso_up_rect.center,
            down_center=self.iso_down_rect.center,
            label="ISO",
            is_auto=ISO_VALUES[self.current_iso_index] == "Auto",
            live_value=(f"ISO {int(self.live_gain * 100)}" if self.live_gain else None),
            manual_value=str(ISO_VALUES[self.current_iso_index]),
        )

        # 5. Right Sidebar Procedural Icons
        self.draw_icon(ui_surface, "power", self.power_center)
        self.draw_icon(ui_surface, "grid", self.gallery_center)
        self.draw_icon(ui_surface, "bw", self.bw_center, active=self.grayscale_mode)
        self.draw_icon(ui_surface, "fp", self.fp_center, active=self.focus_peaking_mode)

        if self.camera is None:
            # Everything that needs a camera is dimmed; taps on it are ignored
            lower_right = pygame.Rect(self.right_bar_rect.x, self.height // 2,
                                      self.right_bar_rect.w, self.height - self.height // 2)
            for rect in (self.left_bar_rect, lower_right):
                ui_surface.blit(self._dim(rect.size), rect.topleft)

        # 6. Battery Indicator (Top Right)
        if self.battery and self.battery.available:
            batt_val = int(self.battery.percentage)
            # Pill icon + numeral, right-aligned in the top-right corner.
            batt_text = self._text(self.small_bold_font, f"{batt_val}%", TEXT_HI)
            icon_rect = pygame.Rect(self.width - 80, 15, 26, 13)
            self.draw_battery(ui_surface, icon_rect, batt_val)
            ui_surface.blit(batt_text, (icon_rect.right + 8, icon_rect.centery
                                        - batt_text.get_height() // 2))

        # 7. Letterbox bars: toasts / remote indicator above, focus tools below
        self._draw_top_bar(ui_surface)
        if self.focus_peaking_mode:
            self._draw_focus_toolbar(ui_surface)

        # 8. Debug Overlay (if enabled)
        if DEBUG_MODE and self.battery and self.battery.available:
            debug_lines = [
                f"Batt: {self.battery.percentage:.1f}% ({self.battery.voltage:.3f}V)",
                f"FPS: {self._fps_frames}"  # updated every 5s by preview loop
            ]
            y_offset = 50
            for line in debug_lines:
                debug_surf = self.small_font.render(line, True, YELLOW)
                ui_surface.blit(debug_surf, (10, y_offset))
                y_offset += 20

        return ui_surface

    def _dim(self, size):
        """Translucent veil over controls that cannot be used (cached per size)."""
        veil = self._dim_cache.get(size)
        if veil is None:
            veil = pygame.Surface(size, pygame.SRCALPHA)
            veil.fill((*SURFACE_0, 165))
            self._dim_cache[size] = veil
        return veil

    def _draw_no_camera(self, surface):
        """The viewfinder area when there is no camera: what is wrong and what to do."""
        if self.camera_absent:
            title = "Fotocamera non rilevata"
            hint = "Spegni il Pi, ricollega il cavo flat della fotocamera e riaccendi."
        else:
            title = "Fotocamera non avviata"
            hint = "Controlla il cavo flat e riavvia dal menu."
        note = "Menu, galleria e pagina web restano disponibili."
        pygame.draw.rect(surface, SURFACE_0, self.preview_rect)
        cx = self.preview_rect.centerx
        w = min(self.preview_rect.w - 32, 420)
        text_w = w - 40
        hint_lines = len(self._wrap(self.f_caption, hint, text_w))
        note_lines = len(self._wrap(self.f_caption, note, text_w))
        card = pygame.Rect(0, 0, w, 24 + 70 + 34 + 20 * hint_lines + 8 + 18 * note_lines + 20)
        card.center = self.preview_rect.center
        self.draw_card(surface, card, radius=18, fill=SURFACE_1, border=HAIRLINE)
        y = card.y + 24
        self._disc(surface, (cx, y + 28), 30, (*DANGER, 60))
        self._blit_icon(surface, "camera_off", (cx, y + 28), 32, DANGER)
        y += 70
        label = self._text(self.f_heading, title, TEXT_HI)
        surface.blit(label, label.get_rect(midtop=(cx, y)))
        y += 34
        y = self._blit_lines(surface, self.f_caption, hint, TEXT_MID, (cx, y), text_w,
                             line_h=20, center=True)
        self._blit_lines(surface, self.f_caption, note, TEXT_LOW, (cx, y + 8), text_w,
                         line_h=18, center=True)

    def _no_camera_surface(self):
        """Camera screen without a camera: re-rendered only when something on it changes."""
        toast = self._active_toast()
        battery = int(self.battery.percentage) if self.battery and self.battery.available else None
        sig = (toast[:2] if toast else None, battery, self.remote_active, self.camera_absent)
        if self._nocam_frame is None or self._nocam_frame[0] != sig:
            self._nocam_frame = (sig, pygame.transform.rotate(self.draw_camera_ui(), 180))
        return self._nocam_frame[1]

    def _check_camera_appeared(self, now):
        """Without a camera: look for one every few seconds, and start over when it shows up."""
        if now - self._last_camera_check < CAMERA_RECHECK:
            return
        self._last_camera_check = now
        ready = _camera_devices_ready()
        self.camera_absent = not ready
        # Not while a menu or the gallery is open: starting over closes them
        if not ready or self.overlay is not None or self.mode != "camera":
            return
        attempts = self._relaunch_count()
        if attempts >= MAX_RELAUNCH:
            return  # registered but it will not start: the screen says so
        try:
            with open(RELAUNCH_FILE, "w") as f:
                f.write(str(attempts + 1))
        except OSError:
            return
        print(f"✓ A camera is registered: starting over to pick it up ({attempts + 1}/{MAX_RELAUNCH})")
        self.relaunch = True
        self.running = False

    @staticmethod
    def _relaunch_count():
        try:
            with open(RELAUNCH_FILE) as f:
                return int(f.read())
        except (OSError, ValueError):
            return 0

    def _draw_stall_warning(self, surface):
        """Card over the preview while the sensor is not delivering frames."""
        title = self._text(self.small_bold_font, "Fotocamera non risponde", TEXT_HI)
        hint = self._text(self.micro_font, "Controlla il cavo flat", TEXT_MID)
        card = pygame.Rect(0, 0, max(title.get_width(), hint.get_width()) + 32,
                           title.get_height() + hint.get_height() + 22)
        card.center = self.preview_rect.center
        self.draw_card(surface, card, radius=12, fill=SURFACE_1, border=DANGER)
        surface.blit(title, title.get_rect(centerx=card.centerx, y=card.y + 8))
        surface.blit(hint, hint.get_rect(centerx=card.centerx,
                                         y=card.y + 12 + title.get_height()))

    def _draw_top_bar(self, surface):
        """Toast, or the remote indicator, in the letterbox bar above the image."""
        area = pygame.Rect(self.image_rect.x, self.preview_rect.y, self.image_rect.w,
                           self.image_rect.y - self.preview_rect.y)
        if area.h < 36:
            area = pygame.Rect(self.image_rect.x, self.image_rect.y, self.image_rect.w, 48)
        toast = self._active_toast()
        if toast:
            text, tone, until = toast
            pill = self._toast_surface(text, tone)
            # Fade out over the last 0.3 s
            pill.set_alpha(int(255 * min(1.0, (until - time.monotonic()) / 0.3)))
            surface.blit(pill, pill.get_rect(center=area.center))
        elif self.remote_active:
            chip = self._chip_surface("TELECOMANDO", "phone", ACCENT)
            surface.blit(chip, chip.get_rect(center=area.center))

    def _draw_focus_toolbar(self, surface):
        tb = self.toolbar
        key = (self.zoom_level, self._scaler_crop(), self.peak_color_index, self.peak_level_index)
        static = self._toolbar_cache.get(key)
        if static is None:
            static = self._render_toolbar(key)
            self._toolbar_cache.clear()
            self._toolbar_cache[key] = static
        surface.blit(static, tb["bar"].topleft)

        # Focus meter: bar relative to the best value seen lately. Turn the
        # ring until the bar reaches the marker.
        m = tb["meter"]
        track = pygame.Rect(m.x + 66, m.centery - 4, m.w - 80, 8)
        pygame.draw.rect(surface, SURFACE_3, track, border_radius=4)
        peak = self._meter_peak
        scale = max(peak * 1.1, 1.0)
        fill_w = int(track.w * min(1.0, self._meter_value / scale))
        near_peak = peak > 0 and self._meter_value >= 0.93 * peak
        if fill_w > 0:
            pygame.draw.rect(surface, ACCENT if near_peak else TEXT_MID,
                             (track.x, track.y, fill_w, track.h), border_radius=4)
        if peak > 0:
            px = track.x + int(track.w * min(1.0, peak / scale))
            pygame.draw.rect(surface, TEXT_HI, (px - 1, track.y - 6, 3, track.h + 12), border_radius=1)

    def _render_toolbar(self, key):
        """Static part of the focus toolbar (chips, labels, zoom map)."""
        tb = self.toolbar
        bar = tb["bar"]
        surf = pygame.Surface(bar.size, pygame.SRCALPHA)
        if self._toolbar_floating:
            pygame.draw.rect(surf, (0, 0, 0, 150), surf.get_rect())

        def local(r):
            return r.move(-bar.x, -bar.y)

        zoomed = self.zoom_level > 1
        # Zoom chip: level and a map of where the magnified window sits
        z = local(tb["zoom"])
        self.draw_card(surf, z, radius=12, border=ACCENT if zoomed else HAIRLINE)
        label = self._text(self.small_bold_font, f"×{self.zoom_level}", ACCENT if zoomed else TEXT_HI)
        surf.blit(label, label.get_rect(midleft=(z.x + 10, z.centery)))
        mm = pygame.Rect(0, 0, 28, 21)
        mm.midright = (z.right - 9, z.centery)
        pygame.draw.rect(surf, TEXT_LOW, mm, 1, border_radius=2)
        x, y, w, h = self._scaler_crop()
        sw, sh = SENSOR_SIZE
        win = pygame.Rect(mm.x + round(x / sw * mm.w), mm.y + round(y / sh * mm.h),
                          max(3, round(w / sw * mm.w)), max(3, round(h / sh * mm.h)))
        pygame.draw.rect(surf, ACCENT if zoomed else TEXT_LOW, win, 0 if zoomed else 1, border_radius=1)

        m = local(tb["meter"])
        self.draw_card(surf, m, radius=12)
        self.draw_label(surf, "FOCUS", (m.x + 34, m.centery))

        c = local(tb["color"])
        self.draw_card(surf, c, radius=12)
        color = PEAK_COLORS[self.peak_color_index][1]
        self._blit_icon(surf, "dot", c.center, 20, color)
        self._blit_icon(surf, "ring", c.center, 28, TEXT_HI)

        lv = local(tb["level"])
        self.draw_card(surf, lv, radius=12)
        base_x, base_y = lv.centerx - 14, lv.centery + 10
        for i in range(3):
            bh = 8 + 6 * i
            on = i <= self.peak_level_index
            pygame.draw.rect(surf, ACCENT if on else SURFACE_3,
                             (base_x + i * 11, base_y - bh, 7, bh), border_radius=2)
        return surf

    # ---------------- modern UI primitives ----------------

    def _make_sidebar_surface(self, edge):
        """Pre-render one sidebar: vertical gradient + hairline on the preview side."""
        w, h = self.sidebar_width, self.height
        surf = pygame.Surface((w, h))
        top, bottom = (18, 20, 25), SURFACE_0
        for y in range(h):
            t = y / max(1, h - 1)
            surf.fill(
                (
                    int(top[0] + (bottom[0] - top[0]) * t),
                    int(top[1] + (bottom[1] - top[1]) * t),
                    int(top[2] + (bottom[2] - top[2]) * t),
                ),
                (0, y, w, 1),
            )
        x = w - 1 if edge == "right" else 0
        pygame.draw.line(surf, HAIRLINE, (x, 0), (x, h), 1)
        return surf

    def _text(self, font, text, color):
        """Cached font.render for strings redrawn every frame.

        Bounded because the live AE readback produces a new string whenever the
        exposure drifts.
        """
        key = (id(font), text, color)
        surf = self._text_cache.get(key)
        if surf is None:
            if len(self._text_cache) > 128:
                self._text_cache.clear()
            surf = font.render(text, True, color)
            self._text_cache[key] = surf
        return surf

    def draw_card(self, surface, rect, radius=12, fill=SURFACE_2, border=HAIRLINE):
        """Rounded surface used to group related controls."""
        if fill is not None:
            pygame.draw.rect(surface, fill, rect, border_radius=radius)
        if border is not None:
            pygame.draw.rect(surface, border, rect, width=1, border_radius=radius)

    def draw_label(self, surface, text, center, color=TEXT_MID, tracking=1):
        """Uppercase micro-label with letter tracking, centred on `center`.

        Labels are static, so the tracked-out result is composed once and
        cached: rendering each glyph every frame cost ~0.5 ms of the 33 ms
        budget on a Pi Zero 2W.
        """
        key = (text, color, tracking)
        cached = self._label_cache.get(key)
        if cached is None:
            chars = [self.micro_font.render(c, True, color) for c in text]
            if not chars:
                return
            total = sum(c.get_width() for c in chars) + tracking * (len(chars) - 1)
            cached = pygame.Surface((total, chars[0].get_height()), pygame.SRCALPHA)
            x = 0
            for ch in chars:
                cached.blit(ch, (x, 0))
                x += ch.get_width() + tracking
            self._label_cache[key] = cached
        surface.blit(cached, cached.get_rect(center=center))

    def draw_arrow(self, surface, center, direction):
        """Chevron (two strokes) - lighter and more modern than a filled triangle."""
        x, y = center
        w, h = 9, 5
        thickness = 2
        if direction == "up":
            pygame.draw.lines(surface, TEXT_MID, False,
                              [(x - w, y + h), (x, y - h), (x + w, y + h)], thickness)
        else:
            pygame.draw.lines(surface, TEXT_MID, False,
                              [(x - w, y - h), (x, y + h), (x + w, y - h)], thickness)

    def draw_battery(self, surface, rect, percent, charging=False):
        """Battery drawn as a pill with a fill bar rather than a bare number."""
        body = pygame.Rect(rect.x, rect.y, rect.width - 3, rect.height)
        # Terminal nub on the right
        nub = pygame.Rect(body.right + 1, body.y + body.height // 3,
                          2, body.height - 2 * (body.height // 3))
        level = max(0, min(100, percent))
        color = SUCCESS if level > 40 else (ACCENT if level > 15 else DANGER)

        pygame.draw.rect(surface, HAIRLINE, body, width=1, border_radius=3)
        pygame.draw.rect(surface, TEXT_MID, nub, border_radius=1)
        inner_w = int((body.width - 4) * level / 100)
        if inner_w > 0:
            pygame.draw.rect(surface, color,
                             (body.x + 2, body.y + 2, inner_w, body.height - 4),
                             border_radius=2)

    def draw_icon(self, surface, name, center, size=30, color=WHITE, active=False):
        """Draw procedural icons for sharper look"""
        x, y = center

        if name == "power":
            # Power glyph: IEC standard ring-with-break plus vertical stroke,
            # sitting in a circular well so it reads as a pressable target.
            #
            # pygame.draw.arc renders a thick arc as several 1px-radius-offset
            # arcs that don't line up pixel-for-pixel, which shows up as
            # broken, uneven segments (worst right at the gap edges). Draw a
            # full ring with pygame.draw.circle instead -- it doesn't have
            # that bug -- and punch the top gap out with a mask wedge.
            radius = size // 2
            pygame.draw.circle(surface, SURFACE_2, center, radius + 4)
            pygame.draw.circle(surface, HAIRLINE, center, radius + 4, 1)
            r = radius - 4
            ring_w = 2
            pygame.draw.circle(surface, DANGER, center, r, ring_w)
            gap, top = math.radians(35), -math.pi / 2
            notch = [
                center,
                (x + math.cos(top - gap) * (r + ring_w), y + math.sin(top - gap) * (r + ring_w)),
                (x + math.cos(top + gap) * (r + ring_w), y + math.sin(top + gap) * (r + ring_w)),
            ]
            pygame.draw.polygon(surface, SURFACE_2, notch)
            pygame.draw.line(surface, DANGER, (x, y - r - 1), (x, y + 1), 2)

        elif name == "grid":
            # Gallery: stacked-photos glyph (back sheet + front sheet with peak)
            w = int(size * 0.62)
            back = pygame.Rect(x - w // 2 + 3, y - w // 2 - 3, w, w)
            front = pygame.Rect(x - w // 2 - 3, y - w // 2 + 3, w, w)
            pygame.draw.rect(surface, TEXT_LOW, back, 1, border_radius=3)
            pygame.draw.rect(surface, SURFACE_0, front, border_radius=3)
            pygame.draw.rect(surface, color, front, 1, border_radius=3)
            # Mountain + sun inside the front sheet
            pygame.draw.circle(surface, color, (front.x + 5, front.y + 5), 1)
            pygame.draw.lines(surface, color, False, [
                (front.x + 2, front.bottom - 3),
                (front.x + w // 2 - 1, front.y + w // 2),
                (front.right - 2, front.bottom - 3),
            ], 1)

        elif name == "bw":
            # Monochrome toggle: circle split into filled/outlined halves.
            col = ACCENT if active else TEXT_MID
            r = int(size * 0.36)
            pygame.draw.circle(surface, col, (x, y - 2), r, 1)
            # Fill the right half to signal the tonal split
            half = pygame.Surface((r, r * 2), pygame.SRCALPHA)
            pygame.draw.circle(half, col, (0, r), r)
            surface.blit(half, (x, y - 2 - r))
            self.draw_label(surface, "B/W", (x, y + r + 7),
                            color=ACCENT if active else TEXT_LOW)

        elif name == "fp":
            # Focus peaking: viewfinder corner brackets + centre dot.
            peak_col = ACCENT if active else TEXT_MID
            r = size // 2 - 2
            blen, bthk = 6, 2
            for sx in (-1, 1):
                for sy in (-1, 1):
                    cx_, cy_ = x + sx * r, y + sy * r - 2
                    pygame.draw.line(surface, peak_col, (cx_, cy_),
                                     (cx_ - sx * blen, cy_), bthk)
                    pygame.draw.line(surface, peak_col, (cx_, cy_),
                                     (cx_, cy_ - sy * blen), bthk)
            pygame.draw.circle(surface, peak_col, (x, y - 2), 2)
            self.draw_label(surface, "PEAK", (x, y + r + 7),
                            color=ACCENT if active else TEXT_LOW)

    # ---------------- menu primitives ----------------

    def _icon(self, name, size, color, level=4):
        """Anti-aliased vector icon: drawn 4x oversize, smooth-scaled down, cached."""
        key = (name, size, color, level)
        surf = self._icon_cache.get(key)
        if surf is None:
            big = pygame.Surface((size * 4, size * 4), pygame.SRCALPHA)
            _draw_icon_shape(big, name, size * 4, color, level)
            surf = pygame.transform.smoothscale(big, (size, size))
            self._icon_cache[key] = surf
        return surf

    def _blit_icon(self, surf, name, center, size, color, level=4):
        icon = self._icon(name, size, color, level)
        surf.blit(icon, icon.get_rect(center=(int(center[0]), int(center[1]))))

    def _rrect(self, surf, rect, color, radius, width=0):
        """Rounded rectangle; a colour with alpha is blended over what is below."""
        rect = pygame.Rect(rect)
        if len(color) == 4 and color[3] < 255:
            layer = pygame.Surface(rect.size, pygame.SRCALPHA)
            pygame.draw.rect(layer, color, layer.get_rect(), width, border_radius=radius)
            surf.blit(layer, rect.topleft)
        else:
            pygame.draw.rect(surf, color[:3], rect, width, border_radius=radius)

    def _glass(self, surf, rect, radius, active=False, strength=1.0):
        """Translucent panel over the frosted backdrop (amber-tinted when active)."""
        if active:
            fill, border = (*ACCENT, 46), (*ACCENT, 150)
        else:
            fill, border = (255, 255, 255, int(20 * strength)), (255, 255, 255, int(32 * strength))
        self._rrect(surf, rect, fill, radius)
        self._rrect(surf, rect, border, radius, width=1)

    def _disc(self, surf, center, r, color):
        x, y = int(center[0]), int(center[1])
        gfxdraw.filled_circle(surf, x, y, r, color)
        gfxdraw.aacircle(surf, x, y, r, color)

    def _spinner(self, surf, center, r, color, width=3):
        """Activity arc; the menus re-render ~10 times a second while one spins."""
        phase = int(time.monotonic() * 10) % 12 * 30
        size = 2 * r + width + 4
        layer = pygame.Surface((size * 2, size * 2), pygame.SRCALPHA)
        _arc_band(layer, color, (size, size), r * 2, width * 2, -phase, -phase + 110)
        layer = pygame.transform.smoothscale(layer, (size, size))
        surf.blit(layer, layer.get_rect(center=(int(center[0]), int(center[1]))))

    def _fit(self, font, text, max_w):
        """Text shortened with an ellipsis to fit max_w pixels."""
        if font.size(text)[0] <= max_w:
            return text
        while text and font.size(text + "…")[0] > max_w:
            text = text[:-1]
        return text + "…"

    def _wrap(self, font, text, max_w):
        lines, line = [], ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if font.size(trial)[0] <= max_w or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines

    def _blit_lines(self, surf, font, text, color, pos, max_w, line_h=None, center=False, max_lines=4):
        x, y = pos
        line_h = line_h or font.get_linesize()
        for line in self._wrap(font, text, max_w)[:max_lines]:
            s = font.render(line, True, color)
            surf.blit(s, s.get_rect(midtop=(x, y)) if center else (x, y))
            y += line_h
        return y

    def _chip_surface(self, label, icon, color):
        key = ("chip", label, icon, color)
        surf = self._text_cache.get(key)
        if surf is None:
            text = self.f_caption_b.render(label, True, color)
            w, h = text.get_width() + 46, 30
            surf = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.rect(surf, (*SURFACE_2, 235), surf.get_rect(), border_radius=h // 2)
            pygame.draw.rect(surf, (*color, 110), surf.get_rect(), 1, border_radius=h // 2)
            self._blit_icon(surf, icon, (20, h // 2), 18, color)
            surf.blit(text, (34, (h - text.get_height()) // 2))
            self._text_cache[key] = surf
        return surf

    def _toast_surface(self, text, tone):
        key = ("toast", text, tone)
        surf = self._text_cache.get(key)
        if surf is None:
            color = {"success": SUCCESS, "danger": DANGER, "accent": ACCENT}.get(tone, INFO)
            label = self.f_caption_b.render(text, True, TEXT_HI)
            w, h = label.get_width() + 50, 36
            surf = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.rect(surf, (*SURFACE_1, 242), surf.get_rect(), border_radius=h // 2)
            pygame.draw.rect(surf, (*HAIRLINE, 255), surf.get_rect(), 1, border_radius=h // 2)
            self._blit_icon(surf, "dot", (20, h // 2), 10, color)
            surf.blit(label, (34, (h - label.get_height()) // 2))
            self._text_cache[key] = surf
        return surf.copy()

    def _active_toast(self):
        if self._toast and self._toast[2] > time.monotonic():
            return self._toast
        return None

    def _frost(self, surf):
        """Blurred, darkened copy of a frame: the backdrop behind the menus."""
        w, h = surf.get_size()
        small = pygame.transform.smoothscale(surf, (max(1, w // 10), max(1, h // 10)))
        small = pygame.transform.smoothscale(small, (max(1, w // 20), max(1, h // 20)))
        out = pygame.transform.smoothscale(small, (w, h))
        out.fill((72, 76, 86), special_flags=pygame.BLEND_RGB_MULT)
        return out

    def _qr(self, text):
        matrix = self._qr_cache.get(("matrix", text))
        if matrix is None:
            matrix = self._qr_cache[("matrix", text)] = qr_matrix(text)
        return matrix

    def _qr_surface(self, text, max_size, tile_modules=None):
        """QR code on a white tile with a two-module quiet zone.

        Modules are whole pixels; tile_modules makes codes of different
        versions share one tile size (the smaller one is centred).
        """
        key = (text, max_size, tile_modules)
        surf = self._qr_cache.get(key)
        if surf is None:
            matrix = self._qr(text)
            total = max(tile_modules or 0, len(matrix)) + 4
            module = max(2, max_size // total)
            offset = (total - len(matrix)) // 2
            surf = pygame.Surface((total * module, total * module), pygame.SRCALPHA)
            pygame.draw.rect(surf, WHITE, surf.get_rect(), border_radius=module * 2)
            for r, row in enumerate(matrix):
                for c, dark in enumerate(row):
                    if dark:
                        surf.fill(BLACK, ((c + offset) * module, (r + offset) * module, module, module))
            self._qr_cache[key] = surf
        return surf

    # ---------------- overlays (menus) ----------------

    def _current_logical(self):
        if self.mode == "gallery" and self._gallery_logical is not None:
            return self._gallery_logical
        return self._ui_surface

    def _open_overlay(self, name):
        """Show a menu screen; the first one freezes a frosted copy of the view behind it."""
        if self.overlay is None:
            self._backdrop = self._frost(self._current_logical())
        self.overlay = name
        self._overlay_frame = None
        self._hits = []
        if name in ("menu", "wifi", "password"):
            self._start_net_monitor()
        if name == "wifi" and not self.wifi_networks:
            self.scan_wifi_networks()

    def _close_overlay(self):
        self.overlay = None
        self._confirm = None
        self._backdrop = None
        self._overlay_frame = None
        self._hits = []
        self._last_presented = None

    def _ask(self, title, body, icon, tone, ok_label, action):
        """Confirmation dialog; Cancel returns to the screen it was opened from."""
        self._confirm = {"title": title, "body": body, "icon": icon, "tone": tone,
                         "ok": ok_label, "action": action, "back": self.overlay}
        self._open_overlay("confirm")

    def _cancel_confirm(self):
        back = self._confirm["back"] if self._confirm else None
        self._confirm = None
        if back:
            self._open_overlay(back)
        else:
            self._close_overlay()

    def _hit(self, rect, action, radius=16):
        """Register a touch target for the overlay being drawn (radius None: no highlight)."""
        self._hits.append((pygame.Rect(rect), radius, action))

    def _overlay_signature(self):
        """Everything an overlay shows; it is re-rendered only when this changes."""
        now = time.monotonic()
        toast = self._active_toast()
        flash = self._flash if self._flash and self._flash[2] > now else None
        animating = bool(self._busy) or self.wifi_scanning
        sig = [self.overlay, id(self._confirm), tuple(sorted(self.net.items())),
               tuple(sorted(self._busy.items())), self.wifi_scanning, id(self.wifi_networks),
               self.wifi_page, toast[:2] if toast else None,
               (tuple(flash[0]), flash[1]) if flash else None,
               int(now * 10) if animating else None]
        if self.overlay == "password":
            sig += [self.password_input, self.password_visible, self.keyboard_shift,
                    self.keyboard_symbols, int(now * 2) % 2, now - self._last_key_time < 1.0]
        return tuple(sig)

    def _overlay_surface(self):
        sig = self._overlay_signature()
        if self._overlay_frame is None or self._overlay_frame[0] != sig:
            surf = self._backdrop.copy() if self._backdrop is not None else pygame.Surface((self.width, self.height))
            self._hits = []
            {"menu": self._render_menu, "wifi": self._render_wifi,
             "password": self._render_password, "confirm": self._render_confirm,
             "shutdown": self._render_shutdown}[self.overlay](surf)
            flash = self._flash
            if flash and flash[2] > time.monotonic() and flash[1] is not None:
                self._rrect(surf, flash[0], (255, 255, 255, 46), flash[1])
            toast = self._active_toast()
            if toast and self.overlay != "shutdown":
                pill = self._toast_surface(toast[0], toast[1])
                surf.blit(pill, pill.get_rect(center=(self.width // 2, self.height - 34)))
            self._overlay_frame = (sig, pygame.transform.rotate(surf, 180))
        return self._overlay_frame[1]

    def _touch_overlay(self, pos):
        for rect, radius, action in reversed(self._hits):
            if rect.collidepoint(pos):
                if radius is not None:
                    self._flash = (rect, radius, time.monotonic() + 0.18)
                action()
                return

    def _header(self, surf, title, subtitle, back=None, right=None):
        """Title block of the menu screens: optional back button, title, subtitle, right button."""
        x = 24
        if back:
            r = pygame.Rect(16, 16, 50, 50)
            self._round_button(surf, r, "back")
            self._hit(r.inflate(12, 12), back, r.w // 2 + 6)
            x = 78
        surf.blit(self._text(self.f_title, title, TEXT_HI), (x, 17))
        if subtitle:
            max_w = self.width - x - (84 if right else 24)
            surf.blit(self._text(self.f_caption, self._fit(self.f_caption, subtitle, max_w), TEXT_MID),
                      (x, 53))
        if right:
            icon, action, spinning = right
            r = pygame.Rect(self.width - 66, 16, 50, 50)
            self._round_button(surf, r, icon, spinning=spinning)
            self._hit(r.inflate(12, 12), action, r.w // 2 + 6)

    def _round_button(self, surf, r, icon, spinning=False, color=TEXT_HI):
        self._disc(surf, r.center, r.w // 2, (255, 255, 255, 26))
        gfxdraw.aacircle(surf, r.centerx, r.centery, r.w // 2, (255, 255, 255, 50))
        if spinning:
            self._spinner(surf, r.center, r.w // 2 - 13, ACCENT)
        else:
            self._blit_icon(surf, icon, r.center, 26, color)

    def _draw_tile(self, surf, r, label, icon, active, state, busy):
        """Quick-settings tile: icon disc, name, state."""
        self._glass(surf, r, 20, active=active)
        cx, cy = r.x + 38, r.y + 38
        self._disc(surf, (cx, cy), 23, ACCENT if active else SURFACE_3)
        if busy:
            self._spinner(surf, (cx, cy), 12, SURFACE_0 if active else ACCENT)
        else:
            self._blit_icon(surf, icon, (cx, cy), 28, SURFACE_0 if active else TEXT_HI)
        surf.blit(self._text(self.f_body_b, label, TEXT_HI), (r.x + 16, r.bottom - 50))
        state = self._fit(self.f_caption, state, r.w - 28)
        surf.blit(self._text(self.f_caption, state, ACCENT if active else TEXT_MID), (r.x + 16, r.bottom - 26))

    def _draw_action(self, surf, r, label, icon, tone):
        self._glass(surf, r, 18)
        cx, cy = r.x + 32, r.centery
        self._disc(surf, (cx, cy), 18, (*tone, 64))
        self._blit_icon(surf, icon, (cx, cy), 24, tone)
        text = self._text(self.f_body_b, label, TEXT_HI)
        surf.blit(text, text.get_rect(midleft=(r.x + 60, cy)))

    def _menu_subtitle(self):
        net = self.net
        if net.get("hotspot"):
            return f"{self.hostname} · hotspot {HOTSPOT_IP}"
        if net.get("ip"):
            return f"{self.hostname} · {net['ip']}"
        if net.get("server") is None:
            return self.hostname
        return f"{self.hostname} · nessuna rete"

    def _render_menu(self, surf):
        """System menu: toggles, power actions and how to reach the web remote."""
        W, H = self.width, self.height
        m, gap = 22, 14
        net = self.net
        self._header(surf, "Impostazioni", self._menu_subtitle(), right=("close", self._close_overlay, False))

        server, hotspot, wifi = net.get("server"), net.get("hotspot"), net.get("wifi")

        def state(on, on_text, off_text):
            return "…" if on is None else (on_text if on else off_text)

        ssid = (self._hotspot_credentials or (HOTSPOT_SSID, ""))[0]
        tiles = (
            ("Server web", "globe", bool(server), self._busy.get("server") or state(server, "Attivo", "Spento"),
             "server" in self._busy, self.toggle_server),
            ("Hotspot", "hotspot", bool(hotspot), self._busy.get("hotspot") or state(hotspot, ssid, "Spento"),
             "hotspot" in self._busy, self.toggle_hotspot),
            ("Wi-Fi", "wifi", bool(wifi), self._busy.get("wifi") or wifi or state(hotspot, "Non connessa", "Non connessa"),
             "wifi" in self._busy, lambda: self._open_overlay("wifi")),
        )
        top = 86
        th = max(96, int(H * 0.235))
        tw = (W - 2 * m - 2 * gap) // 3
        for i, (label, icon, active, text, busy, action) in enumerate(tiles):
            r = pygame.Rect(m + i * (tw + gap), top, tw, th)
            self._draw_tile(surf, r, label, icon, active, text, busy)
            self._hit(r, action, 20)

        top2 = top + th + gap
        bh = 58
        actions = (
            ("Standby", "moon", INFO, self._standby_from_menu),
            ("Riavvia", "reboot", ACCENT, lambda: self._ask(
                "Riavviare la fotocamera?", "Torna operativa in circa mezzo minuto.",
                "reboot", ACCENT, "Riavvia", self._reboot)),
            ("Spegni", "power", DANGER, lambda: self._ask(
                "Spegnere la fotocamera?",
                "Per riaccenderla scollega e ricollega l'alimentazione.",
                "power", DANGER, "Spegni", self._shutdown)),
        )
        for i, (label, icon, tone, action) in enumerate(actions):
            r = pygame.Rect(m + i * (tw + gap), top2, tw, bh)
            self._draw_action(surf, r, label, icon, tone)
            self._hit(r, action, 18)

        top3 = top2 + bh + gap
        card = pygame.Rect(m, top3, W - 2 * m, H - top3 - 16)
        if card.h >= 110:
            self._draw_connection_card(surf, card)

    def _draw_connection_card(self, surf, card):
        """How to open the web remote: URL and QR code (plus the hotspot's Wi-Fi QR)."""
        self._glass(surf, card, 20)
        net = self.net
        pad = 13
        tx = card.x + 22

        def message(icon, title, body):
            cx = card.x + 46
            self._disc(surf, (cx, card.centery), 24, (255, 255, 255, 22))
            self._blit_icon(surf, icon, (cx, card.centery), 28, TEXT_MID)
            x = cx + 40
            y = card.centery - 30
            surf.blit(self._text(self.f_body_b, title, TEXT_HI), (x, y))
            self._blit_lines(surf, self.f_caption, body, TEXT_MID, (x, y + 28), card.right - x - 18)

        if net.get("server") is None:
            return
        if not net.get("server"):
            message("globe", "Server web spento",
                    "Attivalo per usare la galleria e il telecomando dal telefono.")
            return
        ip = net.get("ip")
        if not ip:
            message("wifi", "Nessuna rete",
                    "Collegati a una rete Wi-Fi o attiva l'hotspot per usare il telecomando.")
            return

        url = f"http://{ip}:{SERVER_PORT}/live"
        qr_box = card.h - 2 * pad
        if net.get("hotspot"):
            ssid, password = self._hotspot_credentials or (HOTSPOT_SSID, HOTSPOT_PASSWORD)

            def esc(s):
                return "".join("\\" + ch if ch in '\\;,:"' else ch for ch in s)

            codes = ((f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;", "1 · RETE"), (url, "2 · PAGINA"))
            tile = max(len(self._qr(text)) for text, _ in codes)
            x = card.x + pad
            for text, caption in codes:
                q = self._qr_surface(text, qr_box - 18, tile)
                y = card.centery - (q.get_height() + 18) // 2
                surf.blit(q, (x, y))
                self.draw_label(surf, caption, (x + q.get_width() // 2, y + q.get_height() + 11),
                                color=TEXT_MID)
                x += q.get_width() + 14
            tx = x + 6
            y = card.centery - 64
            self.draw_label(surf, "HOTSPOT", (tx + self._label_width("HOTSPOT") // 2, y + 6), color=TEXT_LOW)
            surf.blit(self._text(self.f_body_b, self._fit(self.f_body_b, ssid, card.right - tx - 14), TEXT_HI),
                      (tx, y + 20))
            y = self._blit_lines(surf, self.f_caption, f"Password {password}", TEXT_MID, (tx, y + 46),
                                 card.right - tx - 14)
            self._blit_lines(surf, self.f_caption, f"Poi apri {ip}:{SERVER_PORT}/live", TEXT_MID,
                             (tx, y + 6), card.right - tx - 14)
            return

        q = self._qr_surface(url, qr_box)
        surf.blit(q, (card.x + pad, card.centery - q.get_height() // 2))
        tx = card.x + pad + q.get_width() + 20
        y = card.centery - 54
        self.draw_label(surf, "TELECOMANDO", (tx + self._label_width("TELECOMANDO") // 2, y + 6), color=TEXT_LOW)
        surf.blit(self._text(self.f_heading, f"{ip}:{SERVER_PORT}", TEXT_HI), (tx, y + 20))
        self._blit_lines(surf, self.f_caption,
                         "Inquadra il codice con il telefono: anteprima dal vivo, scatto e galleria.",
                         TEXT_MID, (tx, y + 56), card.right - tx - 16)

    def _label_width(self, text, tracking=1):
        return sum(self.micro_font.size(c)[0] for c in text) + tracking * (len(text) - 1)

    def _standby_from_menu(self):
        self._close_overlay()
        self.enter_standby()

    def _shutdown(self):
        print("Shutdown requested from the menu")
        self._shutdown_text = "Spegnimento…"
        self._open_overlay("shutdown")
        self._present(self._overlay_surface())
        subprocess.Popen(['sudo', 'shutdown', 'now'])

    def _reboot(self):
        print("Reboot requested from the menu")
        self._shutdown_text = "Riavvio…"
        self._open_overlay("shutdown")
        self._present(self._overlay_surface())
        subprocess.Popen(['sudo', 'reboot'])

    def _render_shutdown(self, surf):
        surf.fill(BLACK)
        cx, cy = self.width // 2, self.height // 2
        self._blit_icon(surf, "power", (cx, cy - 46), 56, TEXT_MID)
        title = self._text(self.f_heading, self._shutdown_text, TEXT_HI)
        surf.blit(title, title.get_rect(center=(cx, cy + 14)))
        self._blit_lines(surf, self.f_caption, "Attendi qualche secondo prima di scollegare l'alimentazione.",
                         TEXT_MID, (cx, cy + 40), self.width - 120, center=True)

    def _render_wifi(self, surf):
        W, H = self.width, self.height
        m = 22
        net = self.net
        connecting = self._connecting_ssid if "wifi" in self._busy else None
        if connecting:
            sub = f"Connessione a {connecting}…"
        elif net.get("hotspot"):
            sub = "Hotspot attivo: si spegne se ti colleghi a una rete"
        elif net.get("wifi"):
            sub = f"Connessa a {net['wifi']}"
        else:
            sub = "Scegli una rete"
        self._header(surf, "Wi-Fi", sub, back=lambda: self._open_overlay("menu"),
                     right=("refresh", self.scan_wifi_networks, self.wifi_scanning))

        nets = self.wifi_networks
        top = 86
        if not nets:
            card = pygame.Rect(m, top, W - 2 * m, 150)
            self._glass(surf, card, 20)
            if self.wifi_scanning:
                self._spinner(surf, (card.centerx, card.y + 52), 16, ACCENT)
                text = self._text(self.f_body, "Ricerca reti…", TEXT_MID)
                surf.blit(text, text.get_rect(center=(card.centerx, card.y + 104)))
            else:
                text = self._text(self.f_body_b, "Nessuna rete trovata", TEXT_HI)
                surf.blit(text, text.get_rect(center=(card.centerx, card.y + 56)))
                hint = self._text(self.f_caption, "Tocca il pulsante in alto a destra per cercare di nuovo", TEXT_MID)
                surf.blit(hint, hint.get_rect(center=(card.centerx, card.y + 88)))
            return

        row_h = 58
        max_rows = max(2, (H - top - 16) // row_h)
        per_page = max_rows if len(nets) <= max_rows else max_rows - 1
        pages = (len(nets) + per_page - 1) // per_page
        page = min(self.wifi_page, pages - 1)
        visible = nets[page * per_page:(page + 1) * per_page]
        card = pygame.Rect(m, top, W - 2 * m, row_h * len(visible))
        self._glass(surf, card, 18)
        connected = net.get("wifi")
        saved = self.wifi_saved
        for i, n in enumerate(visible):
            r = pygame.Rect(card.x, card.y + i * row_h, card.w, row_h)
            if i:
                self._rrect(surf, (r.x + 58, r.y, r.w - 58, 1), (255, 255, 255, 26), 0)
            level = max(1, min(4, 1 + n["signal"] // 25))
            self._blit_icon(surf, "wifi", (r.x + 30, r.centery), 28, TEXT_HI, level)
            is_conn = n["ssid"] == connected
            secured = n["security"] != "Open"
            name = self._fit(self.f_body_b, n["ssid"], r.w - 150)
            surf.blit(self._text(self.f_body_b, name, TEXT_HI), (r.x + 58, r.y + 8))
            if n["ssid"] == connecting:
                detail, color = "Connessione…", ACCENT
            elif is_conn:
                detail, color = "Connessa", ACCENT
            elif n["ssid"] in saved:
                detail, color = "Salvata", TEXT_MID
            else:
                detail, color = ("Protetta · " + n["security"].replace(" ", " / ")) if secured else "Aperta", TEXT_MID
            surf.blit(self._text(self.f_caption, detail, color), (r.x + 58, r.y + 33))
            if n["ssid"] == connecting:
                self._spinner(surf, (r.right - 30, r.centery), 10, ACCENT)
            elif is_conn:
                self._blit_icon(surf, "check", (r.right - 30, r.centery), 26, ACCENT)
            elif secured:
                self._blit_icon(surf, "lock", (r.right - 30, r.centery), 22, TEXT_LOW)
            self._hit(r, lambda n=n: self._select_network(n), 0 if 0 < i < len(visible) - 1 else 18)

        if pages > 1:
            bar = pygame.Rect(m, card.bottom + 10, W - 2 * m, 46)
            prev_r = pygame.Rect(bar.x, bar.y, 120, bar.h)
            next_r = pygame.Rect(bar.right - 120, bar.y, 120, bar.h)
            for r, icon, enabled, delta in ((prev_r, "up", page > 0, -1), (next_r, "down", page < pages - 1, 1)):
                self._glass(surf, r, 16, strength=1.0 if enabled else 0.4)
                self._blit_icon(surf, icon, r.center, 24, TEXT_HI if enabled else TEXT_LOW)
                if enabled:
                    self._hit(r, lambda d=delta: self._wifi_page(d), 16)
            text = self._text(self.f_caption, f"{page * per_page + 1}–{page * per_page + len(visible)} di {len(nets)}",
                              TEXT_MID)
            surf.blit(text, text.get_rect(center=bar.center))

    def _wifi_page(self, delta):
        self.wifi_page = max(0, self.wifi_page + delta)

    def _select_network(self, n):
        ssid = n["ssid"]
        if ssid == self.net.get("wifi") or "wifi" in self._busy:
            return
        print(f"Selected network: {ssid} ({n['security']})")
        if n["security"] == "Open" or ssid in self.wifi_saved:
            self._connecting_ssid = ssid
            self.connect_to_wifi(ssid)
        else:
            self.password_network = n
            self.password_input = ""
            self.password_visible = False
            self.keyboard_shift = 0
            self.keyboard_symbols = False
            self._open_overlay("password")

    # Keyboard rows: (key id, label, width in key units)
    def _keyboard_rows(self):
        bottom = [("MODE", "ABC" if self.keyboard_symbols else "?123", 1.6), (",", ",", 1),
                  ("SPACE", "spazio", 4), (".", ".", 1), ("OK", "Connetti", 2.4)]
        if self.keyboard_symbols:
            return [
                [(c, c, 1) for c in "1234567890"],
                [(c, c, 1) for c in "!@#$%^&*()"],
                [(c, c, 1) for c in "-_=+[]{}\\|~"],
                [(c, c, 1) for c in ";:'\"`<>/?"] + [("BACK", "", 1.5)],
                bottom,
            ]

        def letters(s):
            return [((c.upper() if self.keyboard_shift else c),) * 2 + (1,) for c in s]

        return [
            [(c, c, 1) for c in "1234567890"],
            letters("qwertyuiop"),
            letters("asdfghjkl"),
            [("SHIFT", "", 1.5)] + letters("zxcvbnm") + [("BACK", "", 1.5)],
            bottom,
        ]

    def _keyboard_layout(self, top, bottom):
        """Key rects: plain rows keep a common key width (centred), rows with wide keys span the width."""
        W = self.width
        margin, gap = 10, 6
        avail = W - 2 * margin
        rows = self._keyboard_rows()
        key_h = (bottom - top - (len(rows) - 1) * gap) // len(rows)
        base_u = (avail - 9 * gap) / 10
        out = []
        for ri, row in enumerate(rows):
            y = top + ri * (key_h + gap)
            units = sum(k[2] for k in row)
            plain = all(k[2] == 1 for k in row)
            if plain and units <= 10:
                u = base_u
            else:
                u = (avail - (len(row) - 1) * gap) / units
            width = u * units + gap * (len(row) - 1)
            x = margin + (avail - width) / 2
            for kid, label, kw in row:
                w = u * kw
                out.append((kid, label, pygame.Rect(round(x), y, round(w), key_h)))
                x += w + gap
        return out

    def _render_password(self, surf):
        W, H = self.width, self.height
        net = self.password_network or {"ssid": ""}
        busy = "wifi" in self._busy
        self._header(surf, "Password", net["ssid"], back=lambda: self._open_overlay("wifi"))

        field = pygame.Rect(22, 82, W - 44 - 62, 52)
        self._rrect(surf, field, (0, 0, 0, 90), 14)
        self._rrect(surf, field, (*ACCENT, 170), 14, width=1)
        eye = pygame.Rect(field.right + 10, field.y, 52, 52)
        self._glass(surf, eye, 14)
        self._blit_icon(surf, "eye_off" if self.password_visible else "eye", eye.center, 28, TEXT_HI)
        self._hit(eye, self._toggle_password_visibility, 14)

        pw = self.password_input
        x, cy = field.x + 16, field.centery
        inner_w = field.w - 40
        if not pw:
            hint = self._text(self.f_body, "Inserisci la password", TEXT_LOW)
            surf.blit(hint, hint.get_rect(midleft=(x + 6, cy)))
        elif self.password_visible:
            shown = pw
            while self.f_body_b.size(shown)[0] > inner_w:
                shown = shown[1:]
            text = self.f_body_b.render(shown, True, TEXT_HI)
            surf.blit(text, text.get_rect(midleft=(x, cy)))
            x += text.get_width() + 3
        else:
            # Dots, with the last character readable for a second after typing it
            reveal = time.monotonic() - self._last_key_time < 1.0
            last = self.f_body_b.render(pw[-1], True, TEXT_HI) if reveal else None
            slots = (inner_w - (last.get_width() + 6 if last else 0)) // 16
            dots = len(pw) - (1 if last else 0)
            for _ in range(min(dots, slots)):
                self._disc(surf, (x + 6, cy), 5, TEXT_HI)
                x += 16
            if last:
                surf.blit(last, last.get_rect(midleft=(x, cy)))
                x += last.get_width() + 3
        if int(time.monotonic() * 2) % 2 == 0:
            pygame.draw.rect(surf, ACCENT, (x + 2, cy - 13, 2, 26))

        keys = self._keyboard_layout(150, H - 10)
        for kid, label, r in keys:
            if kid == "OK":
                enabled = bool(pw) and not busy
                self._rrect(surf, r, ACCENT if enabled else (*ACCENT, 90), 12)
                if busy:
                    self._spinner(surf, r.center, 11, SURFACE_0)
                else:
                    text = self._text(self.f_body_b, label, SURFACE_0)
                    surf.blit(text, text.get_rect(center=r.center))
            else:
                special = kid in ("SHIFT", "BACK", "MODE")
                on = kid == "SHIFT" and self.keyboard_shift
                self._rrect(surf, r, (255, 255, 255, 64 if on else 13 if special else 28), 12)
                if kid == "SHIFT":
                    self._blit_icon(surf, "shift", r.center, 26, TEXT_HI, self.keyboard_shift)
                elif kid == "BACK":
                    self._blit_icon(surf, "backspace", r.center, 28, TEXT_HI)
                elif kid in ("MODE", "SPACE"):
                    text = self._text(self.f_caption_b if kid == "MODE" else self.f_caption, label,
                                      TEXT_HI if kid == "MODE" else TEXT_MID)
                    surf.blit(text, text.get_rect(center=r.center))
                else:
                    text = self._text(self.f_key, label, TEXT_HI)
                    surf.blit(text, text.get_rect(center=r.center))
            self._hit(r, lambda kid=kid: self._key(kid), 12)

    def _toggle_password_visibility(self):
        self.password_visible = not self.password_visible

    def _key(self, kid):
        now = time.monotonic()
        if kid == "BACK":
            self.password_input = self.password_input[:-1]
        elif kid == "SHIFT":
            # Tap: next letter upper case; double tap: caps lock
            if self.keyboard_shift == 1 and now - self._last_shift_tap < 0.45:
                self.keyboard_shift = 2
            else:
                self.keyboard_shift = 0 if self.keyboard_shift else 1
            self._last_shift_tap = now
        elif kid == "MODE":
            self.keyboard_symbols = not self.keyboard_symbols
        elif kid == "OK":
            self._submit_password()
        elif len(self.password_input) < 63:
            self.password_input += " " if kid == "SPACE" else kid
            self._last_key_time = now
            if self.keyboard_shift == 1 and kid.isalpha():
                self.keyboard_shift = 0

    def _submit_password(self):
        net = self.password_network
        pw = self.password_input
        if not net or not pw or "wifi" in self._busy:
            return
        if "WPA" in net["security"] and len(pw) < 8:
            self._show_toast("La password Wi-Fi ha almeno 8 caratteri", "danger")
            return
        print(f"Connecting to {net['ssid']} with password...")
        self._connecting_ssid = net["ssid"]
        self.connect_to_wifi(net["ssid"], pw)

    def _render_confirm(self, surf):
        c = self._confirm
        W, H = self.width, self.height
        # Tapping outside the dialog cancels it
        self._hit(pygame.Rect(0, 0, W, H), self._cancel_confirm, None)
        panel = pygame.Rect(0, 0, min(468, W - 40), 252)
        panel.center = (W // 2, H // 2)
        for i in range(1, 9):
            self._rrect(surf, panel.inflate(i * 3, i * 3).move(0, 6), (0, 0, 0, 14), 26 + i)
        self._rrect(surf, panel, (*SURFACE_1, 248), 26)
        self._rrect(surf, panel, (255, 255, 255, 36), 26, width=1)
        self._hit(panel, lambda: None, None)
        tone = c["tone"]
        cx = panel.centerx
        self._disc(surf, (cx, panel.y + 50), 30, (*tone, 60))
        self._blit_icon(surf, c["icon"], (cx, panel.y + 50), 32, tone)
        title = self._text(self.f_heading, c["title"], TEXT_HI)
        surf.blit(title, title.get_rect(center=(cx, panel.y + 104)))
        self._blit_lines(surf, self.f_caption, c["body"], TEXT_MID, (cx, panel.y + 126), panel.w - 64,
                         center=True, max_lines=2)
        bw = (panel.w - 3 * 18) // 2
        cancel = pygame.Rect(panel.x + 18, panel.bottom - 72, bw, 54)
        ok = pygame.Rect(cancel.right + 18, cancel.y, bw, 54)
        self._glass(surf, cancel, 16, strength=1.4)
        text = self._text(self.f_body_b, "Annulla", TEXT_HI)
        surf.blit(text, text.get_rect(center=cancel.center))
        self._rrect(surf, ok, tone, 16)
        text = self._text(self.f_body_b, c["ok"], WHITE if tone == DANGER else SURFACE_0)
        surf.blit(text, text.get_rect(center=ok.center))
        self._hit(cancel, self._cancel_confirm, 16)
        self._hit(ok, self._run_confirm, 16)

    def _run_confirm(self):
        action = self._confirm["action"]
        back = self._confirm["back"]
        self._confirm = None
        if back:
            self._open_overlay(back)
        else:
            self._close_overlay()
        action()

    def _saving_frame(self):
        """The last viewfinder frame, dimmed, with a "saving" badge (shown during a capture)."""
        surf = self._ui_surface.copy()
        surf.fill((120, 120, 120), special_flags=pygame.BLEND_RGB_MULT)
        badge = self._chip_surface("Salvataggio…", "camera", TEXT_HI)
        surf.blit(badge, badge.get_rect(center=self.image_rect.center))
        return surf

    # ---------------- gallery ----------------

    def _photo_caption(self, path, exif):
        """'16 ott 2026, 18:26 · 1/15 s · ISO 813' from EXIF, falling back to the file name."""
        months = ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic")
        parts = []
        sub = exif.get_ifd(0x8769) if exif else {}
        stamp = sub.get(36867) or (exif.get(306) if exif else None)
        try:
            dt = datetime.strptime(str(stamp), "%Y:%m:%d %H:%M:%S")
        except (TypeError, ValueError):
            try:
                dt = datetime.strptime(path.stem[-15:], "%Y%m%d_%H%M%S")
            except ValueError:
                dt = datetime.fromtimestamp(path.stat().st_mtime)
        parts.append(f"{dt.day} {months[dt.month - 1]} {dt.year}, {dt:%H:%M}")
        exposure = sub.get(33434)
        if exposure:
            exposure = float(exposure)
            parts.append(f"1/{round(1 / exposure)} s" if exposure < 0.5 else f"{exposure:.1f} s")
        iso = sub.get(34855)
        if iso:
            parts.append(f"ISO {iso}")
        return " · ".join(parts)

    def _decode_photo(self, path):
        """Decode a photo at screen size: JPEG draft mode decodes at 1/4 scale directly.

        The old gallery decoded the full 12 MP file and resized it with Lanczos
        on every frame, i.e. a second or more per frame on a Pi Zero 2W.
        """
        Image = _pil_image()
        with Image.open(path) as im:
            exif = im.getexif()
            im.draft("RGB", (self.width, self.height))
            img = im.convert("RGB")
        img.thumbnail((self.width, self.height), Image.Resampling.BILINEAR)
        return {"pixels": img.tobytes(), "size": img.size, "caption": self._photo_caption(path, exif),
                "surface": None}

    def _gallery_entry(self, path):
        with self._gallery_lock:
            entry = self._gallery_cache.get(path)
        if entry is None:
            try:
                entry = self._decode_photo(path)
            except Exception as e:
                print(f"Gallery load error: {e}")
                return None
            with self._gallery_lock:
                self._gallery_cache[path] = entry
        if entry["surface"] is None:
            entry["surface"] = pygame.image.frombuffer(entry["pixels"], entry["size"], "RGB")
        return entry

    def _prefetch_gallery(self):
        """Decode the neighbours in the background so swiping is instant."""
        wanted = []
        for d in (1, -1, 2):
            if self.photos:
                wanted.append(self.photos[(self.gallery_index + d) % len(self.photos)])
        current = self.photos[self.gallery_index] if self.photos else None
        with self._gallery_lock:
            keep = set(wanted) | {current}
            for path in [p for p in self._gallery_cache if p not in keep]:
                if len(self._gallery_cache) <= 5:
                    break
                del self._gallery_cache[path]
            todo = [p for p in wanted if p not in self._gallery_cache and p not in self._gallery_loading]
            self._gallery_loading.update(todo)

        def load(paths):
            for path in paths:
                try:
                    entry = self._decode_photo(path)
                    with self._gallery_lock:
                        self._gallery_cache[path] = entry
                except Exception as e:
                    print(f"Gallery prefetch error: {e}")
                finally:
                    with self._gallery_lock:
                        self._gallery_loading.discard(path)

        if todo:
            threading.Thread(target=load, args=(todo,), daemon=True).start()

    def _gallery_scrims(self):
        cached = self._scrims
        if cached is None:
            top = pygame.Surface((self.width, 90), pygame.SRCALPHA)
            for i in range(90):
                pygame.draw.line(top, (0, 0, 0, int(170 * (1 - i / 90) ** 1.6)), (0, i), (self.width, i))
            bottom = pygame.transform.flip(top, False, True)
            cached = self._scrims = (top, bottom)
        return cached

    def draw_gallery_ui(self):
        """Gallery frame, drawn the right way up (photo plus controls over it)."""
        W, H = self.width, self.height
        surf = pygame.Surface((W, H))
        surf.fill(SURFACE_0)
        entry = None
        if not self.photos:
            self._blit_icon(surf, "camera", (W // 2, H // 2 - 52), 64, TEXT_LOW)
            text = self._text(self.f_heading, "Nessuna foto", TEXT_MID)
            surf.blit(text, text.get_rect(center=(W // 2, H // 2 + 6)))
            hint = self._text(self.f_caption, "Scatta con il pulsante per iniziare", TEXT_LOW)
            surf.blit(hint, hint.get_rect(center=(W // 2, H // 2 + 36)))
        else:
            self.gallery_index %= len(self.photos)
            entry = self._gallery_entry(self.photos[self.gallery_index])
            if entry:
                surf.blit(entry["surface"], entry["surface"].get_rect(center=(W // 2, H // 2)))
            else:
                text = self._text(self.f_body, "Impossibile aprire la foto", DANGER)
                surf.blit(text, text.get_rect(center=(W // 2, H // 2)))
            self._prefetch_gallery()

        top, bottom = self._gallery_scrims()
        surf.blit(top, (0, 0))
        surf.blit(bottom, (0, H - bottom.get_height()))

        for rect, icon, color in ((self.back_btn_rect, "back", TEXT_HI),
                                  (self.trash_btn_rect, "trash", (255, 128, 128))):
            if icon == "trash" and not self.photos:
                continue
            self._disc(surf, rect.center, rect.w // 2, (0, 0, 0, 120))
            gfxdraw.aacircle(surf, rect.centerx, rect.centery, rect.w // 2, (255, 255, 255, 60))
            self._blit_icon(surf, icon, rect.center, 28, color)

        if self.photos:
            counter = self._text(self.f_caption_b, f"{self.gallery_index + 1} / {len(self.photos)}", TEXT_HI)
            pill = counter.get_rect(center=(W // 2, self.back_btn_rect.centery)).inflate(26, 14)
            self._rrect(surf, pill, (0, 0, 0, 120), pill.h // 2)
            surf.blit(counter, counter.get_rect(center=pill.center))
            if len(self.photos) > 1:
                self._blit_icon(surf, "back", (22, H // 2), 30, (255, 255, 255, 150))
                self._blit_icon(surf, "next", (W - 22, H // 2), 30, (255, 255, 255, 150))
        if entry:
            caption = self._text(self.f_caption, entry["caption"], TEXT_HI)
            pill = caption.get_rect(center=(W // 2, H - 30)).inflate(28, 14)
            self._rrect(surf, pill, (0, 0, 0, 130), pill.h // 2)
            surf.blit(caption, caption.get_rect(center=pill.center))

        toast = self._active_toast()
        if toast:
            pill = self._toast_surface(toast[0], toast[1])
            surf.blit(pill, pill.get_rect(center=(W // 2, H - 78)))
        self._gallery_logical = surf
        return surf

    def _gallery_surface(self):
        path = self.photos[self.gallery_index % len(self.photos)] if self.photos else None
        with self._gallery_lock:
            loaded = path in self._gallery_cache
        toast = self._active_toast()
        sig = (path, loaded, len(self.photos), toast[:2] if toast else None)
        if self._gallery_frame is None or self._gallery_frame[0] != sig:
            self._gallery_frame = (sig, pygame.transform.rotate(self.draw_gallery_ui(), 180))
        return self._gallery_frame[1]

    def _confirm_delete(self):
        self.delete_photo()
        self._gallery_frame = None

    # ---------------- input ----------------

    def calibrate_touch(self, raw_x, raw_y):
        """Calibra coordinate touch grezze a coordinate schermo"""
        cal = self.touch_cal

        # Swap XY se necessario
        if cal['swap_xy']:
            raw_x, raw_y = raw_y, raw_x

        # Normalizza 0-1
        x_norm = (raw_x - cal['x_min']) / (cal['x_max'] - cal['x_min'])
        y_norm = (raw_y - cal['y_min']) / (cal['y_max'] - cal['y_min'])

        # Inverti se necessario
        if cal['invert_x']:
            x_norm = 1.0 - x_norm
        if cal['invert_y']:
            y_norm = 1.0 - y_norm

        # Clamp 0-1
        x_norm = max(0, min(1, x_norm))
        y_norm = max(0, min(1, y_norm))

        # Scala a risoluzione schermo
        screen_x = int(x_norm * self.width)
        screen_y = int(y_norm * self.height)

        return screen_x, screen_y

    def _on_tap(self, pos):
        """Any touch wakes the camera from standby; otherwise it is a tap on the UI."""
        if self.standby_mode:
            self.wake_from_standby()
        else:
            self.handle_touch(pos)

    def handle_touch(self, pos):
        """Handle touch/click events"""
        # Con display ruotato di 180, dobbiamo invertire X e Y
        # per mappare il tocco visuale sul sistema di coordinate logico
        vx, vy = pos
        x = self.width - vx
        y = self.height - vy
        pos = (x, y)

        # Debug Log
        print(f"DEBUG TOUCH: visual=({vx}, {vy}) logical=({x}, {y})")

        if self.overlay is not None:
            self._touch_overlay(pos)
        elif self.mode == "camera":
            self._touch_camera(pos)
        elif self.mode == "gallery":
            self._touch_gallery(pos)

    def _touch_camera(self, pos):
        x, y = pos
        if self.camera is None and not (self.power_rect.collidepoint(x, y)
                                        or self.gallery_rect.collidepoint(x, y)):
            return  # nothing but the menu and the gallery works without a camera
        # Left Sidebar Interaction
        if x < self.sidebar_width:
            # SHUTTER (Top)
            if self.shutter_up_rect.collidepoint(x, y):
                self.current_shutter_index = (self.current_shutter_index + 1) % len(SHUTTER_SPEEDS)
                self.apply_camera_settings()
            elif self.shutter_down_rect.collidepoint(x, y):
                self.current_shutter_index = (self.current_shutter_index - 1) % len(SHUTTER_SPEEDS)
                self.apply_camera_settings()
            # ISO (Bottom)
            elif self.iso_up_rect.collidepoint(x, y):
                self.current_iso_index = (self.current_iso_index + 1) % len(ISO_VALUES)
                self.apply_camera_settings()
            elif self.iso_down_rect.collidepoint(x, y):
                self.current_iso_index = (self.current_iso_index - 1) % len(ISO_VALUES)
                self.apply_camera_settings()
            return

        # Right Sidebar Interaction
        if x > self.width - self.sidebar_width:
            if self.power_rect.collidepoint(x, y):
                print("Power button pressed!")
                self._open_overlay("menu")
            elif self.gallery_rect.collidepoint(x, y):
                self.mode = "gallery"
                self.load_photos()
                self.gallery_index = 0  # newest first
                self._gallery_frame = None
            elif self.bw_rect.collidepoint(x, y):
                self.grayscale_mode = not self.grayscale_mode
                print(f"B/W mode: {self.grayscale_mode}")
                self.apply_camera_settings()
            elif self.fp_rect.collidepoint(x, y):
                self.set_focus_assist(not self.focus_peaking_mode)
            return

        # The preview itself only reacts in focus-assist mode (a tap used to
        # capture, and triggered by accident with the camera in hand).
        if not self.focus_peaking_mode:
            return
        tb = self.toolbar
        for name in ("zoom", "meter", "color", "level"):
            r = tb[name]
            # The whole height of the bar counts, not just the chip
            if pygame.Rect(r.x, tb["bar"].y, r.w, tb["bar"].h).collidepoint(x, y):
                if name == "zoom":
                    self._cycle_zoom()
                elif name == "meter":
                    self._meter_peak = self._meter_value  # restart the peak hold
                elif name == "color":
                    self.peak_color_index = (self.peak_color_index + 1) % len(PEAK_COLORS)
                    self._show_toast(f"Peaking {PEAK_COLORS[self.peak_color_index][0].lower()}", "accent", 1.2)
                elif name == "level":
                    self.peak_level_index = (self.peak_level_index + 1) % len(PEAK_LEVELS)
                    self._show_toast(f"Sensibilità {PEAK_LEVELS[self.peak_level_index][0].lower()}",
                                     "accent", 1.2)
                return
        img = self.image_rect
        if img.collidepoint(x, y) and not (self._toolbar_floating and tb["bar"].collidepoint(x, y)):
            # Magnify around the tapped point (or move the magnified window there)
            self._zoom_at((x - img.x) / img.w, (y - img.y) / img.h)

    def _touch_gallery(self, pos):
        x, y = pos
        # Back Button
        if self.back_btn_rect.inflate(20, 20).collidepoint(pos):
            self.mode = "camera"
            self._last_presented = None
            return
        # Trash Button
        if self.photos and self.trash_btn_rect.inflate(20, 20).collidepoint(pos):
            name = self.photos[self.gallery_index].name
            self._ask("Eliminare la foto?", f"{name} verrà cancellata dalla scheda.",
                      "trash", DANGER, "Elimina", self._confirm_delete)
            return
        # Navigate
        if not self.photos:
            return
        if x < self.width // 3:
            self.gallery_index = (self.gallery_index - 1) % len(self.photos)
        elif x > self.width * 2 // 3:
            self.gallery_index = (self.gallery_index + 1) % len(self.photos)

    def _process_input(self):
        # Process touch queue from evdev
        try:
            while True:
                event_type, x, y = self.touch_queue.get_nowait()
                if event_type == 'click':
                    self._on_tap((x, y))
        except queue.Empty:
            pass

        # Events (fallback se evdev non disponibile)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                # Use mouse events always
                print(f"DEBUG MOUSE: pos={event.pos}")
                self._on_tap(event.pos)

    # ---------------- main loop ----------------

    def _compose(self):
        """The next frame to show (already rotated for the panel)."""
        if self.overlay is not None:
            if self.mode == "camera":
                self._feed_remote_only()
            return self._overlay_surface()
        if self.mode == "gallery":
            return self._gallery_surface()
        if self.camera is None:
            return self._no_camera_surface()
        self.update_preview()
        return pygame.transform.rotate(self.draw_camera_ui(), 180)

    def run(self):
        """Main loop"""
        clock = pygame.time.Clock()

        # Display and UI are ready; block here for whatever is left of the
        # camera bring-up that did not overlap with them. The margin above
        # CAMERA_INIT_TIMEOUT covers the picamera2 import (several seconds
        # from a cold SD card) and Picamera2()/camera.start() overhead on top
        # of the retry loop's own deadline.
        if not self._camera_ready.wait(timeout=CAMERA_INIT_TIMEOUT + 50):
            print("Camera did not become ready in time")
        if self.camera is None:
            # Not fatal. Returning here ended the process and systemd started it
            # again every ~17 s: a boot loop. The interface stays up instead and
            # says what is wrong; the menu, the gallery and the web page work,
            # and _check_camera_appeared() starts over if a camera shows up.
            self.camera_absent = not _camera_devices_ready()
            print(f"⚠ No camera ({self.camera_error}): "
                  f"{'no sensor is registered' if self.camera_absent else 'it did not start'}; "
                  "the interface stays up")
        else:
            print(f"✓ Ready in {time.monotonic() - _T0:.2f}s")
            try:
                os.remove(RELAUNCH_FILE)
            except OSError:
                pass

        try:
            while self.running:
                # Shutter button: wakes the camera when it is asleep
                if self.capture_pending:
                    self.capture_pending = False
                    if self.standby_mode:
                        self.wake_from_standby()
                    elif self.overlay != "shutdown":
                        self.capture_photo()

                self._process_input()
                if self._overlay_request:
                    if self.overlay in ("wifi", "password"):
                        self._open_overlay(self._overlay_request)
                    self._overlay_request = None

                # Process UDP commands
                self.udp_check()

                # Update Battery every 30 seconds
                current_time = time.monotonic()
                if self.battery and current_time - self.last_battery_update > 30:
                    self.battery.update()
                    self.last_battery_update = current_time

                # Sync remote status timeout
                if self.remote_active and current_time - self.remote_last_heartbeat > REMOTE_TIMEOUT:
                    self.remote_active = False
                    print("Remote disconnected")

                # Standby: nothing to draw (the output is off); poll input at 10 Hz
                if self.standby_mode:
                    if current_time - self._last_status_write > 1.0:
                        self._write_status()
                    clock.tick(10)
                    continue

                if self.camera is None:
                    self._check_camera_appeared(current_time)
                    if not self.running:
                        continue

                frame = self._compose()
                # Menus and the gallery return the same surface while nothing
                # changes; the framebuffer already shows it.
                if frame is not self._last_presented:
                    self._present(frame)

                if not self._first_frame.is_set() and self.current_preview_surface is not None:
                    self._first_frame.set()
                    # The number that matters for boot time: power-on to a
                    # live viewfinder is roughly the firmware stage plus this.
                    print(f"✓ First frame on screen {time.monotonic() - _T0:.2f}s "
                          f"after start, {_uptime():.1f}s after kernel start")

                if current_time - self._last_status_write > STATUS_INTERVAL:
                    self._write_status()

                # In the viewfinder the camera paces the loop (capture_request
                # waits for the next frame). A 30 fps tick on top of that
                # sometimes slept past a frame's arrival, and the loop then
                # waited for the following one: skipped frames, extra lag.
                if (self.overlay is not None or self.mode != "camera" or self.camera is None
                        or self.camera_stalled_since is not None):
                    clock.tick(30)

        finally:
            self.cleanup()

    def cleanup(self):
        """Cleanup"""
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        print("Shutting down...")
        self.running = False
        if self.standby_mode:
            # Do not leave the panel dark and the CPU slowed down for the next start
            _write_sysfs(self._standby_restore)
            self._clear_standby_state()
            self._set_display_power(True)
        for worker in (self.peaker, self.remote_writer):
            if worker is not None:
                worker.stop()
        if self.camera is not None:
            try:
                # Pending requests would make stop() wait on a stalled sensor.
                self.camera.cancel_all_and_flush()
                self.camera.stop()
            except:
                pass
        if self.fb is not None:
            self.fb.close()
        pygame.quit()


def main():
    """Entry point"""
    app = None
    # systemctl stop/restart sends SIGTERM, which would otherwise end the
    # process without cleanup(): the camera left running, and after standby
    # the display left off.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        app = CameraApp()
        app.run()
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if app:
            app.cleanup()
    if app is not None and app.relaunch:
        print("Starting over")
        try:
            # Same command line as this process (python3 -c "..." under systemd)
            os.execv(sys.executable, [sys.executable] + sys.orig_argv[1:])
        except OSError as e:
            print(f"Restart failed ({e}); the service manager will start the app again")


if __name__ == "__main__":
    main()
