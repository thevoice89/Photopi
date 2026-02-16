#!/usr/bin/env python3
"""
Raspberry Pi HQ Camera Application - FIXED VERSION
Ottimizzato per display 3.5" 480x320 con touch
"""

import pygame
import os
import sys
from datetime import datetime
from pathlib import Path
from picamera2 import Picamera2
from gpiozero import Button
import time
import threading
import queue
import socket
import json
import subprocess
from PIL import Image
import numpy as np
import struct

# Battery Management
try:
    import smbus
    SMBUS_AVAILABLE = True
except ImportError:
    SMBUS_AVAILABLE = False
    print("Warning: python3-smbus not installed, battery indicator will not work")

class BatteryManager:
    def __init__(self):
        self.percentage = 0
        self.available = False
        self.last_update = 0
        if SMBUS_AVAILABLE:
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
            
            # Debug output
            print(f"🔋 CW2015 BATTERY:")
            print(f"   SOC: {soc_high}% + {soc_low}/256 = {self.percentage:.1f}%")
            print(f"   VCELL: 0x{vcell_raw:04x} = {self.voltage:.3f}V")
            
        except Exception as e:
            print(f"Battery update error: {e}")

# Try to import evdev for raw touch input
try:
    import evdev
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    print("Warning: python3-evdev not installed, touch might not work correctly")
    print("Install with: sudo apt-get install python3-evdev")

# Configuration
DEBUG_MODE = False  # Set to True only for diagnostics
PHOTOS_DIR = Path.home() / "photos"
GPIO_BUTTON_PIN = 26
BUTTON_DEBOUNCE = 0.3
UDP_PORT = 12345
SHARED_MEM_PREVIEW = "/tmp/camera_preview.jpg"
SHARED_MEM_STATUS = "/tmp/camera_status.json"

# Colors
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY = (128, 128, 128)
LIGHT_GRAY = (200, 200, 200)
DARK_GRAY = (50, 50, 50)
GREEN = (0, 200, 0)
BLUE = (0, 120, 200)
RED = (200, 0, 0)
YELLOW = (255, 255, 0)

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


