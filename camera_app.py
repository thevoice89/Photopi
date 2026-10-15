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
import queue
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


class CameraApp:
    def __init__(self):
        """Initialize the camera application"""
        PHOTOS_DIR.mkdir(exist_ok=True)

        # Camera config - Preview ottimizzato per BOOT VELOCE
        self.photo_width = 4056
        self.photo_height = 3040
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
        # Per 480x320, usiamo scale 1.2 per ingrandire tutto
        if self.width <= 640:
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
        self.photo_cache = {}
        self.thumbnail_queue = queue.Queue()
        self.loading_paths = set()
        self.last_capture_time = 0
        self.show_power_popup = False
        self.show_wifi_popup = False  # NEW: WiFi manager popup
        self.show_password_popup = False  # NEW: Password input popup
        self.password_input = ""  # Current password being typed
        self.password_network = None  # Network waiting for password
        self.keyboard_shift = False  # Shift key state for uppercase
        self.wifi_networks = []  # Available networks
        self.wifi_scroll_offset = 0  # For scrolling network list
        self.saved_brightness = None
        self.standby_mode = False
        self.hotspot_active = False
        self.remote_active = False
        self.remote_last_heartbeat = 0
        self.shm_sync_counter = 0
        self.last_shm_sync = 0
        self.grayscale_mode = False
        self.focus_peaking_mode = False  # Focus peaking overlay
        self.live_exposure_us = None     # Live AE readback (microseconds)
        self.live_gain = None            # Live AE readback (linear gain)
        
        # Memoized systemd/NetworkManager probes -> {key: (value, monotonic)}
        self._status_cache = {}
        # Rendered-glyph caches for HUD text redrawn every frame
        self._label_cache = {}
        self._text_cache = {}

        # Preview surface cache & perf optimizations
        self.current_preview_surface = None
        self.last_preview_time = 0
        # Pre-allocated surface (set in setup_ui after preview_rect is known)
        self._preview_surf = None          # reusable pygame.Surface for the preview area
        self._preview_scaled_size = None   # (new_w, new_h) pre-calculated at setup time
        self._preview_offset = None        # (x_off, y_off) pre-calculated at setup time
        # Async metadata: updated by a background thread to avoid blocking preview
        self._meta_lock = threading.Lock()
        self._meta_thread_running = False
        # FPS debug counter
        self._fps_frames = 0
        self._fps_last_t = time.monotonic()
        
        # Prepare UDP listener
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Bind to all interfaces to be sure
            self.sock.bind(('0.0.0.0', UDP_PORT))
            self.sock.setblocking(False)
            print(f"UDP listener bound to 0.0.0.0:{UDP_PORT}")
        except Exception as e:
            print(f"UDP Bind Error: {e}")
        
        # Icons are only drawn by the gallery, so load_icons() runs on first use.
        self.icons = None

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
        """
        return camera.create_video_configuration(
            main={"size": (self.preview_width, self.preview_height), "format": "RGB888"},
            sensor={"output_size": (2028, 1520)},
            buffer_count=2
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
                        camera.set_controls({"ScalerCrop": (0, 0, 4056, 3040)})
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

    def _present(self):
        """Show the composed frame: framebuffer copy, or SDL flip as a fallback."""
        if self.fb is not None:
            self.fb.show(self.screen)
        else:
            pygame.display.flip()

    def _lazy_init(self):
        """Initialize non-critical components in background for faster boot"""
        # None of this is usable before run() enters its loop, which happens
        # only once the camera is up; importing evdev/gpiozero earlier (~2 s on
        # a cold boot) just competes with picamera2, and then with the first
        # frame, for the SD card and the GIL. The second wait is bounded so a
        # camera that never delivers a frame does not leave touch dead.
        self._camera_ready.wait(timeout=CAMERA_INIT_TIMEOUT + 50)
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

        # Start async metadata reader (updates live ISO/shutter without blocking preview)
        if not self._meta_thread_running:
            self._meta_thread_running = True
            threading.Thread(target=self._meta_reader_thread, daemon=True).start()
            print("✓ Async metadata reader started")

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

    def _meta_reader_thread(self):
        """Background thread: reads camera metadata every ~0.4 s.
        This avoids blocking the preview loop with capture_metadata()."""
        # Background init can outrun the camera bring-up; don't spin on None.
        self._camera_ready.wait()
        if self.camera is None:
            return
        while self._meta_thread_running and self.running:
            # While the sensor is stalled the main loop owns recovery; a
            # metadata request would only queue up behind it.
            if self.camera_stalled_since is None:
                try:
                    meta = self.camera.capture_metadata(wait=FRAME_TIMEOUT)
                    exp  = meta.get("ExposureTime", None)
                    gain = meta.get("AnalogueGain", None)
                    with self._meta_lock:
                        self.live_exposure_us = exp
                        self.live_gain        = gain
                except Exception:
                    pass
            time.sleep(0.4)

    def load_icons(self):
        """Load UI icons from assets folder.

        Decoded with PIL (already loaded by picamera2): pygame's own PNG
        loader lives in pygame.imageext, which is not imported (see top of file).
        """
        self.icons = {}
        assets_path = Path(__file__).parent / "assets"
        
        icon_files = {
            "power": "icon_power.png",
            "gallery": "icon_gallery.png",
            "iso": "icon_iso.png",
            "shutter": "icon_shutter.png",
            "back": "icon_back.png",
            "arrow_left": "icon_arrow_left.png",
            "iso": "icon_iso.png",
            "shutter": "icon_shutter.png",
            "back": "icon_back.png",
            "arrow_left": "icon_arrow_left.png",
            "arrow_right": "icon_arrow_right.png",
            "bw": "icon_bw.png" # Optional icon for B/W
        }
        
        for name, filename in icon_files.items():
            path = assets_path / filename
            if path.exists():
                try:
                    with _pil_image().open(path) as src:
                        rgba = src.convert("RGBA")
                    img = pygame.image.frombuffer(rgba.tobytes(), rgba.size,
                                                  "RGBA").convert_alpha()
                    # Use smaller fixed icon sizes to prevent stretching
                    size = 24  # Smaller fixed size for minimal look
                    scaled = pygame.transform.smoothscale(img, (size, size))
                    # Make icons white for better visibility (preserves alpha)
                    scaled.fill((255, 255, 255), special_flags=pygame.BLEND_RGB_ADD)
                    self.icons[name] = scaled
                except Exception as e:
                    print(f"Error loading icon {name}: {e}")
            else:
                print(f"Icon not found: {path}")

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

    def udp_check(self):
        """Check for UDP commands (non-blocking)"""
        while True:
            try:
                data, addr = self.sock.recvfrom(1024)
                cmd = data.decode('utf-8').strip()
                print(f"UDP Command: {cmd}")
                
                if cmd == "START_REMOTE":
                    self.remote_active = True
                    self.remote_last_heartbeat = time.monotonic()
                elif cmd == "STOP_REMOTE":
                    self.remote_active = False
                elif cmd == "HEARTBEAT":
                    self.remote_last_heartbeat = time.monotonic()
                elif cmd == "CAPTURE":
                    self.capture_pending = True
                elif cmd == "ISO_UP":
                    self.current_iso_index = (self.current_iso_index + 1) % len(ISO_VALUES)
                    self.apply_camera_settings()
                elif cmd == "ISO_DOWN":
                    self.current_iso_index = (self.current_iso_index - 1) % len(ISO_VALUES)
                    self.apply_camera_settings()
                elif cmd == "SHUTTER_UP":
                    self.current_shutter_index = (self.current_shutter_index + 1) % len(SHUTTER_SPEEDS)
                    self.apply_camera_settings()
                elif cmd == "SHUTTER_DOWN":
                    self.current_shutter_index = (self.current_shutter_index - 1) % len(SHUTTER_SPEEDS)
                    self.apply_camera_settings()
                elif cmd == "BW":
                    # Placeholder for BW command action
                    pass
            except BlockingIOError:
                break
            except Exception as e:
                print(f"UDP Error: {e}")
                break
        
    def setup_ui(self):
        """Setup UI - SIDEBARS LAYOUT"""
        # Define sidebar width
        self.sidebar_width = int(70 * self.scale)
        
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
        
        # Popup power menu
        popup_width = int(self.width * 0.90)  # Wider popup (90%)
        popup_height = int(self.height * 0.90) # Taller popup (90%)
        popup_x = (self.width - popup_width) // 2
        popup_y = (self.height - popup_height) // 2
        self.popup_rect = pygame.Rect(popup_x, popup_y, popup_width, popup_height)
        
        # Custom sizes for MAXIMIZED buttons
        btn_width = int(popup_width * 0.45)
        btn_height = int(68 * self.scale) # Optimized Height (68px) to fit text
        btn_spacing_x = int(15 * self.scale)
        btn_spacing_y = int(8 * self.scale) # Minimal vertical spacing
        
        # Grid layout for popup buttons (now 3 rows)
        start_x = popup_x + (popup_width - (2 * btn_width + btn_spacing_x)) // 2
        start_y = popup_y + 35 # Reduced top margin to fit huge buttons
        
        # Popup power menu - Row 1
        self.popup_shutdown_btn = pygame.Rect(start_x, start_y, btn_width, btn_height)
        self.popup_standby_btn = pygame.Rect(start_x + btn_width + btn_spacing_x, start_y, btn_width, btn_height)
        
        # Row 2
        self.popup_server_btn = pygame.Rect(start_x, start_y + btn_height + btn_spacing_y, btn_width, btn_height)
        self.popup_hotspot_btn = pygame.Rect(start_x + btn_width + btn_spacing_x, start_y + btn_height + btn_spacing_y, btn_width, btn_height)
        
        # Row 3 - WiFi Manager
        self.popup_wifi_btn = pygame.Rect(start_x, start_y + 2 * (btn_height + btn_spacing_y), btn_width, btn_height)
        
        # Cancel button at bottom
        self.popup_cancel_btn = pygame.Rect(popup_x + (popup_width - btn_width) // 2, 
                                             popup_y + popup_height - int(45 * self.scale) - 10, btn_width, int(40 * self.scale))
        
        # Back button per gallery
        self.back_btn_rect = pygame.Rect(10, 10, 80, 60)
        
        # Trash button per gallery (Top Right)
        self.trash_btn_rect = pygame.Rect(self.width - 90, 10, 80, 60)

    def _cached_status(self, key, probe, ttl=3.0):
        """Memoize a subprocess-backed status probe.

        draw_power_popup() runs every frame while the menu is open, so probing
        systemd and NetworkManager directly meant spawning ~60 processes per
        second on a Pi Zero.
        """
        now = time.monotonic()
        value, stamp = self._status_cache.get(key, (None, 0.0))
        if value is None or now - stamp > ttl:
            value = probe()
            self._status_cache[key] = (value, now)
        return value

    def _invalidate_status(self, key):
        self._status_cache.pop(key, None)

    def check_server_status(self):
        """Check if photo server is running"""
        def probe():
            try:
                status = subprocess.run(['systemctl', 'is-active', 'photo-server'],
                                       capture_output=True, text=True, timeout=3).stdout.strip()
                return status == 'active'
            except:
                return False
        return self._cached_status('server', probe)

    def toggle_server(self):
        """Toggle photo server"""
        if self.check_server_status():
            subprocess.run(['sudo', 'systemctl', 'stop', 'photo-server'])
        else:
            subprocess.run(['sudo', 'systemctl', 'start', 'photo-server'])
        self._invalidate_status('server')

    def check_hotspot_status(self):
        """Check if hotspot is active"""
        def probe():
            try:
                status = subprocess.run(['nmcli', '-t', '-f', 'ACTIVE,NAME', 'con', 'show'],
                                       capture_output=True, text=True, timeout=3).stdout
                return "yes:RaspiCam_Hotspot" in status
            except:
                return False
        return self._cached_status('hotspot', probe)

    def toggle_hotspot(self):
        """Toggle hotspot"""
        if self.check_hotspot_status():
            subprocess.run(['sudo', 'nmcli', 'con', 'down', 'RaspiCam_Hotspot'])
        else:
            subprocess.run(['sudo', 'nmcli', 'con', 'up', 'RaspiCam_Hotspot'])
        self._invalidate_status('hotspot')


    def scan_wifi_networks(self):
        """Scan for available WiFi networks"""
        try:
            # Rescan networks
            subprocess.run(['sudo', 'nmcli', 'device', 'wifi', 'rescan'], 
                          capture_output=True, timeout=5)
            time.sleep(1)  # Wait for scan to complete
            
            # Get list of networks
            result = subprocess.run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'],
                                   capture_output=True, text=True, timeout=5)
            
            networks = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    parts = line.split(':')
                    if len(parts) >= 3 and parts[0]:  # Skip empty SSIDs
                        ssid = parts[0]
                        signal = int(parts[1]) if parts[1].isdigit() else 0
                        security = parts[2] if parts[2] else 'Open'
                        networks.append({'ssid': ssid, 'signal': signal, 'security': security})
            
            # Sort by signal strength
            networks.sort(key=lambda x: x['signal'], reverse=True)
            self.wifi_networks = networks[:10]  # Keep top 10
            print(f"Found {len(self.wifi_networks)} WiFi networks")
            
        except Exception as e:
            print(f"WiFi scan error: {e}")
            self.wifi_networks = []
    
    def connect_to_wifi(self, ssid, password=None):
        """Connect to a WiFi network"""
        try:
            if password:
                # Connect with password
                result = subprocess.run(['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid, 
                                       'password', password],
                                      capture_output=True, text=True, timeout=15)
            else:
                # Connect to open network
                result = subprocess.run(['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid],
                                      capture_output=True, text=True, timeout=15)
            
            if result.returncode == 0:
                print(f"✓ Connected to {ssid}")
                return True
                print(f"Connection failed: {result.stderr}")
                return False
                
        except Exception as e:
            print(f"WiFi connect error: {e}")
            return False

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
            if str(photo_path) in self.photo_cache:
                del self.photo_cache[str(photo_path)]
            
            # Adjust index
            if self.gallery_index >= len(self.photos):
                self.gallery_index = len(self.photos) - 1
            if self.gallery_index < 0:
                self.gallery_index = 0
                
        except Exception as e:
            print(f"Error deleting photo: {e}")

    def enter_standby(self):
        """Enter real low-power standby by writing to /sys/power/state.
        Tries 'mem' (S3 sleep) first, then 'freeze' (lighter suspend).
        The process blocks until the Pi wakes; wake_from_standby() is then
        called automatically."""
        print("Entering standby (suspend)...")
        self.standby_mode = True

        try:
            # 0. Turn off heavy services
            if self.check_server_status():
                print("Stopping photo server...")
                subprocess.run(['sudo', 'systemctl', 'stop', 'photo-server'])
            if self.check_hotspot_status():
                print("Stopping hotspot...")
                subprocess.run(['sudo', 'nmcli', 'con', 'down', 'RaspiCam_Hotspot'])

            # 1. Stop camera to release hardware
            try:
                self.camera.stop()
                print("✓ Camera stopped")
            except Exception as e:
                print(f"Camera stop warning: {e}")

            # 2. Turn off LCD backlight
            subprocess.run(['sudo', 'sh', '-c', 'echo 0 > /sys/class/backlight/*/brightness'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 1 > /sys/class/backlight/*/bl_power'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 18 > /sys/class/gpio/export'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo out > /sys/class/gpio/gpio18/direction'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 0 > /sys/class/gpio/gpio18/value'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # For HDMI/DSI displays
            subprocess.run(['sudo', 'vcgencmd', 'display_power', '0'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LCD backlight off")

            # 3. Blank framebuffer
            subprocess.run(['sudo', 'sh', '-c', 'echo 1 > /sys/class/graphics/fb1/blank'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 1 > /sys/class/graphics/fb0/blank'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # 4. Black screen and short pause
            self.screen.fill((0, 0, 0))
            self._present()
            time.sleep(0.3)

            # 5. Write to /sys/power/state — this BLOCKS until the Pi wakes up.
            #    Try 'mem' (S3/S2 real sleep) first; fallback to 'freeze'.
            suspended = False
            for sleep_state in ('mem', 'freeze'):
                print(f"💤 Trying suspend state: {sleep_state}")
                result = subprocess.run(
                    ['sudo', 'sh', '-c', f'echo {sleep_state} > /sys/power/state'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
                if result.returncode == 0:
                    # We reach here only AFTER the Pi has woken up
                    print(f"☀ Resumed from '{sleep_state}' suspend")
                    suspended = True
                    break
                else:
                    err = result.stderr.decode().strip()
                    print(f"'{sleep_state}' not supported: {err}")

            if not suspended:
                # Hardware doesn't support any sleep state — stay in display-off mode
                print("⚠ Suspend not supported: staying in display-off standby")
                subprocess.run(['sudo', 'sh', '-c', 'cpufreq-set -g powersave'],
                              check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # standby_mode stays True; wake on next touch
                return

            self.wake_from_standby()

        except Exception as e:
            print(f"Standby error: {e}")


    def wake_from_standby(self):
        """Restore display and camera after waking from standby."""
        if not self.standby_mode:
            return

        print("Waking from standby...")

        try:
            # Restore LCD backlight
            subprocess.run(['sudo', 'sh', '-c', 'echo 255 > /sys/class/backlight/*/brightness'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 0 > /sys/class/backlight/*/bl_power'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 1 > /sys/class/gpio/gpio18/value'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 18 > /sys/class/gpio/unexport'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # For HDMI/DSI displays
            subprocess.run(['sudo', 'vcgencmd', 'display_power', '1'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LCD backlight on")

            # Unblank framebuffer
            subprocess.run(['sudo', 'sh', '-c', 'echo 0 > /sys/class/graphics/fb1/blank'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c', 'echo 0 > /sys/class/graphics/fb0/blank'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Restore CPU governor
            subprocess.run(['sudo', 'cpufreq-set', '-g', 'ondemand'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Re-enable LED
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 1 > /sys/class/leds/led0/brightness'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # Restart camera
            self.camera.configure(self._preview_config(self.camera))
            self.camera.start()
            try:
                self.camera.set_controls({"ScalerCrop": (0, 0, 4056, 3040)})
            except Exception:
                pass
            print("✓ Camera restarted")

            self.standby_mode = False
            self.saved_brightness = None
            print("✓ Wake complete")

        except Exception as e:
            print(f"Wake error: {e}")
            try:
                self.camera.start()
            except Exception:
                pass
            self.standby_mode = False
            self.saved_brightness = None


    def apply_camera_settings(self):
        """Apply ISO and shutter settings"""
        iso = ISO_VALUES[self.current_iso_index]
        shutter_name, shutter_us = SHUTTER_SPEEDS[self.current_shutter_index]
        
        if iso == "Auto":
            self.camera.set_controls({"AeEnable": True})
        else:
            self.camera.set_controls({"AeEnable": False})
            self.camera.set_controls({"AnalogueGain": iso / 100.0})
        
        if shutter_us > 0:
            requested_fps = 1000000.0 / shutter_us
            if requested_fps < 30.0:
                self.camera.set_controls({"FrameRate": (0.1, requested_fps)})
            else:
                self.camera.set_controls({"FrameRate": (30.0, 30.0)})
            self.camera.set_controls({"ExposureTime": shutter_us})
        else:
            self.camera.set_controls({"FrameRate": (30.0, 30.0)})
            # We don't disable Ae unless iso is manual, so handled above
            
        # Apply B/W effect
        try:
            if self.grayscale_mode:
                self.camera.set_controls({"Saturation": 0.0})
            else:
                self.camera.set_controls({"Saturation": 1.0})
        except Exception as e:
            print(f"Warning: Could not set Saturation: {e}")
            
        # Anti-flicker 50Hz
        try:
            self.camera.set_controls({"AePowerLineFrequency": 1})
        except Exception as e:
            print(f"Warning: Could not set AePowerLineFrequency: {e}")
            
    def capture_photo(self):
        """Capture high resolution photo"""
        current_time = time.time()
        if current_time - self.last_capture_time < BUTTON_DEBOUNCE:
            return
        self.last_capture_time = current_time
        
        if self.camera_stalled_since is not None:
            print("Capture skipped: the camera is not delivering frames")
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = PHOTOS_DIR / f"photo_{timestamp}.jpg"
        
        try:
            print(f"Capturing to {filename}...")
            
            # Feedback visivo
            self.screen.fill(WHITE)
            self._present()
            time.sleep(0.1)
            
            # Stop preview
            self.camera.stop()
            
            # Config alta risoluzione
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
            
            # Restore preview
            self.camera.stop()
            self.camera.configure(self._preview_config(self.camera))
            self.camera.start()
            try:
                self.camera.set_controls({"ScalerCrop": (0, 0, 4056, 3040)})
            except Exception:
                pass

            self.load_photos()
            
        except Exception as e:
            print(f"Capture error: {e}")
            import traceback
            traceback.print_exc()
            
            try:
                # A timed-out capture job is still queued, and stop() would
                # wait behind it for a frame that never comes.
                self.camera.cancel_all_and_flush()
                self.camera.stop()
                self.camera.configure(self._preview_config(self.camera))
                self.camera.start()
                try:
                    self.camera.set_controls({"ScalerCrop": (0, 0, 4056, 3040)})
                except Exception:
                    pass
            except:
                pass

    def load_photos(self):
        """Load photo list"""
        self.photos = sorted(PHOTOS_DIR.glob("*.jpg"), reverse=True)
        self.photo_cache.clear()
        self.loading_paths.clear()

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
            frame = self.camera.capture_array("main", wait=wait)
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
            self.camera.set_controls({"ScalerCrop": (0, 0, 4056, 3040)})
            print("Camera restarted")
        except Exception as e:
            print(f"Camera restart failed: {e}")

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

            # ── 2. BGR→RGB in un'unica copia contigua ──
            # frame[:, :, ::-1] da sola crea una vista non-contigua (stride negativo)
            # che rallenta PIL e frombuffer; ascontiguousarray forza una copia contigua.
            frame_rgb = np.ascontiguousarray(frame[:, :, ::-1])

            h, w = frame_rgb.shape[:2]
            new_w, new_h = self._preview_scaled_size
            x_off, y_off  = self._preview_offset

            # ── 3. Surface dal buffer numpy senza tobytes() (buffer protocol) ──
            src_surf = pygame.image.frombuffer(frame_rgb, (w, h), 'RGB')

            # ── 4. Scale C-level (SDL) alle dimensioni pre-calcolate ──
            scaled_surf = pygame.transform.scale(src_surf, (new_w, new_h))

            # ── 5. Blit sulla surface pre-allocata (zero nuove allocazioni) ──
            self._preview_surf.blit(scaled_surf, (x_off, y_off))

            # ── 6. Focus Peaking Overlay (solo se attivo) ──
            if self.focus_peaking_mode:
                try:
                    scaled_arr = pygame.surfarray.array3d(scaled_surf).transpose(1, 0, 2)
                    gray = (0.299 * scaled_arr[:, :, 0] +
                            0.587 * scaled_arr[:, :, 1] +
                            0.114 * scaled_arr[:, :, 2]).astype(np.float32)
                    lap = np.abs(
                        np.roll(gray, 1, axis=0) + np.roll(gray, -1, axis=0) +
                        np.roll(gray, 1, axis=1) + np.roll(gray, -1, axis=1) -
                        4 * gray
                    )
                    threshold = max(float(np.percentile(lap, 97)), 18.0)
                    mask = lap > threshold
                    mask_rgba = np.zeros((new_h, new_w, 4), dtype=np.uint8)
                    mask_rgba[mask, 0] = 255
                    mask_rgba[mask, 1] = 220
                    mask_rgba[mask, 3] = 230
                    overlay_img = pygame.image.frombuffer(
                        mask_rgba.tobytes(), (new_w, new_h), 'RGBA'
                    )
                    self._preview_surf.blit(overlay_img, (x_off, y_off))
                except Exception as e:
                    print(f"Focus peaking error: {e}")

            # ── 7. Pubblica la surface per draw_camera_ui ──
            self.current_preview_surface = self._preview_surf

            # ── 7. Shared memory per remote preview (max 5 FPS) ──
            current_t = time.monotonic()
            if current_t - self.last_shm_sync > 0.2:
                self.last_shm_sync = current_t
                try:
                    img = _pil_image().fromarray(frame_rgb)
                    temp_path = f"{SHARED_MEM_PREVIEW}.tmp"
                    img.save(temp_path, "JPEG", quality=75)
                    os.replace(temp_path, SHARED_MEM_PREVIEW)

                    status = {
                        "iso":     str(ISO_VALUES[self.current_iso_index]),
                        "shutter": SHUTTER_SPEEDS[self.current_shutter_index][0],
                        "mode":    "remote" if self.remote_active else "local",
                        "status":  "active"
                    }
                    temp_status = f"{SHARED_MEM_STATUS}.tmp"
                    with open(temp_status, 'w') as f:
                        json.dump(status, f)
                    os.replace(temp_status, SHARED_MEM_STATUS)
                except Exception as e:
                    print(f"SHM Sync Error: {e}")

        except Exception as e:
            print(f"Update Preview Error: {e}")

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
        """Disegna UI camera - FULL PREVIEW + TRASPARENZA"""
        # Surface riusata per l'intera UI: allocarne una nuova a ogni frame
        # costava una malloc + clear di ~600 KB 30 volte al secondo.
        ui_surface = self._ui_surface
        ui_surface.fill(BLACK)

        # 1. Preview (sfondo)
        if self.current_preview_surface:
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
        
        # POWER (Top)
        self.draw_icon(ui_surface, "power", self.power_center)
        
        # GALLERY / GRID (Middle)
        self.draw_icon(ui_surface, "grid", self.gallery_center)
        
        # B/W (Bottom)
        self.draw_icon(ui_surface, "bw", self.bw_center, active=self.grayscale_mode)
        
        # FOCUS PEAKING (Bottom)
        self.draw_icon(ui_surface, "fp", self.fp_center, active=self.focus_peaking_mode)
        
        # 7. Power popup
        if self.show_power_popup:
            # Draw popup directly on screen later or include it here
            pass

        # 8. Battery Indicator (Top Right)
        # Fix: User sees it at bottom-left visually now with (15, self.height-35)
        # If it's bottom-left visually, and we want top-right visually:
        # We move it diagonally.
        if self.battery and self.battery.available:
            batt_val = int(self.battery.percentage)
            # Pill icon + numeral, right-aligned in the top-right corner.
            batt_text = self._text(self.small_bold_font, f"{batt_val}%", TEXT_HI)
            icon_rect = pygame.Rect(self.width - 80, 15, 26, 13)
            self.draw_battery(ui_surface, icon_rect, batt_val)
            ui_surface.blit(batt_text, (icon_rect.right + 8, icon_rect.centery
                                        - batt_text.get_height() // 2))

        # 9. Debug Overlay (if enabled)
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
        
        # 10. ROTAZIONE 180
        rotated_surface = pygame.transform.rotate(ui_surface, 180)
        self.screen.blit(rotated_surface, (0, 0))
        
        # Draw popup over everything else if active
        if self.show_power_popup:
            self.draw_power_popup()
        elif self.show_wifi_popup:
            self.draw_wifi_popup()
        elif self.show_password_popup:
            self.draw_password_popup()

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

    def _begin_popup(self, title=None, title_font=None):
        """Scrim + rounded panel shared by every popup. Returns the surface."""
        popup_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)

        overlay = pygame.Surface((self.width, self.height))
        overlay.set_alpha(215)
        overlay.fill((6, 7, 9))
        popup_surface.blit(overlay, (0, 0))

        # Panel: single hairline border instead of the old heavy 2px gray one.
        pygame.draw.rect(popup_surface, SURFACE_1, self.popup_rect, border_radius=18)
        pygame.draw.rect(popup_surface, HAIRLINE, self.popup_rect, width=1, border_radius=18)

        if title:
            # No divider rule: the button grid and the WiFi back button are
            # positioned by setup_ui and start high enough that a rule under the
            # title collides with them.
            font = title_font or self.small_bold_font
            title_surf = font.render(title, True, TEXT_HI)
            popup_surface.blit(
                title_surf,
                title_surf.get_rect(centerx=self.popup_rect.centerx,
                                    y=self.popup_rect.y + 14))
        return popup_surface

    def _end_popup(self, popup_surface):
        """Rotate 180 and present, matching the rest of the UI."""
        self.screen.blit(pygame.transform.rotate(popup_surface, 180), (0, 0))

    def draw_power_popup(self):
        """Power menu popup - RUOTATO SE SERVE"""
        popup_surface = self._begin_popup("MENU SISTEMA")

        # Buttons - now 2 rows (removed LCD Off)
        self.draw_button_on_surface(popup_surface, self.popup_shutdown_btn, "Spegni Pi", DANGER)

        standby_color = ACCENT if self.standby_mode else INFO
        standby_text = "Risveglia" if self.standby_mode else "Standby"
        self.draw_button_on_surface(popup_surface, self.popup_standby_btn, standby_text,
                                    standby_color,
                                    text_color=SURFACE_0 if self.standby_mode else WHITE)

        server_active = self.check_server_status()
        server_color = SUCCESS if server_active else SURFACE_2
        server_text = "Server: ON" if server_active else "Server: OFF"
        self.draw_button_on_surface(popup_surface, self.popup_server_btn, server_text,
                                    server_color,
                                    text_color=WHITE if server_active else TEXT_MID)

        hotspot_active = self.check_hotspot_status()
        hotspot_color = SUCCESS if hotspot_active else SURFACE_2
        hotspot_text = "Hotspot: ON" if hotspot_active else "Hotspot: OFF"
        self.draw_button_on_surface(popup_surface, self.popup_hotspot_btn, hotspot_text,
                                    hotspot_color,
                                    text_color=WHITE if hotspot_active else TEXT_MID)

        # WiFi Manager button (Row 3)
        self.draw_button_on_surface(popup_surface, self.popup_wifi_btn, "WiFi Manager", INFO)

        # Show IP info in self.popup_rect area below buttons
        # Now buttons occupy 3 rows, so adjust info display area
        info_y_start = self.popup_rect.y + int(250 * self.scale)
        
        if server_active and not hotspot_active:
            # Get actual IP address for local network
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('10.255.255.255', 1))
                ip_address = s.getsockname()[0]
                s.close()
                if ip_address and not ip_address.startswith("127."):
                    server_info = self.micro_font.render(f"Server: http://{ip_address}:8080", True, TEXT_MID)
                    popup_surface.blit(server_info, (self.popup_rect.centerx - server_info.get_width()//2, info_y_start))
                    info_y_start += 25
            except:
                pass

        if hotspot_active:
            info_h = self.micro_font.render("SSID: RaspiCam | Pass: raspicam_admin", True, TEXT_MID)
            info_ip = self.micro_font.render("Hotspot: http://10.42.0.1:8080", True, TEXT_MID)
            popup_surface.blit(info_h, (self.popup_rect.centerx - info_h.get_width()//2, info_y_start))
            popup_surface.blit(info_ip, (self.popup_rect.centerx - info_ip.get_width()//2, info_y_start + 25))

        self.draw_button_on_surface(popup_surface, self.popup_cancel_btn, "Chiudi",
                                    SURFACE_2, text_color=TEXT_MID)

        self._end_popup(popup_surface)

    def draw_wifi_popup(self):
        """WiFi selection popup"""
        popup_surface = self._begin_popup("RETI WIFI")

        # Back button
        back_btn = pygame.Rect(self.popup_rect.x + 10, self.popup_rect.y + 10, 100, 50)
        self.draw_button_on_surface(popup_surface, back_btn, "< MENU", SURFACE_2,
                                    text_color=TEXT_MID)

        # Network list
        list_y = self.popup_rect.y + 70
        item_height = int(50 * self.scale)

        if not self.wifi_networks:
            # Scanning message
            scan_text = self.small_font.render("Scansione reti…", True, TEXT_MID)
            popup_surface.blit(scan_text, (self.popup_rect.centerx - scan_text.get_width()//2, list_y + 50))
        else:
            # Show networks
            for i, network in enumerate(self.wifi_networks[:6]):  # Show max 6 networks
                net_rect = pygame.Rect(self.popup_rect.x + 20, list_y + i * (item_height + 5),
                                      self.popup_rect.width - 40, item_height)

                # Row as a raised card
                self.draw_card(popup_surface, net_rect, radius=10)

                # SSID
                ssid_text = self.small_bold_font.render(network['ssid'][:25], True, TEXT_HI)
                popup_surface.blit(ssid_text, (net_rect.x + 12, net_rect.centery - 16))

                # Security - a drawn padlock reads faster than the word alone
                # (FreeSans has no emoji coverage, so the glyph is vector-drawn).
                text_x = net_rect.x + 12
                if network['security'] not in ('Open', '', '--'):
                    ly = net_rect.centery + 9
                    pygame.draw.rect(popup_surface, TEXT_LOW, (text_x, ly, 7, 5),
                                     border_radius=1)
                    pygame.draw.arc(popup_surface, TEXT_LOW,
                                    (text_x + 1, ly - 5, 5, 8), 0, math.pi, 1)
                    text_x += 11
                info_text = self.micro_font.render(network['security'], True, TEXT_LOW)
                popup_surface.blit(info_text, (text_x, net_rect.centery + 4))

                # Signal bars visualization
                bars_x = net_rect.right - 60
                bars_y = net_rect.centery - 10
                signal_level = network['signal'] // 25  # 0-4 bars
                for b in range(4):
                    bar_color = SUCCESS if b < signal_level else SURFACE_3
                    bar_height = (b + 1) * 4
                    pygame.draw.rect(popup_surface, bar_color,
                                   (bars_x + b * 12, bars_y + (16 - bar_height), 8, bar_height),
                                   border_radius=2)

        self._end_popup(popup_surface)

    def draw_password_popup(self):
        """Password input popup with virtual keyboard"""
        popup_surface = self._begin_popup()

        # Title
        if self.password_network:
            title = self.small_bold_font.render(
                f"Password: {self.password_network['ssid'][:20]}", True, TEXT_HI)
            title_rect = title.get_rect(centerx=self.popup_rect.centerx, y=self.popup_rect.y + 15)
            popup_surface.blit(title, title_rect)

        # Password input field
        input_y = self.popup_rect.y + 50
        input_rect = pygame.Rect(self.popup_rect.x + 20, input_y, self.popup_rect.width - 40, 40)
        pygame.draw.rect(popup_surface, SURFACE_0, input_rect, border_radius=8)
        # Focus ring in the accent colour rather than a flat blue outline
        pygame.draw.rect(popup_surface, ACCENT if self.password_input else HAIRLINE,
                         input_rect, width=1, border_radius=8)

        # Show password as dots
        if self.password_input:
            pwd_text = self.small_bold_font.render("•" * len(self.password_input[:30]), True, TEXT_HI)
        else:
            pwd_text = self.small_font.render("Inserisci password", True, TEXT_LOW)
        popup_surface.blit(pwd_text, (input_rect.x + 12,
                                      input_rect.centery - pwd_text.get_height() // 2))
        
        # Keyboard layout - Compact QWERTY for 480x320
        kb_y = self.popup_rect.y + 105
        key_w = int(28 * self.scale)  # Key width
        key_h = int(35 * self.scale)  # Key height
        key_gap = int(3 * self.scale)
        
        # Define keyboard rows
        rows = [
            ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
            ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
            ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
            ['⇧', 'z', 'x', 'c', 'v', 'b', 'n', 'm', '⌫']
        ]
        
        # Draw keyboard
        for row_idx, row in enumerate(rows):
            row_y = kb_y + row_idx * (key_h + key_gap)
            # Center align row
            row_width = len(row) * key_w + (len(row) - 1) * key_gap
            start_x = self.popup_rect.centerx - row_width // 2
            
            for col_idx, key in enumerate(row):
                key_x = start_x + col_idx * (key_w + key_gap)
                key_rect = pygame.Rect(key_x, row_y, key_w, key_h)
                
                # Keycap: modifiers sit darker than letters; active shift is accent.
                shift_on = key == '⇧' and self.keyboard_shift
                if shift_on:
                    color, label_color = ACCENT, SURFACE_0
                elif key in ['⇧', '⌫']:
                    color, label_color = SURFACE_3, TEXT_HI
                else:
                    color, label_color = SURFACE_2, TEXT_HI

                self._draw_keycap(popup_surface, key_rect, color)

                # Key label. Shift/backspace are drawn as vectors: neither
                # FreeSansBold nor the old default font actually carries those
                # code points, so rendering them as text produced tofu boxes.
                if key in ('⇧', '⌫'):
                    self._draw_key_glyph(popup_surface, key, key_rect.center,
                                         label_color, color)
                else:
                    label = key.upper() if self.keyboard_shift and key.isalpha() else key
                    key_text = self.small_bold_font.render(label, True, label_color)
                    text_rect = key_text.get_rect(center=key_rect.center)
                    popup_surface.blit(key_text, text_rect)

        # Space bar
        space_y = kb_y + 4 * (key_h + key_gap)
        space_w = int(key_w * 5)
        space_rect = pygame.Rect(self.popup_rect.centerx - space_w // 2, space_y, space_w, key_h)
        self._draw_keycap(popup_surface, space_rect, SURFACE_2)
        space_text = self.micro_font.render("SPAZIO", True, TEXT_MID)
        popup_surface.blit(space_text, space_text.get_rect(center=space_rect.center))

        # Action buttons (Connect / Cancel)
        btn_y = self.popup_rect.bottom - 60
        btn_w = int(self.popup_rect.width * 0.4)
        btn_h = 45

        connect_rect = pygame.Rect(self.popup_rect.x + 20, btn_y, btn_w, btn_h)
        cancel_rect = pygame.Rect(self.popup_rect.right - btn_w - 20, btn_y, btn_w, btn_h)

        self.draw_button_on_surface(popup_surface, connect_rect, "CONNETTI", SUCCESS)
        self.draw_button_on_surface(popup_surface, cancel_rect, "ANNULLA", SURFACE_2,
                                    text_color=TEXT_MID)

        self._end_popup(popup_surface)

    def _draw_key_glyph(self, surface, key, center, color, bg):
        """Vector shift / backspace glyphs (see caller: the fonts lack them).

        `bg` is the keycap fill, used to knock the X out of the backspace tag.
        """
        x, y = center
        if key == '⇧':
            # Upward arrowhead over a short stem
            pygame.draw.polygon(surface, color,
                                [(x, y - 7), (x - 7, y), (x + 7, y)])
            pygame.draw.rect(surface, color, (x - 3, y, 6, 6))
        else:  # '⌫'
            # Left-pointing tag with an X knocked out of it
            pygame.draw.polygon(surface, color, [
                (x - 8, y), (x - 3, y - 6), (x + 8, y - 6),
                (x + 8, y + 6), (x - 3, y + 6),
            ])
            pygame.draw.line(surface, bg, (x, y - 3), (x + 5, y + 3), 2)
            pygame.draw.line(surface, bg, (x + 5, y - 3), (x, y + 3), 2)

    def _draw_keycap(self, surface, rect, color):
        """Keycap with a 1px bottom shadow so the row reads as physical keys."""
        shadow = rect.move(0, 1)
        pygame.draw.rect(surface, SURFACE_0, shadow, border_radius=6)
        pygame.draw.rect(surface, color, rect, border_radius=6)

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

    def _tint(self, color, factor):
        """Lighten (factor>1) or darken (factor<1) a colour, clamped to 0-255."""
        return tuple(max(0, min(255, int(c * factor))) for c in color[:3])

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

    def draw_button_on_surface(self, surface, rect, text, color=BLUE, text_color=WHITE):
        """Filled pill button.

        The old style outlined every button in 1px white, which read as dated and
        muddied the colour. Depth now comes from a subtle lighter top edge.
        """
        radius = min(14, rect.height // 2)
        pygame.draw.rect(surface, color, rect, border_radius=radius)
        # 1px highlight along the top edge suggests a light source above.
        highlight = pygame.Rect(rect.x + radius // 2, rect.y,
                                max(1, rect.width - radius), 1)
        pygame.draw.rect(surface, self._tint(color, 1.35), highlight)

        text_surface = self.small_bold_font.render(text, True, text_color)
        surface.blit(text_surface, text_surface.get_rect(center=rect.center))

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
        
        if name == "trash":
            # Trash Icon: Bin body and lid
            w, h = size * 0.7, size * 0.8
            # Body
            body_rect = pygame.Rect(x - w//2, y - h//2 + 5, w, h - 5)
            pygame.draw.rect(surface, color, body_rect, 2)
            # Lid
            lid_w = w + 8
            pygame.draw.line(surface, color, (x - lid_w//2, y - h//2), (x + lid_w//2, y - h//2), 2)
            # Handle
            pygame.draw.line(surface, color, (x - 4, y - h//2 - 4), (x + 4, y - h//2 - 4), 2)
            pygame.draw.line(surface, color, (x - 4, y - h//2 - 4), (x - 4, y - h//2), 2)
            pygame.draw.line(surface, color, (x + 4, y - h//2 - 4), (x + 4, y - h//2), 2)
        
        elif name == "power":
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

    def draw_gallery_ui(self):
        """Gallery UI con rotazione"""
        gallery_surf = pygame.Surface((self.width, self.height))
        gallery_surf.fill(SURFACE_0)

        if not self.photos:
            text = self.font.render("Nessuna foto", True, TEXT_MID)
            text_rect = text.get_rect(center=(self.width // 2, self.height // 2 - 10))
            gallery_surf.blit(text, text_rect)
            hint = self.micro_font.render("Scatta per iniziare", True, TEXT_LOW)
            gallery_surf.blit(hint, hint.get_rect(center=(self.width // 2, self.height // 2 + 14)))
        else:
            try:
                # Load photo with proper aspect ratio preservation
                photo_path = self.photos[self.gallery_index]
                img = _pil_image().open(photo_path)
                
                # More generous display area - reduce margins
                display_w = self.width - 40   # 20px margin on each side
                display_h = self.height - 80  # 40px top (for counter) + 40px bottom
                
                # Calculate scaling to fit within display while maintaining aspect ratio
                # Use object-fit: contain logic
                img_w, img_h = img.size
                
                # Calculate scale factor (fit to contain)
                scale_x = display_w / img_w
                scale_y = display_h / img_h
                scale = min(scale_x, scale_y)
                
                # Calculate new dimensions
                new_w = int(img_w * scale)
                new_h = int(img_h * scale)
                
                # Resize with high-quality resampling
                img_resized = img.resize((new_w, new_h), _pil_image().Resampling.LANCZOS)
                
                # Convert to pygame
                mode = img_resized.mode
                size = img_resized.size
                data = img_resized.tobytes()
                photo_surf = pygame.image.fromstring(data, size, mode)
                
                # Center the image
                photo_rect = photo_surf.get_rect(center=(self.width // 2, self.height // 2))
                gallery_surf.blit(photo_surf, photo_rect)
                
                # Debug overlay if enabled
                if DEBUG_MODE:
                    debug_text = f"Orig: {img_w}x{img_h} | Scaled: {new_w}x{new_h} | Scale: {scale:.2f}"
                    debug_surf = self.small_font.render(debug_text, True, YELLOW)
                    gallery_surf.blit(debug_surf, (10, self.height - 30))
                    
            except Exception as e:
                print(f"Gallery load error: {e}")
                error_text = self.small_font.render(f"Errore: {str(e)[:40]}", True, RED)
                gallery_surf.blit(error_text, (10, self.height // 2))
                
        # Top scrim so the chrome stays legible over bright photos
        scrim = pygame.Surface((self.width, 78), pygame.SRCALPHA)
        for i in range(78):
            alpha = int(180 * (1 - i / 78) ** 1.6)
            pygame.draw.line(scrim, (0, 0, 0, alpha), (0, i), (self.width, i))
        gallery_surf.blit(scrim, (0, 0))

        # Counter as a pill (hidden when empty: it used to read "1 / 0")
        if self.photos:
            counter = f"{self.gallery_index + 1} / {len(self.photos)}"
            counter_surf = self.small_bold_font.render(counter, True, TEXT_HI)
            pill = counter_surf.get_rect(center=(self.width // 2, 24)).inflate(20, 10)
            pygame.draw.rect(gallery_surf, SURFACE_2, pill, border_radius=pill.height // 2)
            gallery_surf.blit(counter_surf, counter_surf.get_rect(center=pill.center))

        # Back button with icon
        pygame.draw.rect(gallery_surf, SURFACE_2, self.back_btn_rect, border_radius=12)
        if self.icons is None:
            self.load_icons()
        if "back" in self.icons:
            icon_rect = self.icons["back"].get_rect(center=self.back_btn_rect.center)
            gallery_surf.blit(self.icons["back"], icon_rect)
        else:
            # Chevron-left drawn to match the sidebar arrows
            cx, cy = self.back_btn_rect.center
            pygame.draw.lines(gallery_surf, TEXT_HI, False,
                              [(cx + 5, cy - 9), (cx - 5, cy), (cx + 5, cy + 9)], 2)

        # Trash button (Top Right)
        pygame.draw.rect(gallery_surf, DANGER, self.trash_btn_rect, border_radius=12)
        self.draw_icon(gallery_surf, "trash", self.trash_btn_rect.center, size=24, color=WHITE)

        # Ruota 180
        rotated_gallery = pygame.transform.rotate(gallery_surf, 180)
        self.screen.blit(rotated_gallery, (0, 0))

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
        
        # Password popup handle (highest priority)
        if self.show_password_popup:
            # Keyboard layout dimensions (same as draw function)
            kb_y = self.popup_rect.y + 105
            key_w = int(28 * self.scale)
            key_h = int(35 * self.scale)
            key_gap = int(3 * self.scale)
            
            rows = [
                ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
                ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
                ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
                ['⇧', 'z', 'x', 'c', 'v', 'b', 'n', 'm', '⌫']
            ]
            
            # Check keyboard keys
            for row_idx, row in enumerate(rows):
                row_y = kb_y + row_idx * (key_h + key_gap)
                row_width = len(row) * key_w + (len(row) - 1) * key_gap
                start_x = self.popup_rect.centerx - row_width // 2
                
                for col_idx, key in enumerate(row):
                    key_x = start_x + col_idx * (key_w + key_gap)
                    key_rect = pygame.Rect(key_x, row_y, key_w, key_h)
                    
                    if key_rect.collidepoint(pos):
                        if key == '⌫':  # Backspace
                            self.password_input = self.password_input[:-1]
                        elif key == '⇧':  # Shift
                            self.keyboard_shift = not self.keyboard_shift
                        else:
                            # Add character
                            char = key.upper() if self.keyboard_shift and key.isalpha() else key
                            self.password_input += char
                            # Auto-toggle shift after letter
                            if key.isalpha():
                                self.keyboard_shift = False
                        return
            
            # Check space bar
            space_y = kb_y + 4 * (key_h + key_gap)
            space_w = int(key_w * 5)
            space_rect = pygame.Rect(self.popup_rect.centerx - space_w // 2, space_y, space_w, key_h)
            if space_rect.collidepoint(pos):
                self.password_input += ' '
                return
            
            # Check action buttons
            btn_y = self.popup_rect.bottom - 60
            btn_w = int(self.popup_rect.width * 0.4)
            btn_h = 45
            
            connect_rect = pygame.Rect(self.popup_rect.x + 20, btn_y, btn_w, btn_h)
            cancel_rect = pygame.Rect(self.popup_rect.right - btn_w - 20, btn_y, btn_w, btn_h)
            
            if connect_rect.collidepoint(pos):
                # Connect to network with password
                if self.password_network and self.password_input:
                    print(f"Connecting to {self.password_network['ssid']} with password...")
                    success = self.connect_to_wifi(self.password_network['ssid'], self.password_input)
                    if success:
                        self.show_password_popup = False
                        self.show_wifi_popup = False
                        self.password_input = ""
                        self.password_network = None
                return
            
            if cancel_rect.collidepoint(pos):
                # Cancel password input
                self.show_password_popup = False
                self.show_wifi_popup = True
                self.password_input = ""
                self.password_network = None
                return
            
            return
        
        # WiFi popup handle (priority over power popup)
        if self.show_wifi_popup:
            # Back button
            back_rect = pygame.Rect(10, 10, 100, 50)
            if back_rect.collidepoint(pos):
                self.show_wifi_popup = False
                self.show_power_popup = True
                return
            
            # Network selection
            list_y = self.popup_rect.y + 70
            item_height = int(50 * self.scale)
            
            for i, network in enumerate(self.wifi_networks[:6]):
                net_rect = pygame.Rect(self.popup_rect.x + 20, list_y + i * (item_height + 5), 
                                      self.popup_rect.width - 40, item_height)
                
                if net_rect.collidepoint(pos):
                    ssid = network['ssid']
                    security = network['security']
                    
                    print(f"Selected network: {ssid} ({security})")
                    
                    # Connect to open or protected networks
                    if security == '' or 'Open' in security or security == '--':
                        print(f"Connecting to open network {ssid}...")
                        success = self.connect_to_wifi(ssid)
                        if success:
                            self.show_wifi_popup = False
                    else:
                        # Password required - show password input
                        print(f"Network {ssid} requires password - opening keyboard")
                        self.password_network = network
                        self.password_input = ""
                        self.keyboard_shift = False
                        self.show_wifi_popup = False
                        self.show_password_popup = True
                    return
            return
        
        # Power popup handle
        if self.show_power_popup:
            if self.popup_shutdown_btn.collidepoint(pos):
                subprocess.run(['sudo', 'shutdown', 'now'])
            elif self.popup_standby_btn.collidepoint(pos):
                if self.standby_mode:
                    self.wake_from_standby()
                else:
                    self.enter_standby()
                self.show_power_popup = False
            elif self.popup_server_btn.collidepoint(pos):
                self.toggle_server()
            elif self.popup_hotspot_btn.collidepoint(pos):
                self.toggle_hotspot()
            elif self.popup_wifi_btn.collidepoint(pos):
                print("Opening WiFi manager...")
                self.show_power_popup = False
                self.show_wifi_popup = True
                threading.Thread(target=self.scan_wifi_networks, daemon=True).start()
            elif self.popup_cancel_btn.collidepoint(pos):
                self.show_power_popup = False
            return

        if self.mode == "camera":
            # Priority to buttons in SIDEBARS
            
            # Left Sidebar Interaction
            if x < self.sidebar_width:
                # SHUTTER (Top)
                if self.shutter_up_rect.collidepoint(x, y):
                    self.current_shutter_index = (self.current_shutter_index + 1) % len(SHUTTER_SPEEDS)
                    self.apply_camera_settings()
                    return
                elif self.shutter_down_rect.collidepoint(x, y):
                    self.current_shutter_index = (self.current_shutter_index - 1) % len(SHUTTER_SPEEDS)
                    self.apply_camera_settings()
                    return
                
                # ISO (Bottom)
                if self.iso_up_rect.collidepoint(x, y):
                    self.current_iso_index = (self.current_iso_index + 1) % len(ISO_VALUES)
                    self.apply_camera_settings()
                    return
                elif self.iso_down_rect.collidepoint(x, y):
                    self.current_iso_index = (self.current_iso_index - 1) % len(ISO_VALUES)
                    self.apply_camera_settings()
                    return
            
            # Right Sidebar Interaction
            elif x > self.width - self.sidebar_width:
                # Power button (Top)
                if self.power_rect.collidepoint(x, y):
                    print("Power button pressed!")
                    self.show_power_popup = True
                    return

                # Gallery button (Middle)
                if self.gallery_rect.collidepoint(x, y):
                    self.mode = "gallery"
                    self.load_photos()
                    return

                # B/W Effect button
                if self.bw_rect.collidepoint(x, y):
                    self.grayscale_mode = not self.grayscale_mode
                    print(f"B/W mode: {self.grayscale_mode}")
                    self.apply_camera_settings()
                    return
                
                # Focus Peaking toggle (Bottom)
                if self.fp_rect.collidepoint(x, y):
                    self.focus_peaking_mode = not self.focus_peaking_mode
                    print(f"Focus peaking: {self.focus_peaking_mode}")
                    return
            
            # Since preview is full screen, we remove the tap-preview check completely
            # to avoid unintentional triggers with the 180 flip

        elif self.mode == "gallery":
            # Back Button
            if self.back_btn_rect.collidepoint(pos):
                self.mode = "camera"
                return
            
            # Trash Button
            if self.trash_btn_rect.collidepoint(pos):
                self.delete_photo()
                return
            
            # Navigate
            if x < self.width // 3:
                self.gallery_index = (self.gallery_index - 1) % len(self.photos) if self.photos else 0
            elif x > self.width * 2 // 3:
                self.gallery_index = (self.gallery_index + 1) % len(self.photos) if self.photos else 0

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
            self._draw_splash("Fotocamera non disponibile")
            print(f"Fatal: camera unavailable ({self.camera_error})")
            return
        print(f"✓ Ready in {time.monotonic() - _T0:.2f}s")

        try:
            while self.running:
                # Capture pending
                if self.capture_pending:
                    self.capture_photo()
                    self.capture_pending = False
                
                # Process touch queue from evdev
                try:
                    while True:
                        event_data = self.touch_queue.get_nowait()
                        event_type, x, y = event_data
                        if event_type == 'click':
                            # Wake from standby on any touch
                            if self.standby_mode:
                                self.wake_from_standby()
                            # Restore screen if off
                            elif self.saved_brightness == "software_black":
                                self.saved_brightness = None
                            else:
                                self.handle_touch((x, y))
                except queue.Empty:
                    pass
                
                # Process UDP commands
                self.udp_check()
                
                # Update Battery every 30 seconds
                current_time = time.monotonic()
                if self.battery and current_time - self.last_battery_update > 30:
                    self.battery.update()
                    self.last_battery_update = current_time
                
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
                        # Wake from standby on any touch
                        if self.standby_mode:
                            self.wake_from_standby()
                        elif self.saved_brightness == "software_black":
                            self.saved_brightness = None
                        else:
                            self.handle_touch(event.pos)
                
                # Screen off mode or standby
                if self.saved_brightness == "software_black" or self.standby_mode:
                    self.screen.fill(BLACK)
                    # In standby, skip preview update to save power
                    if not self.standby_mode:
                        self.update_preview()
                else:
                    # Sync remote status timeout
                    if time.monotonic() - self.remote_last_heartbeat > 10:
                        self.remote_active = False
                        
                    # Update preview
                    if self.mode == "camera":
                        self.update_preview()
                        self.draw_camera_ui()
                    elif self.mode == "gallery":
                        self.draw_gallery_ui()
                
                self._present()

                if not self._first_frame.is_set() and self.current_preview_surface is not None:
                    self._first_frame.set()
                    # The number that matters for boot time: power-on to a
                    # live viewfinder is roughly the firmware stage plus this.
                    print(f"✓ First frame on screen {time.monotonic() - _T0:.2f}s "
                          f"after start, {_uptime():.1f}s after kernel start")

                clock.tick(30)
                
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Cleanup"""
        print("Shutting down...")
        self._meta_thread_running = False  # stop async metadata thread
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


if __name__ == "__main__":
    main()