class CameraApp:
    def __init__(self):
        """Initialize the camera application"""
        PHOTOS_DIR.mkdir(exist_ok=True)
        
        print("Initializing display...")
        os.environ['SDL_NOMOUSE'] = '1'
        
        pygame.init()
        
        # Get display info
        try:
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
            self.headless = True
            self.width = 480
            self.height = 320
            os.environ['SDL_VIDEODRIVER'] = 'dummy'
            pygame.init()
            self.screen = pygame.display.set_mode((self.width, self.height))
        
        pygame.mouse.set_visible(False)
        
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
        
        self.font = pygame.font.Font(None, self.font_size)
        self.small_font = pygame.font.Font(None, self.small_font_size)
        
        print(f"DEBUG: Screen size: {self.width}x{self.height}, Scale: {self.scale}")

        # Camera config - Preview ottimizzato per BOOT VELOCE
        self.photo_width = 4056
        self.photo_height = 3040
        # Use higher resolution for better quality, will scale to fit display
        self.preview_width = 426
        self.preview_height = 320
        
        # Initialize camera FIRST for fast boot
        self.camera = Picamera2()
        
        # Config video per preview veloce
        config = self.camera.create_video_configuration(
            main={"size": (self.preview_width, self.preview_height), "format": "RGB888"},
            buffer_count=1  # Ridotto da 2 a 1 per boot più veloce
        )
        self.camera.configure(config)
        
        print(f"Camera config: {self.camera.camera_config}")
        
        # Camera settings
        self.current_iso_index = 0
        self.current_shutter_index = 0
        
        # Start camera IMMEDIATELY
        try:
            self.camera.start()
            print("✓ Camera started FAST!")
        except Exception as e:
            print(f"Camera start error: {e}")
        
        # GPIO Button
        self.capture_pending = False
        self.button = Button(GPIO_BUTTON_PIN, pull_up=True, bounce_time=BUTTON_DEBOUNCE)
        self.button.when_pressed = self.trigger_capture
        print(f"GPIO button on pin {GPIO_BUTTON_PIN}")
        
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
        
        # Preview surface cache
        self.current_preview_surface = None
        self.last_preview_time = 0
        
        # Prepare UDP listener
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Bind to all interfaces to be sure
            self.sock.bind(('0.0.0.0', UDP_PORT))
            self.sock.setblocking(False)
            print(f"UDP listener bound to 0.0.0.0:{UDP_PORT}")
        except Exception as e:
            print(f"UDP Bind Error: {e}")
        
        # Load Icons
        self.load_icons()
        
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

    def _lazy_init(self):
        """Initialize non-critical components in background for faster boot"""
        time.sleep(0.5)  # Let preview start first
        
        print("Background init: Battery manager...")
        self.battery = BatteryManager()
        
        print("Background init: Touch input...")
        if EVDEV_AVAILABLE:
            self.init_touch_input()
        
        print("Background init: Loading photos...")
        self.load_photos()
        
        print("✓ Background initialization complete")

    def load_icons(self):
        """Load UI icons from assets folder"""
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
                    img = pygame.image.load(str(path)).convert_alpha()
                    # Use smaller fixed icon sizes to prevent stretching
                    size = 24  # Smaller fixed size for minimal look
                    self.icons[name] = pygame.transform.smoothscale(img, (size, size))
                except Exception as e:
                    print(f"Error loading icon {name}: {e}")
            else:
                print(f"Icon not found: {path}")

    def trigger_capture(self):
        """GPIO button callback"""
        self.capture_pending = True
    
    def init_touch_input(self):
        """Inizializza input touch diretto via evdev"""
        try:
            # Trova il dispositivo touch
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            for device in devices:
                if 'ADS7846' in device.name or 'touch' in device.name.lower():
                    self.touch_device = device
                    print(f"Touch device found: {device.name}")
                    break
            
            if self.touch_device:
                # Thread per leggere eventi touch
                self.touch_thread = threading.Thread(target=self.touch_reader_thread, daemon=True)
                self.touch_thread.start()
                print("Touch reader thread started")
            else:
                print("No touch device found, using pygame events")
                
        except Exception as e:
            print(f"Touch init error: {e}")
    
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
                    self.trigger_capture()
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
        # Power Top, Gallery Middle, B/W Bottom
        right_center_x = self.width - (self.sidebar_width // 2)
        
        self.power_center = (right_center_x, int(self.height * 0.15))
        self.gallery_center = (right_center_x, int(self.height * 0.50))
        self.bw_center = (right_center_x, int(self.height * 0.85))
        
        # Button Rects for Right Sidebar
        btn_touch_size = int(60 * self.scale)
        self.power_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.power_rect.center = self.power_center
        
        self.gallery_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.gallery_rect.center = self.gallery_center
        
        self.bw_rect = pygame.Rect(0, 0, btn_touch_size, btn_touch_size)
        self.bw_rect.center = self.bw_center
        
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

    def check_server_status(self):
        """Check if photo server is running"""
        try:
            status = subprocess.run(['systemctl', 'is-active', 'photo-server'], 
                                   capture_output=True, text=True).stdout.strip()
            return status == 'active'
        except:
            return False

    def toggle_server(self):
        """Toggle photo server"""
        if self.check_server_status():
            subprocess.run(['sudo', 'systemctl', 'stop', 'photo-server'])
        else:
            subprocess.run(['sudo', 'systemctl', 'start', 'photo-server'])

    def check_hotspot_status(self):
        """Check if hotspot is active"""
        try:
            status = subprocess.run(['nmcli', '-t', '-f', 'ACTIVE,NAME', 'con', 'show'], 
                                   capture_output=True, text=True).stdout
            return "yes:RaspiCam_Hotspot" in status
        except:
            return False

    def toggle_hotspot(self):
        """Toggle hotspot"""
        if self.check_hotspot_status():
            subprocess.run(['sudo', 'nmcli', 'con', 'down', 'RaspiCam_Hotspot'])
        else:
            subprocess.run(['sudo', 'nmcli', 'con', 'up', 'RaspiCam_Hotspot'])
    
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
        """Enter low power standby mode with display power off"""
        print("Entering standby mode...")
        self.standby_mode = True
        
        try:
            # Stop camera to save power
            self.camera.stop()
            print("✓ Camera stopped")
            
            # Turn off LCD backlight - Method 1: via sysfs (more common)
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 0 > /sys/class/backlight/*/brightness'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LCD backlight disabled via sysfs")
            
            # Turn off LCD backlight - Method 2: GPIO pin 18 (common for 3.5" displays)
            # This might fail if your display doesn't use GPIO18, that's OK
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 18 > /sys/class/gpio/export'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c',
                          'echo out > /sys/class/gpio/gpio18/direction'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 0 > /sys/class/gpio/gpio18/value'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LCD backlight disabled via GPIO18")
            
            # Disable framebuffer blanking (we control power manually now)
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 1 > /sys/class/graphics/fb1/blank'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ Framebuffer blanked")
            
            # Disable HDMI (if connected to external display)
            subprocess.run(['sudo', 'tvservice', '-o'], check=False, 
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ HDMI disabled")
            
            # CPU to powersave mode
            subprocess.run(['sudo', 'cpufreq-set', '-g', 'powersave'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ CPU powersave")
            
            # Disable activity LED
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 0 > /sys/class/leds/led0/brightness'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LED off")
            
            print("🌙 Standby mode active - display power OFF - touch to wake")
            
        except Exception as e:
            print(f"Standby error: {e}")
    
    def wake_from_standby(self):
        """Wake from standby mode and restore display power"""
        if not self.standby_mode:
            return
            
        print("Waking from standby...")
        
        try:
            # Re-enable LCD backlight - Method 1: via sysfs
            # First try to set max brightness (usually 255)
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 255 > /sys/class/backlight/*/brightness'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LCD backlight enabled via sysfs")
            
            # Re-enable LCD backlight - Method 2: GPIO pin 18
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 1 > /sys/class/gpio/gpio18/value'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Cleanup GPIO export (optional)
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 18 > /sys/class/gpio/unexport'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ LCD backlight enabled via GPIO18")
            
            # Unblank framebuffer
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 0 > /sys/class/graphics/fb1/blank'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("✓ Framebuffer unblanked")
            
            # Re-enable HDMI
            subprocess.run(['sudo', 'tvservice', '-p'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Wait for HDMI to initialize
            time.sleep(0.5)
            
            # Restore framebuffer
            subprocess.run(['sudo', 'fbset', '-depth', '8'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['sudo', 'fbset', '-depth', '16'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # CPU back to ondemand
            subprocess.run(['sudo', 'cpufreq-set', '-g', 'ondemand'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Re-enable LED
            subprocess.run(['sudo', 'sh', '-c',
                          'echo 1 > /sys/class/leds/led0/brightness'], check=False,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Restart camera
            config = self.camera.create_video_configuration(
                main={"size": (self.preview_width, self.preview_height), "format": "RGB888"},
                buffer_count=1
            )
            self.camera.configure(config)
            self.camera.start()
            print("✓ Camera restarted")
            
            # Screen on
            self.saved_brightness = None
            self.standby_mode = False
            
            print("✓ Wake complete - display power ON")
            
        except Exception as e:
            print(f"Wake error: {e}")
            # Fallback: just restart camera
            try:
                self.camera.start()
            except:
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
            self.camera.set_controls({"AeEnable": True})
            
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
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = PHOTOS_DIR / f"photo_{timestamp}.jpg"
        
        try:
            print(f"Capturing to {filename}...")
            
            # Feedback visivo
            self.screen.fill(WHITE)
            pygame.display.flip()
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
            
            # Capture
            self.camera.capture_file(str(filename))
            print(f"Photo saved: {filename}")
            
            # Restore preview
            self.camera.stop()
            config = self.camera.create_video_configuration(
                main={"size": (self.preview_width, self.preview_height), "format": "RGB888"},
                buffer_count=2
            )
            self.camera.configure(config)
            self.camera.start()
            
            self.load_photos()
            
        except Exception as e:
            print(f"Capture error: {e}")
            import traceback
            traceback.print_exc()
            
            try:
                self.camera.stop()
                config = self.camera.create_video_configuration(
                    main={"size": (self.preview_width, self.preview_height), "format": "RGB888"},
                    buffer_count=2
                )
                self.camera.configure(config)
                self.camera.start()
            except:
                pass

    def load_photos(self):
        """Load photo list"""
        self.photos = sorted(PHOTOS_DIR.glob("*.jpg"), reverse=True)
        self.photo_cache.clear()
        self.loading_paths.clear()

    def update_preview(self):
        """Capture e mostra preview VELOCE"""
        try:
            current_time = time.monotonic()
            # 20 FPS max
            if current_time - self.last_preview_time < 0.05:
                return
            self.last_preview_time = current_time
            
            # Capture frame
            frame = self.camera.capture_array("main")
            
            if frame is not None and len(frame.shape) == 3:
                # BGR -> RGB
                frame_rgb = frame[:, :, ::-1].copy()
                
                h, w = frame_rgb.shape[:2]
                
                # Crea surface
                surf = pygame.image.frombuffer(frame_rgb.tobytes(), (w, h), 'RGB')
                
                # Scale to fit display (object-fit: contain style) within preview_rect
                scale_x = self.preview_rect.width / w
                scale_y = self.preview_rect.height / h
                scale_factor = min(scale_x, scale_y) * 1.15  # Zoom 15% to reduce black bars
                
                new_w = int(w * scale_factor)
                new_h = int(h * scale_factor)
                
                # Scale surf
                scaled_surf = pygame.transform.scale(surf, (new_w, new_h))
                
                # Center in preview_rect
                final_surf = pygame.Surface((self.preview_rect.width, self.preview_rect.height))
                # Fill with black just in case
                final_surf.fill(BLACK) 
                
                x_offset = (self.preview_rect.width - new_w) // 2
                y_offset = (self.preview_rect.height - new_h) // 2
                
                final_surf.blit(scaled_surf, (x_offset, y_offset))
                
                # Apply B/W effect to preview if active (software fallback for preview)
                if self.grayscale_mode:
                     # Handled by camera settings usually, but passing here too
                     pass

                self.current_preview_surface = final_surf
                
                # self.current_preview_surface = final_surf # OLD logic replaced

                # Sync to shared memory for remote preview (Max 5 FPS to save CPU)
                current_t = time.monotonic()
                if current_t - self.last_shm_sync > 0.2:
                    self.last_shm_sync = current_t
                    try:
                        # Save current frame as JPEG to shared memory
                        img = Image.fromarray(frame_rgb)
                        
                        # Use a temporary file and rename to ensure atomic write for server
                        temp_path = f"{SHARED_MEM_PREVIEW}.tmp"
                        img.save(temp_path, "JPEG", quality=75)
                        os.replace(temp_path, SHARED_MEM_PREVIEW)
                        
                        # Update status for web UI (ALWAYS SYNC)
                        status = {
                            "iso": str(ISO_VALUES[self.current_iso_index]),
                            "shutter": SHUTTER_SPEEDS[self.current_shutter_index][0],
                            "mode": "remote" if self.remote_active else "local",
                            "status": "active"
                        }
                        temp_status = f"{SHARED_MEM_STATUS}.tmp"
                        with open(temp_status, 'w') as f:
                            json.dump(status, f)
                        os.replace(temp_status, SHARED_MEM_STATUS)
                        
                    except Exception as e:
                        print(f"SHM Sync Error: {e}")
                
        except Exception as e:
            print(f"Update Preview Error: {e}")

    def draw_camera_ui(self):
        """Disegna UI camera - FULL PREVIEW + TRASPARENZA"""
        # Creiamo un surface per l'intera UI così possiamo ruotarlo se serve
        ui_surface = pygame.Surface((self.width, self.height))
        ui_surface.fill(BLACK)
        
        # 1. Preview (sfondo)
        if self.current_preview_surface:
            ui_surface.blit(self.current_preview_surface, self.preview_rect)
        else:
            pygame.draw.rect(ui_surface, (20, 20, 20), self.preview_rect)
            text = self.font.render("Caricamento...", True, WHITE)
            text_rect = text.get_rect(center=self.preview_rect.center)
            ui_surface.blit(text, text_rect)
        
        # 2. Sidebars (Left and Right)
        # Left Bar
        pygame.draw.rect(ui_surface, BLACK, self.left_bar_rect)
        # Right Bar
        pygame.draw.rect(ui_surface, BLACK, self.right_bar_rect)
        
        # 3. SHUTTER Area (Left Top)
        # Arrow Up
        self.draw_arrow(ui_surface, self.shutter_up_rect.center, "up")
        
        # Label "SHUTTER"
        shutter_lbl = self.small_font.render("SHUTTER", True, GRAY)
        lbl_rect = shutter_lbl.get_rect(center=(self.shutter_center[0], self.shutter_center[1] - 15))
        ui_surface.blit(shutter_lbl, lbl_rect)
        
        # Value
        shutter_val = SHUTTER_SPEEDS[self.current_shutter_index][0]
        shutter_text = self.font.render(shutter_val, True, WHITE) # Using larger font for value
        sh_rect = shutter_text.get_rect(center=(self.shutter_center[0], self.shutter_center[1] + 15))
        ui_surface.blit(shutter_text, sh_rect)
        
        # Arrow Down
        self.draw_arrow(ui_surface, self.shutter_down_rect.center, "down")
        
        # 4. ISO Area (Left Bottom)
        # Arrow Up
        self.draw_arrow(ui_surface, self.iso_up_rect.center, "up")
        
        # Label "ISO"
        iso_lbl = self.small_font.render("ISO", True, GRAY)
        lbl_rect = iso_lbl.get_rect(center=(self.iso_center[0], self.iso_center[1] - 15))
        ui_surface.blit(iso_lbl, lbl_rect)
        
        # Value
        iso_val = str(ISO_VALUES[self.current_iso_index])
        iso_text = self.font.render(iso_val, True, WHITE) # Using larger font for value
        iso_rect = iso_text.get_rect(center=(self.iso_center[0], self.iso_center[1] + 15))
        ui_surface.blit(iso_text, iso_rect)
        
        # Arrow Down
        self.draw_arrow(ui_surface, self.iso_down_rect.center, "down")
        
        # 5. Right Sidebar Procedural Icons
        
        # POWER (Top)
        self.draw_icon(ui_surface, "power", self.power_center)
        
        # GALLERY / GRID (Middle)
        self.draw_icon(ui_surface, "grid", self.gallery_center)
        
        # B/W (Bottom)
        self.draw_icon(ui_surface, "bw", self.bw_center, active=self.grayscale_mode)
        
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
            color = GREEN if batt_val > 20 else RED
            
            # Show only percentage
            batt_text = self.font.render(f"{batt_val}%", True, color)
            
            # Position at top-right
            ui_surface.blit(batt_text, (self.width - 80, 15))

        # 9. Debug Overlay (if enabled)
        if DEBUG_MODE and self.battery and self.battery.available:
            debug_lines = [
                f"Batt: {self.battery.percentage:.1f}% ({self.battery.voltage:.3f}V)",
                f"FPS: {int(1.0 / (time.monotonic() - self.last_preview_time + 0.001))}"
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

    def draw_power_popup(self):
        """Power menu popup - RUOTATO SE SERVE"""
        popup_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        
        # Semi-transparent overlay
        overlay = pygame.Surface((self.width, self.height))
        overlay.set_alpha(220)
        overlay.fill(BLACK)
        popup_surface.blit(overlay, (0, 0))
        
        # Popup background
        pygame.draw.rect(popup_surface, (40, 40, 40), self.popup_rect, border_radius=15)
        pygame.draw.rect(popup_surface, GRAY, self.popup_rect, width=2, border_radius=15)
        
        # Title
        title = self.font.render("MENU SISTEMA", True, WHITE)
        title_rect = title.get_rect(centerx=self.popup_rect.centerx, y=self.popup_rect.y + 15)
        popup_surface.blit(title, title_rect)
        
        # Buttons - now 2 rows (removed LCD Off)
        self.draw_button_on_surface(popup_surface, self.popup_shutdown_btn, "Spegni Pi", RED)
        
        standby_color = YELLOW if self.standby_mode else BLUE
        standby_text = "Risveglia" if self.standby_mode else "Standby"
        self.draw_button_on_surface(popup_surface, self.popup_standby_btn, standby_text, standby_color)
        
        server_active = self.check_server_status()
        server_color = GREEN if server_active else DARK_GRAY
        server_text = "Server: ON" if server_active else "Server: OFF"
        self.draw_button_on_surface(popup_surface, self.popup_server_btn, server_text, server_color)
        
        hotspot_active = self.check_hotspot_status()
        hotspot_color = GREEN if hotspot_active else DARK_GRAY
        hotspot_text = "Hotspot: ON" if hotspot_active else "Hotspot: OFF"
        self.draw_button_on_surface(popup_surface, self.popup_hotspot_btn, hotspot_text, hotspot_color)
        
        # WiFi Manager button (Row 3)
        self.draw_button_on_surface(popup_surface, self.popup_wifi_btn, "WiFi Manager", BLUE)
        
        # Show IP info in self.popup_rect area below buttons
        # Now buttons occupy 3 rows, so adjust info display area
        info_y_start = self.popup_rect.y + int(250 * self.scale)
        
        if server_active and not hotspot_active:
            # Get actual IP address for local network
            try:
                import socket
                hostname = socket.gethostname()
                ip_address = socket.gethostbyname(hostname)
                if ip_address != "127.0.0.1":
                    server_info = self.small_font.render(f"Server: http://{ip_address}:8080", True, WHITE)
                    popup_surface.blit(server_info, (self.popup_rect.centerx - server_info.get_width()//2, info_y_start))
                    info_y_start += 25
            except:
                pass
        
        if hotspot_active:
            info_h = self.small_font.render("SSID: RaspiCam | Pass: raspicam_admin", True, WHITE)
            info_ip = self.small_font.render("Hotspot: http://10.42.0.1:8080", True, WHITE)
            popup_surface.blit(info_h, (self.popup_rect.centerx - info_h.get_width()//2, info_y_start))
            popup_surface.blit(info_ip, (self.popup_rect.centerx - info_ip.get_width()//2, info_y_start + 25))
            
        self.draw_button_on_surface(popup_surface, self.popup_cancel_btn, "Chiudi", GRAY)
        
        # Ruota il popup e blit sullo schermo
        rotated_popup = pygame.transform.rotate(popup_surface, 180)
        self.screen.blit(rotated_popup, (0, 0))

    def draw_wifi_popup(self):
        """WiFi selection popup"""
        popup_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        
        # Semi-transparent overlay
        overlay = pygame.Surface((self.width, self.height))
        overlay.set_alpha(220)
        overlay.fill(BLACK)
        popup_surface.blit(overlay, (0, 0))
        
        # Popup background
        pygame.draw.rect(popup_surface, (40, 40, 40), self.popup_rect, border_radius=15)
        pygame.draw.rect(popup_surface, GRAY, self.popup_rect, width=2, border_radius=15)
        
        # Title
        title = self.font.render("RETI WIFI", True, WHITE)
        title_rect = title.get_rect(centerx=self.popup_rect.centerx, y=self.popup_rect.y + 15)
        popup_surface.blit(title, title_rect)
        
        # Back button
        back_btn = pygame.Rect(self.popup_rect.x + 10, self.popup_rect.y + 10, 100, 50)
        self.draw_button_on_surface(popup_surface, back_btn, "< MENU", GRAY)
        
        # Network list
        list_y = self.popup_rect.y + 70
        item_height = int(50 * self.scale)
        
        if not self.wifi_networks:
            # Scanning message
            scan_text = self.small_font.render("Scansione reti...", True, WHITE)
            popup_surface.blit(scan_text, (self.popup_rect.centerx - scan_text.get_width()//2, list_y + 50))
        else:
            # Show networks
            for i, network in enumerate(self.wifi_networks[:6]):  # Show max 6 networks
                net_rect = pygame.Rect(self.popup_rect.x + 20, list_y + i * (item_height + 5), 
                                      self.popup_rect.width - 40, item_height)
                
                # Network button
                pygame.draw.rect(popup_surface, DARK_GRAY, net_rect, border_radius=8)
                pygame.draw.rect(popup_surface, GRAY, net_rect, width=1, border_radius=8)
                
                # SSID
                ssid_text = self.small_font.render(network['ssid'][:25], True, WHITE)
                popup_surface.blit(ssid_text, (net_rect.x + 10, net_rect.centery - 15))
                
                # Security & Signal
                info = f"{network['security']} | Signal: {network['signal']}%"
                info_text = self.small_font.render(info, True, LIGHT_GRAY)
                popup_surface.blit(info_text, (net_rect.x + 10, net_rect.centery + 5))
                
                # Signal bars visualization
                bars_x = net_rect.right - 60
                bars_y = net_rect.centery - 10
                signal_level = network['signal'] // 25  # 0-4 bars
                for b in range(4):
                    bar_color = GREEN if b < signal_level else DARK_GRAY
                    bar_height = (b + 1) * 4
                    pygame.draw.rect(popup_surface, bar_color, 
                                   (bars_x + b * 12, bars_y + (16 - bar_height), 8, bar_height))
        
        # Ruota il popup
        rotated_popup = pygame.transform.rotate(popup_surface, 180)
        self.screen.blit(rotated_popup, (0, 0))

    def draw_password_popup(self):
        """Password input popup with virtual keyboard"""
        popup_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        
        # Semi-transparent overlay
        overlay = pygame.Surface((self.width, self.height))
        overlay.set_alpha(220)
        overlay.fill(BLACK)
        popup_surface.blit(overlay, (0, 0))
        
        # Popup background
        pygame.draw.rect(popup_surface, (40, 40, 40), self.popup_rect, border_radius=15)
        pygame.draw.rect(popup_surface, GRAY, self.popup_rect, width=2, border_radius=15)
        
        # Title
        if self.password_network:
            title = self.small_font.render(f"Password: {self.password_network['ssid'][:20]}", True, WHITE)
            title_rect = title.get_rect(centerx=self.popup_rect.centerx, y=self.popup_rect.y + 15)
            popup_surface.blit(title, title_rect)
        
        # Password input field
        input_y = self.popup_rect.y + 50
        input_rect = pygame.Rect(self.popup_rect.x + 20, input_y, self.popup_rect.width - 40, 40)
        pygame.draw.rect(popup_surface, (60, 60, 60), input_rect, border_radius=5)
        pygame.draw.rect(popup_surface, BLUE, input_rect, width=2, border_radius=5)
        
        # Show password as dots
        password_display = "•" * len(self.password_input) if self.password_input else "Inserisci password"
        pwd_color = WHITE if self.password_input else GRAY
        pwd_text = self.small_font.render(password_display[:30], True, pwd_color)
        popup_surface.blit(pwd_text, (input_rect.x + 10, input_rect.y + 10))
        
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
                
                # Special keys color
                if key in ['⇧', '⌫']:
                    color = YELLOW if (key == '⇧' and self.keyboard_shift) else DARK_GRAY
                else:
                    color = (50, 50, 50)
                
                pygame.draw.rect(popup_surface, color, key_rect, border_radius=4)
                pygame.draw.rect(popup_surface, GRAY, key_rect, width=1, border_radius=4)
                
                # Key label
                label = key.upper() if self.keyboard_shift and key.isalpha() else key
                key_text = self.small_font.render(label, True, WHITE)
                text_rect = key_text.get_rect(center=key_rect.center)
                popup_surface.blit(key_text, text_rect)
        
        # Space bar
        space_y = kb_y + 4 * (key_h + key_gap)
        space_w = int(key_w * 5)
        space_rect = pygame.Rect(self.popup_rect.centerx - space_w // 2, space_y, space_w, key_h)
        pygame.draw.rect(popup_surface, (50, 50, 50), space_rect, border_radius=4)
        pygame.draw.rect(popup_surface, GRAY, space_rect, width=1, border_radius=4)
        space_text = self.small_font.render("SPACE", True, WHITE)
        popup_surface.blit(space_text, (space_rect.centerx - space_text.get_width()//2, space_rect.centery - 8))
        
        # Action buttons (Connect / Cancel)
        btn_y = self.popup_rect.bottom - 60
        btn_w = int(self.popup_rect.width * 0.4)
        btn_h = 45
        
        connect_rect = pygame.Rect(self.popup_rect.x + 20, btn_y, btn_w, btn_h)
        cancel_rect = pygame.Rect(self.popup_rect.right - btn_w - 20, btn_y, btn_w, btn_h)
        
        self.draw_button_on_surface(popup_surface, connect_rect, "CONNETTI", GREEN)
        self.draw_button_on_surface(popup_surface, cancel_rect, "ANNULLA", RED)
        
        # Rotate and display
        rotated_popup = pygame.transform.rotate(popup_surface, 180)
        self.screen.blit(rotated_popup, (0, 0))

    def draw_password_popup(self):
        """Password input popup with virtual keyboard"""
        popup_surface = pygame.Surface((self.width, self.height), pygame.SRCALPHA)
        
        # Semi-transparent overlay
        overlay = pygame.Surface((self.width, self.height))
        overlay.set_alpha(220)
        overlay.fill(BLACK)
        popup_surface.blit(overlay, (0, 0))
        
        # Popup background
        pygame.draw.rect(popup_surface, (40, 40, 40), self.popup_rect, border_radius=15)
        pygame.draw.rect(popup_surface, GRAY, self.popup_rect, width=2, border_radius=15)
        
        # Title
        if self.password_network:
            title = self.small_font.render(f"Password: {self.password_network['ssid'][:20]}", True, WHITE)
            title_rect = title.get_rect(centerx=self.popup_rect.centerx, y=self.popup_rect.y + 15)
            popup_surface.blit(title, title_rect)
        
        # Password input field
        input_y = self.popup_rect.y + 50
        input_rect = pygame.Rect(self.popup_rect.x + 20, input_y, self.popup_rect.width - 40, 40)
        pygame.draw.rect(popup_surface, (60, 60, 60), input_rect, border_radius=5)
        pygame.draw.rect(popup_surface, BLUE, input_rect, width=2, border_radius=5)
        
        # Show password as dots
        password_display = "•" * len(self.password_input) if self.password_input else "Inserisci password"
        pwd_color = WHITE if self.password_input else GRAY
        pwd_text = self.small_font.render(password_display[:30], True, pwd_color)
        popup_surface.blit(pwd_text, (input_rect.x + 10, input_rect.y + 10))
        
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
                
                # Special keys color
                if key in ['⇧', '⌫']:
                    color = YELLOW if (key == '⇧' and self.keyboard_shift) else DARK_GRAY
                else:
                    color = (50, 50, 50)
                
                pygame.draw.rect(popup_surface, color, key_rect, border_radius=4)
                pygame.draw.rect(popup_surface, GRAY, key_rect, width=1, border_radius=4)
                
                # Key label
                label = key.upper() if self.keyboard_shift and key.isalpha() else key
                key_text = self.small_font.render(label, True, WHITE)
                text_rect = key_text.get_rect(center=key_rect.center)
                popup_surface.blit(key_text, text_rect)
        
        # Space bar
        space_y = kb_y + 4 * (key_h + key_gap)
        space_w = int(key_w * 5)
        space_rect = pygame.Rect(self.popup_rect.centerx - space_w // 2, space_y, space_w, key_h)
        pygame.draw.rect(popup_surface, (50, 50, 50), space_rect, border_radius=4)
        pygame.draw.rect(popup_surface, GRAY, space_rect, width=1, border_radius=4)
        space_text = self.small_font.render("SPACE", True, WHITE)
        popup_surface.blit(space_text, (space_rect.centerx - space_text.get_width()//2, space_rect.centery - 8))
        
        # Action buttons (Connect / Cancel)
        btn_y = self.popup_rect.bottom - 60
        btn_w = int(self.popup_rect.width * 0.4)
        btn_h = 45
        
        connect_rect = pygame.Rect(self.popup_rect.x + 20, btn_y, btn_w, btn_h)
        cancel_rect = pygame.Rect(self.popup_rect.right - btn_w - 20, btn_y, btn_w, btn_h)
        
        self.draw_button_on_surface(popup_surface, connect_rect, "CONNETTI", GREEN)
        self.draw_button_on_surface(popup_surface, cancel_rect, "ANNULLA", RED)
        
        # Rotate and display
        rotated_popup = pygame.transform.rotate(popup_surface, 180)
        self.screen.blit(rotated_popup, (0, 0))

    def draw_button_on_surface(self, surface, rect, text, color=BLUE, text_color=WHITE):
        """Draw button with rounded corners on specific surface"""
        pygame.draw.rect(surface, color, rect, border_radius=8)
        pygame.draw.rect(surface, WHITE, rect, width=1, border_radius=8)
        
        text_surface = self.small_font.render(text, True, text_color)
        text_rect = text_surface.get_rect(center=rect.center)
        surface.blit(text_surface, text_rect)

    def draw_arrow(self, surface, center, direction):
        """Draw a minimal triangle arrow"""
        x, y = center
        size = 8  # Size of the arrow
        color = LIGHT_GRAY
        
        if direction == "up":
            points = [(x, y - size), (x - size, y + size), (x + size, y + size)]
        else:
            points = [(x, y + size), (x - size, y - size), (x + size, y - size)]
            
        pygame.draw.polygon(surface, color, points)

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
            # Power Icon: Perfect White Circle with Black Dot (Minimalist)
            radius = size // 2
            
            # 1. White Circle (Filled)
            pygame.draw.circle(surface, WHITE, center, radius)
            
            # 2. Black Dot (Filled)
            # Size: approx 30% of circle
            dot_radius = max(3, radius // 3)
            pygame.draw.circle(surface, BLACK, center, dot_radius)

            
        elif name == "grid":
            # Grid Icon: 4 squares
            sq_size = size // 2.5
            gap = 2
            # Top-Left
            pygame.draw.rect(surface, color, (x - gap - sq_size, y - gap - sq_size, sq_size, sq_size), 1, border_radius=1)
            # Top-Right
            pygame.draw.rect(surface, color, (x + gap, y - gap - sq_size, sq_size, sq_size), 1, border_radius=1)
            # Bottom-Left
            pygame.draw.rect(surface, color, (x - gap - sq_size, y + gap, sq_size, sq_size), 1, border_radius=1)
            # Bottom-Right
            pygame.draw.rect(surface, color, (x + gap, y + gap, sq_size, sq_size), 1, border_radius=1)
            
        elif name == "bw":
            # B/W Toggle
            label_col = GRAY
            text = self.small_font.render("B/W", True, label_col)
            text_rect = text.get_rect(center=(x, y - 10))
            surface.blit(text, text_rect)
            
            # Simple camera icon or circle split
            cam_y = y + 10
            cam_w = 20
            cam_h = 14
            pygame.draw.rect(surface, color if not active else YELLOW, (x - cam_w - 5, cam_y - cam_h//2, cam_w, cam_h), 1, border_radius=2)
            pygame.draw.circle(surface, color if not active else YELLOW, (x - cam_w - 5 + cam_w//2, cam_y), 4, 1)
            
            # Contrast/Moon icon
            radius = 7
            cx = x + 10
            pygame.draw.circle(surface, color if not active else YELLOW, (cx, cam_y), radius, 1)
            # Fill half manually for moon effect
            pygame.draw.arc(surface, color if not active else YELLOW, (cx-radius, cam_y-radius, radius*2, radius*2), 1.57, 4.71, 100)

    def draw_gallery_ui(self):
        """Gallery UI con rotazione"""
        gallery_surf = pygame.Surface((self.width, self.height))
        gallery_surf.fill(BLACK)
        
        if not self.photos:
            text = self.font.render("Nessuna foto", True, WHITE)
            text_rect = text.get_rect(center=(self.width // 2, self.height // 2))
            gallery_surf.blit(text, text_rect)
        else:
            try:
                # Load photo with proper aspect ratio preservation
                photo_path = self.photos[self.gallery_index]
                img = Image.open(photo_path)
                
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
                img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                
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
                
        # Counter
        counter = f"{self.gallery_index + 1} / {len(self.photos)}"
        counter_surf = self.small_font.render(counter, True, WHITE)
        gallery_surf.blit(counter_surf, (self.width // 2 - counter_surf.get_width() // 2, 10))
        
        # Back button with icon
        pygame.draw.rect(gallery_surf, GREEN, self.back_btn_rect, border_radius=5)
        if "back" in self.icons:
            icon_rect = self.icons["back"].get_rect(center=self.back_btn_rect.center)
            gallery_surf.blit(self.icons["back"], icon_rect)
        else:
            self.draw_button_on_surface(gallery_surf, self.back_btn_rect, "BACK", GREEN)

        # Trash button (Top Right)
        pygame.draw.rect(gallery_surf, RED, self.trash_btn_rect, border_radius=5)
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

                # B/W Effect button (Bottom)
                if self.bw_rect.collidepoint(x, y):
                    self.grayscale_mode = not self.grayscale_mode
                    print(f"B/W mode: {self.grayscale_mode}")
                    self.apply_camera_settings()
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
                
                pygame.display.flip()
                clock.tick(30)
                
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Cleanup"""
        print("Shutting down...")
        try:
            self.camera.stop()
        except:
            pass
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
