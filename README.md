# Raspberry Pi HQ Camera Application

A lightweight camera application for Raspberry Pi with HQ Camera module, featuring touchscreen interface, WiFi hotspot capability, and remote web control.

[![Look My video (You can put english with autotransaltion)](https://img.youtube.com/vi/JkhWkDddtTM/hqdefault.jpg)](https://youtu.be/JkhWkDddtTM)

## ✨ Features

- 🎥 **Live Camera Preview** at 30 fps, ~65 ms from sensor to screen
- 📸 **Photo Capture** via GPIO button, touchscreen or phone
- 🎛️ **Manual Controls** for ISO (100-6400) and Shutter Speed (1/2000s - 1/4s)
- 🎯 **Focus Assist**: focus peaking that lights up only what is really sharp,
  a 3x/6x magnifier around any point you tap, and a focus meter
- 🖼️ **Built-in Gallery** with photo browsing and deletion
- 📱 **Web Remote**: live view, shutter, ISO/shutter pickers, focus assist,
  standby and wake from any phone or computer — and a web gallery with
  multi-select download and delete
- 🔳 **QR codes** on the camera screen to open the remote (and to join the
  hotspot) from your phone
- 🔋 **Battery Indicator** (compatible with UPS-Lite V1.3)
- ⚡ **Power Management** with shutdown, reboot and standby
- 🌳 **WiFi Hotspot Mode** for outdoor/portable use
- 🔌 **WiFi Manager** to connect to networks from the UI

## 🔧 Hardware Requirements

- Raspberry Pi (Zero 2W or newer recommended):https://amzn.to/4kIDnxV
- Raspberry Pi HQ Camera Module: https://amzn.to/3Of9oSp
- Display with touchscreen support and hdmi(any resolution):https://it.aliexpress.com/item/1005006948147325.html?spm=a2g0o.order_list.order_list_main.21.72883696ciHuuQ&gatewayAdapt=glo2ita
- Hdmi connector: https://it.aliexpress.com/item/1005008922900022.html?spm=a2g0o.order_list.order_list_main.15.72883696ciHuuQ&gatewayAdapt=glo2ita
- Push button connected to GPIO 26 and GND: https://it.aliexpress.com/item/1005004784681487.html?spm=a2g0o.order_list.order_list_main.102.72883696ciHuuQ&gatewayAdapt=glo2ita
- UPS-Lite V1.3 or compatible battery (optional)
- Raspberry Pi OS Trixie Lite (or newer)

## 📦 Installation

### 1. Transfer Files to Raspberry Pi

Copy all project files to your Raspberry Pi. From your computer terminal:

```bash
cd /path/to/raspi/folder
scp camera_app.py photo_server.py setup.sh your_username@YOUR_PI_IP:~/raspi/
```

Example:
```bash
scp camera_app.py photo_server.py setup.sh pi@192.168.1.100:~/raspi/
```

### 2. SSH into Raspberry Pi

Connect to your Raspberry Pi via SSH:

```bash
ssh your_username@YOUR_PI_IP
```

Example:
```bash
ssh pi@192.168.1.100
```

### 3. Display Driver Installation (Optional)

If you're using a 3.5" SPI display (like MPI3508), install the display drivers:

```bash
sudo apt-get install python3-evdev
sudo rm -rf LCD-show
sudo apt install git
git clone https://github.com/goodtft/LCD-show.git
chmod -R 755 LCD-show
cd LCD-show/
sudo ./MPI3508-show  # Replace with your display model
```

**Note:** After driver installation, the system will reboot automatically.

### 4. Run Setup Script

Navigate to the project directory and run the setup script:

```bash
cd ~/raspi
chmod +x setup.sh
./setup.sh
```

The setup script will guide you through:
1. Installing system dependencies
2. Configuring boot settings and GPU memory
3. Fixing display issues (optional)
4. Kernel command line (quiet boot, 32 bpp framebuffer, 720x480 on the
   MPI3508 display) and on-demand mount of the boot partition
5. Setting up directories and permissions
6. Configuring systemd services
7. Configuring auto-login (optional)
8. Configuring camera app autostart (optional)
9. Disabling services that slow down the boot (optional)
10. Configuring WiFi hotspot (optional)

See [Boot Time](#-boot-time) for what the boot-related steps do and why.

### Updating an installed camera

Copy the new files over and restart the app (the web server only if
`photo_server.py` changed):

```bash
scp camera_app.py photo_server.py setup.sh README.md your_username@YOUR_PI_IP:~/raspi/
```

```bash
ssh your_username@YOUR_PI_IP "sudo systemctl restart camera-ui photo-server"
```

System-level changes (boot configuration, service files) are applied by
re-running `./setup.sh` on the Pi.

### 5. Reboot

After installation completes, reboot your Raspberry Pi:

```bash
sudo reboot
```

## 🎮 Usage

### Camera Interface

After reboot, the application starts automatically showing:

- **Live Camera Preview** - 4:3 camera feed between the two sidebars
- **SHUTTER / ISO** (left) - Adjust with ▲/▼; in Auto the live values are
  shown in amber
- **POWER Button** ⚡ (right) - Opens the settings menu
- **GALLERY Button** - View captured photos
- **B/W** - Black and white preview and photos
- **PEAK** - Focus assist (see below)
- **Battery Indicator** (top-right) - Shows battery percentage if UPS is connected
- **TELECOMANDO** chip (above the image) - A phone is using the web remote

### Focus Assist

Tap **PEAK** for manual focusing:

- **Peaking** paints the edges that are in focus. A turn of the focus ring
  past the sharp point makes them disappear: out-of-focus outlines stay dark,
  and noise does not light up at high ISO.
- **Tap the image** to magnify that point 3x (tap again to move the window);
  the **×1/×3/×6** chip cycles the magnification, with a map of where you are.
  At 3x one screen pixel is about one sensor pixel, which is where fine focus
  can actually be judged. Photos are always the full frame.
- **FOCUS meter**: turn the ring until the bar reaches the white marker (the
  best value seen in the last few seconds); tap it to start over.
- The **colour** chip cycles red, yellow, cyan and white; the **bars** chip
  sets the sensitivity (low, medium, high).

The web remote has the same controls.

### Settings Menu

Tap the POWER button to open the settings:

- **Server web**, **Hotspot** - Tap to switch on or off (state shown live)
- **Wi-Fi** - Shows the current network; tap to scan and connect. Saved
  networks connect directly; for the others a full keyboard (letters,
  numbers, all symbols, show/hide password) asks for the password
- **Standby** - Display off and sensor stopped; network and web server stay
  on. Wake with a touch, the shutter button or the web remote
- **Riavvia** / **Spegni** - Reboot / shut down, after a confirmation
- **Telecomando** card - The address of the web remote with a QR code: point
  your phone's camera at it. With the hotspot on there are two codes: the
  first joins the hotspot's Wi-Fi, the second opens the page

### Taking Photos

**GPIO Button (Pin 26)** - Press the physical button

Photos are saved to `~/photos/` with format: `photo_YYYYMMDD_HHMMSS.jpg`

### Photo Gallery

Tap the "GALLERY" button to view your photos (newest first):
- **Tap the left/right third** - Previous/next photo
- **Trash** - Delete the current photo, after a confirmation
- **Back** - Return to camera mode

Each photo shows when it was taken, its shutter speed and ISO.

### Web Server

The web server starts automatically on port 8080 and provides:

1. **Photo Gallery** at `http://YOUR_PI_IP:8080`
   - Thumbnails grouped by day (Oggi, Ieri, ...), generated on the Pi and
     cached, so the grid loads in moments instead of downloading every 2 MB
     original
   - Full-screen viewer: swipe or arrow keys, shutter speed / ISO / size,
     download, delete
   - Select several photos to download them as one ZIP or delete them
   - New photos appear by themselves; camera status, free space, standby,
     reboot and shutdown from the power button

2. **Remote Control** at `http://YOUR_PI_IP:8080/live`
   - Live preview with a rule-of-thirds grid
   - Shutter button with flash, saving indicator and the last photo's thumbnail
   - Shutter and ISO as tappable rows of values; live Auto readings
   - B/N and Focus assist (peaking, magnifier: tap the image, focus meter)
   - Wakes the camera from standby
   - Portrait, landscape (controls on the side), tablet and desktop layouts

Both pages work offline (no external fonts or scripts), so they also work on
the hotspot.

**To find your Raspberry Pi's IP address:**
```bash
hostname -I
```

**Example access:**
```
http://192.168.1.100:8080         # Photo gallery
http://192.168.1.100:8080/live    # Live remote control
```

### WiFi Hotspot Mode (Outdoor/Portable Use) 🌳

For use without a WiFi router (outdoor photography, events, etc.):

1. Press **POWER** → tap the **Hotspot** tile
2. Wait a few seconds for the network to initialize
3. Scan the first QR code on the camera screen with your phone to join the
   network, or connect by hand:
   - **SSID**: `RaspiCam`
   - **Password**: `raspicam_admin`
4. Scan the second QR code, or open `http://10.42.0.1:8080/live`

**To return to home WiFi:**
Press **POWER** → tap the **Hotspot** tile again

## 🔌 GPIO Wiring

```
Raspberry Pi GPIO 26 ──────┐
                           │
                      [Button]
                           │
Raspberry Pi GND ──────────┘
```

**Alternative GPIO Configuration:**
You can use any GPIO pin by editing `camera_app.py`:
```python
GPIO_BUTTON_PIN = 26  # Change to your desired pin
```

## 🛠️ Service Management

### Check Service Status

```bash
# Photo server
sudo systemctl status photo-server

# Camera UI (if autostart is enabled)
sudo systemctl status camera-ui
```

### Manual Start/Stop

```bash
# Start services
sudo systemctl start photo-server
sudo systemctl start camera-ui

# Stop services
sudo systemctl stop photo-server
sudo systemctl stop camera-ui
```

### View Logs

```bash
# Photo server logs
sudo journalctl -u photo-server -f

# Camera UI logs
sudo journalctl -u camera-ui -f
```

### Disable Autostart

```bash
sudo systemctl disable camera-ui
sudo systemctl disable photo-server
```

### Manual Camera Launch

If autostart is disabled, you can run the camera manually:

```bash
cd ~/raspi
python3 camera_app.py
```

## 🎨 Customization

### Change Web Server Port

Edit `photo_server.py`:
```python
PORT = 8080  # Change to your desired port
```

Then restart the service:
```bash
sudo systemctl restart photo-server
```

### Change GPIO Pin

Edit `camera_app.py`:
```python
GPIO_BUTTON_PIN = 26  # Change to your desired pin
```

### Change Photo Directory

Edit both `camera_app.py` and `photo_server.py`:
```python
PHOTOS_DIR = Path.home() / "photos"  # Change path
```

### Change Hotspot Credentials

Edit `setup.sh` before installation:
```bash
HOTSPOT_SSID="RaspiCam"          # Network name
HOTSPOT_PASSWORD="raspicam_admin"  # Password (min 8 chars)
```

Then reconfigure:
```bash
./setup.sh  # Only answer 'y' to Step 7 (hotspot)
```

### Customize Preview Resolution

Edit `camera_app.py`:
```python
# Camera preview configuration
self.picam2.configure(self.picam2.create_preview_configuration(
    main={"size": (640, 480), "format": "RGB888"}  # Change size
))
```

## 🐛 Troubleshooting

### Camera Doesn't Start

1. **Enable camera interface:**
   ```bash
   sudo raspi-config
   # Navigate to: Interface Options → Camera → Enable
   ```

2. **Check camera connection:**
   ```bash
   libcamera-hello --list-cameras
   ```

3. **View error logs:**
   ```bash
   sudo journalctl -u camera-ui -n 50
   ```

### "Fotocamera non risponde" on the Viewfinder

The sensor has stopped sending frames. The journal shows `Camera frontend has
timed out! ... check that your camera sensor connector is attached securely`:

```bash
journalctl -u camera-ui -b | grep -iE "frontend|No frame|recovered"
```

This is the CSI link, not the software (the sensor still answers on I2C, but no
image data arrives). Power off, reseat the ribbon cable at both ends in the
same orientation and check it for creases; try another cable if it keeps
happening.
The app keeps running meanwhile (menu, gallery and shutdown work) and restarts
the camera every 10 s, so the image comes back on its own if the contact
recovers. Before, the whole interface froze until a reboot.

### Display Backlight Stays On in Standby

Standby switches the HDMI signal off (and any backlight the kernel can
control). The 3.5" MPI3508 has no software-controllable backlight and keeps
it lit without a signal: hold its backlight key for 3 seconds to turn it off
(a short press turns it back on). Standby still stops the sensor and slows
the CPU down.

### Image Looks Stretched on the 3.5" Display

The MPI3508's panel is 480x320 (3:2) and it scales whatever it receives to
fill it, so a 4:3 mode such as 640x480 is stretched sideways by 12.5%. Use a
3:2 mode, which `setup.sh` (Step 1.7) sets when it finds this display:

```bash
grep -o "video=[^ ]*" /boot/firmware/cmdline.txt
```

It should read `video=HDMI-A-1:720x480M-32@60`; change it and reboot if it
does not. 720x480 scales evenly by 2/3. The interface adapts to the
resolution.

### Display Shows Nothing

1. **Verify framebuffer:**
   ```bash
   ls -l /dev/fb0
   ```

   The app draws straight into `/dev/fb0`. It should belong to the KMS driver
   (`vc4drmfb`, not `simple`) and the log should say
   `Display initialized on /dev/fb0`:
   ```bash
   cat /sys/class/graphics/fb0/name
   ```

   To use SDL's KMSDRM output instead (slower to start), add
   `Environment=CAMERA_DISPLAY=sdl` to `camera-ui.service`.

2. **Test manually:**
   ```bash
   cd ~/raspi
   python3 camera_app.py
   ```

3. **Check display driver:**
   ```bash
   dmesg | grep -i display
   ```

### Touch Not Working

1. **Verify touch device:**
   ```bash
   ls /dev/input/event*
   evtest  # Then select your touch device
   ```

2. **Calibrate touch (if needed):**
   ```bash
   sudo apt install xinput-calibrator
   xinput_calibrator
   ```

3. **Check touch overlay in `/boot/firmware/config.txt`:**
   ```
   dtoverlay=ads7846,cs=1,penirq=25,speed=50000...
   ```

### GPIO Button Doesn't Work

1. **Verify wiring** (GPIO 26 to button, button to GND)

2. **Test the pin:**
   ```bash
   python3 -c "from gpiozero import Button; b = Button(26); print('Press button...'); b.wait_for_press(); print('Pressed!')"
   ```

3. **Check GPIO permissions:**
   ```bash
   groups  # Should include 'gpio'
   sudo usermod -a -G gpio $USER
   ```

### Web Server Not Accessible

1. **Verify service is running:**
   ```bash
   sudo systemctl status photo-server
   ```

2. **Check firewall (if configured):**
   ```bash
   sudo ufw allow 8080
   ```

3. **Verify Raspberry Pi IP:**
   ```bash
   hostname -I
   ```

4. **Test locally:**
   ```bash
   curl http://localhost:8080
   ```

### Permission Errors

**"Permission denied" for shutdown/power control:**
```bash
sudo visudo
# Add at the end:
your_username ALL=(ALL) NOPASSWD: /sbin/shutdown
your_username ALL=(ALL) NOPASSWD: /usr/bin/vcgencmd
```

**"Permission denied" for GPIO:**
```bash
sudo usermod -a -G gpio,input,video $USER
sudo reboot
```

### Display Fix After LCD-show Drivers

If you previously used LCD-show drivers and the camera doesn't work:

```bash
cd ~/raspi
./setup.sh  # Answer 'y' to Step 1.6 (display fix)
```

Or manually restore KMS drivers in `/boot/firmware/config.txt`:
```bash
sudo nano /boot/firmware/config.txt
# Uncomment or add:
dtoverlay=vc4-kms-v3d,noaudio
gpu_mem=128

sudo reboot
```

### Battery Indicator Shows 0%

1. **Verify UPS-Lite connection** (I2C on GPIO 2/3)

2. **Check I2C is enabled:**
   ```bash
   sudo raspi-config
   # Interface Options → I2C → Enable
   ```

3. **Test I2C communication:**
   ```bash
   sudo apt install i2c-tools
   sudo i2cdetect -y 1
   # Should show device at address 0x62
   ```

4. **Install python3-smbus:**
   ```bash
   sudo apt install python3-smbus
   ```

## 📝 Notes

- **Display Resolution:** The UI automatically adapts to your display resolution
- **Photo Quality:** Images are saved in high-quality JPEG format
- **Network Security:** Web server is only accessible on the local network (not from the Internet)
- **Auto-login Security:** Auto-login disables password prompt at boot. Only use on devices in controlled environments.
- **Power Management:** The setup script automatically configures sudo permissions for shutdown and display control
- **Display Backlight:** Standby powers the video output down through KMS and switches sysfs backlights (DSI/SPI panels) off; HDMI panels like the MPI3508 keep their own backlight on (see Troubleshooting)
- **Wake from Standby:** Touch, the shutter button, or Risveglia on the web remote
- **Standby and the network:** The web server and the hotspot stay up in standby, so photos can still be downloaded. Raspberry Pi kernels have no system sleep state, so this is as deep as standby gets
- **Hotspot IP:** When hotspot is active, the Pi is always accessible at `10.42.0.1`

## 🔒 Security Considerations

⚠️ **Important Security Notes:**

1. **Auto-login:** Disables password requirement at boot. Only use on dedicated camera devices in secure locations.
2. **Web Server:** No authentication by default. Anyone on the same network can view/delete photos.
3. **Hotspot:** Uses WPA2-PSK encryption. Change default password in `setup.sh` before installation.
4. **Sudo Permissions:** The app has passwordless sudo for specific commands (shutdown, network management). Review `/etc/sudoers.d/camera-app` if concerned.

**For enhanced security:**
- Change hotspot password to something strong and unique
- Add HTTP authentication to `photo_server.py`
- Use a firewall to restrict port 8080 access
- Disable autostart and run the camera app manually when needed

## 📄 Case?

I haven’t built a proper case yet. At the moment the prototype is made of cardboard, but it’s neither ergonomic nor does it help with heat dissipation. I’m evaluating different construction materials, and I think the next step will be wood.

Ideally, it should be 3D printed with a specific form factor because, without an optical viewfinder, it would be great to tilt the display like in CinePi, but in a much more compact design. If anyone is skilled in design, feel free to share your projects!


## 🚀 Quick Reference

| Action | Command |
|--------|---------|
| Start camera manually | `cd ~/raspi && python3 camera_app.py` |
| View server logs | `sudo journalctl -u photo-server -f` |
| Check WiFi IP | `hostname -I` |
| Enable hotspot | Power menu → Hotspot tile |
| Access local gallery | `http://YOUR_IP:8080` |
| Access hotspot gallery | `http://10.42.0.1:8080` |
| Remote control | `http://YOUR_IP:8080/live` |
| Shutdown from CLI | `sudo shutdown -h now` |

## 💡 Tips & Best Practices

1. **Photo Backup:** Regularly backup `~/photos/` to external storage
2. **Long Exposures:** Use a tripod for shutter speeds slower than 1/60s
3. **Remote Control:** Use the `/live` page to avoid touching the camera during capture
4. **Battery Life:** Enable standby mode when not in use to save power
5. **Network Switch:** Hotspot takes ~10 seconds to activate/deactivate
6. **Display Orientation:** Adjust in `/boot/firmware/config.txt` with `display_rotate=X` (0,1,2,3)

## ⚡ Boot Time

From power-on to a live viewfinder went from about **70 s to about 18 s** on a
Pi Zero 2W with the HQ camera and a 3.5" HDMI display (Raspberry Pi OS Trixie
Lite). Times below are from the start of the kernel, averaged over two reboots;
the firmware adds about 5 s before that on a reboot (10.4 s before the
changes, measured on a cold power-on, which is a little slower).

| milestone | before | after |
|---|---|---|
| systemd starts (end of kernel stage) | 11.1 s | **3.0 s** |
| `camera-ui` started | 30.6 s | **6.8 s** |
| camera streaming | 61.5 s | **12.6 s** |
| **first frame on screen** | **~62 s** | **13.4 s** |
| network, SSH and web server up (`multi-user.target`) | 49.9 s | **19.1 s** |

### Measuring

The app logs the number that matters, the first frame on screen relative to
the kernel start:

```bash
journalctl -u camera-ui -b | grep -E "First frame|Camera started|Display initialized"
```

```bash
systemd-analyze
```

```bash
systemd-analyze critical-chain camera-ui.service
```

The firmware stage is in the VideoCore log (`Starting ARM` is when the kernel
takes over):

```bash
sudo vclog --msg | grep -E "kernel8|Starting ARM"
```

Do not measure while logged in over SSH during the boot: on a Zero 2W an SSH
login (key exchange, PAM, a `systemd --user` instance) competes with the app
for CPU and the SD card and skews the numbers by several seconds.

### Where the time went, and what changed

The SD card is the bottleneck: the app pages in ~50 MB of Python modules and
shared libraries at a few MB/s while the rest of the system boots.

**Firmware / kernel** (Step 1.5, 1.7)
- `auto_initramfs=0` — the initramfs only redid what the kernel can already do
  (SD card and ext4 are built in). The firmware spent ~1.4 s reading its 12 MB,
  and its scripts ran for ~5 s before the root filesystem was even mounted.
  Applied only when root is ext4 on the SD card.
- `cmdline.txt` back on **one line**: the firmware reads only the first line, so
  a `quiet` added on a second line had been silently ignored, and every kernel
  and systemd message went to the serial console at 115200 baud (~10 ms a line)
  and to the screen. With the initramfs gone, the kernel stage went from 11.1 s
  to 3.0 s.
- `quiet loglevel=3 logo.nologo vt.global_cursor_default=0` (now the default
  answer).
- `video=...-32`: the framebuffer is created at 32 bpp, which the app now draws
  into directly (see below). On the MPI3508 the mode is 720x480 (see
  Troubleshooting).
- Unchanged: `boot_delay=0`, `disable_splash=1`, `initial_turbo=30`.

**systemd** (Step 1.8, 6, 6.5)
- `camera-ui.service` uses `DefaultDependencies=no` and starts once the root
  filesystem is writable, before `sysinit.target`: swap setup (which `/tmp`
  waits for), tmpfiles, sockets and timers are not needed for the viewfinder.
- `IOSchedulingClass=realtime`: the app's reads go ahead of NetworkManager,
  avahi, dbus etc. (the scheduler is mq-deadline). The network comes up a few
  seconds after the viewfinder instead of competing with it.
- `/etc/modules-load.d/camera-ui.conf` loads the display and camera drivers
  (`vc4`, `bcm2835_unicam_legacy`, `imx477`, `bcm2835_isp` and the I2C
  controller/mux both depend on) before udev's coldplug, so they are bound
  ~4 s earlier, before the app needs them.
- `/boot/firmware` is mounted on first access (`x-systemd.automount`) and not
  fsck'd: the boot used to wait for its device, a 3 s FAT check and the mount.
- Disabled on this hardware: `e2scrub_reap` (10 s every boot, only useful with
  LVM), `dpkg-db-backup`/`man-db`/`e2scrub_all` timers, `rpi-eeprom-update`
  (no EEPROM on Pi Zero/3), `NetworkManager-wait-online`, `getty@tty1`.
- Volatile journal; stop timeout capped at 10 s (the app used to take the full
  90 s to stop when the sensor had hung).

**Application** (`camera_app.py`)
- **Direct framebuffer output.** SDL's KMSDRM backend always initialises
  OpenGL ES through Mesa, whose driver library links LLVM: ~70 MB read from the
  SD card and ~9 s before the first `set_mode()` returned. The UI is drawn with
  SDL's `dummy` driver and copied into `/dev/fb0` instead (0.1 s to set up,
  2-3 ms per frame); the text console is switched to graphics mode so it cannot
  draw over it. The KMSDRM path is kept as an automatic fallback
  (`CAMERA_DISPLAY=sdl` forces it).
- pygame's unused optional modules (mixer, imageext, scrap, ...) are not
  imported: they loaded SDL_mixer and SDL_image with all their codecs. Icons are
  decoded with PIL, which picamera2 has already loaded.
- picamera2 is imported only by the camera thread, so the splash is up while
  libcamera is still loading; evdev, smbus and gpiozero are imported in the
  background after the first frame.
- libcamera lists cameras once, when the first `Picamera2()` is created, and a
  sensor that binds later is never seen. The app now waits until the camera
  pipeline and the sensor are registered and accessible before that first
  call. (Before, an early start could leave it without a camera until systemd
  restarted it.) The framebuffer, touch panel, GPIO chip and I2C bus are
  likewise waited for or retried rather than given up on.
- Run as `python3 -c "import camera_app; ..."` so its bytecode is cached.
- Startup logs are line-buffered: previously Python held them back until ~8 KB
  had accumulated, so none of the timings above reached the journal.

### Reverting

Every system file is backed up the first time setup.sh changes it:

```bash
sudo cp /boot/firmware/config.txt.bak_setup /boot/firmware/config.txt
```

```bash
sudo cp /boot/firmware/cmdline.txt.bak_setup /boot/firmware/cmdline.txt
```

```bash
sudo cp /etc/fstab.bak_setup /etc/fstab
```

```bash
sudo rm /etc/modules-load.d/camera-ui.conf
```

Disabled services come back with `sudo systemctl enable --now <unit>`.

## 🎨 Interface

The UI was restyled without changing any behaviour: every touch target in
`setup_ui()` keeps its exact geometry, and no control was added, removed or
rewired.

- **Typography** — FreeSans / FreeSansBold replace pygame's built-in bitmap
  face. Sizes are multiplied by `FONT_SCALE` (0.78) because a TrueType face at
  size N renders much larger than `Font(None, N)`; the factor was measured
  on-device so string widths stay within ~3% of the old font and cannot
  overflow the unchanged button rects.
- **Palette** — design tokens (`SURFACE_0…3`, `TEXT_HI/MID/LOW`, `ACCENT`,
  `SUCCESS`, `DANGER`, `INFO`) replace ad-hoc RGB literals. Amber is the single
  accent marking active states (B/W, focus peaking, shift, live AE readback).
- **Camera HUD** — ISO and shutter sit on raised cards; chevrons replace filled
  triangles; the battery is a pill gauge instead of a bare percentage.
- **Popups** — one shared scrim/panel helper, filled pill buttons without the
  dated 1px white outline, WiFi rows as cards with a vector padlock.
- **Keyboard** — shift and backspace are drawn as vectors. Neither FreeSansBold
  nor the previous default font actually carries `⇧`/`⌫`, so they had been
  rendering as tofu boxes.

Two cosmetic bugs were fixed in passing: the tofu keys above, and the gallery
counter reading `1 / 0` when no photos exist.

### Rendering cost

Measured on the Pi Zero 2W at 640x480 with a synthetic sensor, 5 alternating
runs (spread under 0.3%):

| | before | after |
|---|---|---|
| `draw_camera_ui` | 7.95 ms | **7.27 ms** |
| full frame | 26.7 ms | 27.5 ms |

Drawing got cheaper despite the richer visuals because static labels and HUD
strings are now cached instead of re-rendered per glyph every frame. Both
figures stay under the 33.3 ms budget for the app's 30 fps target.

### Menus, focus assist and viewfinder latency (October 2026)

- **Menus** — a settings screen in the style of a phone's control centre over
  a blurred copy of the viewfinder: toggle tiles that show their live state
  and a spinner while systemctl/nmcli run (in the background: the UI no
  longer freezes for seconds), confirmation before reboot, shutdown and
  delete, toasts for results, and QR codes for the web remote. Icons are
  vector-drawn 4x oversize and scaled down, so they are anti-aliased. Menus
  are rendered once per change, not every frame.
- **Wi-Fi** — list with signal icons, saved networks connect without a
  password, pages for long lists; the keyboard has every ASCII symbol (the old
  one had none, so many passwords could not be typed), caps lock, and
  show/hide password. Connecting no longer blocks the UI.
- **Focus peaking** — second derivatives of a lightly smoothed luminance,
  thresholded at a sensitivity level or above the measured noise floor. The
  old filter (Laplacian, 97th-percentile threshold) always lit 3% of the
  frame, sharp or not, and wrapped edges around the borders. It runs on its
  own core and costs the viewfinder ~2 ms instead of ~70 ms.
- **Gallery** — photos are decoded at screen size with JPEG draft mode and
  cached with their neighbours; the old gallery decoded the full 12 MP file
  on every frame.
- **Standby** — kept the web server stopped after waking (it never restarted
  it), tried sleep states the kernel does not have, and wrote GPIO/LED paths
  that no longer exist on kernel 6.12. Now: HDMI output off through KMS,
  sensor stopped, `powersave` governor, slow LED blink; everything is put back
  on wake, at the next start if the app was killed in standby, and on
  `systemctl stop` (SIGTERM now runs the cleanup).
- **Display mode** — 720x480 instead of 640x480 on the MPI3508, whose
  480x320 panel is 3:2 (see Troubleshooting). The viewfinder grew from
  354x236 to 353x265 panel pixels with the right proportions.

Viewfinder measured on the Pi Zero 2W with the real camera: time from the
sensor timestamp to the frame being in the framebuffer (`SensorTimestamp` vs
the copy into `/dev/fb0`), 20 s per run:

| | fps | sensor → screen, mean / p95 |
|---|---|---|
| before, 640x480 | 21 | 89 / 106 ms |
| before, 640x480, peaking on | 6.6 | ~195 ms |
| **after, 720x480** | **30** | **65 / 67 ms** |
| **after, 720x480, peaking + web remote** | **30** | **74 / 80 ms** |

About 47 ms of that is the sensor readout and the ISP, before the app sees the
frame. What changed in the app:

- frames are handed to pygame as BGR (`frombuffer(..., "BGR")`); the
  `frame[:, :, ::-1]` copy cost ~15 ms per frame
- the remote's JPEG is encoded on a worker thread, and only while a page or
  stream is open
- exposure and gain are read from the viewfinder's own request: the
  separate `capture_metadata()` thread took a frame away every 0.4 s
- three camera buffers instead of two: picamera2 keeps the latest frame
  aside, which left the sensor short of buffers (23 fps with peaking on)
- no 30 fps tick on top of the camera's own pacing

## 🤝 Contributing

This is a personal project, but feel free to fork and modify for your needs!

## 📄 License

This project is provided "as-is" for personal use.

---

**Happy photographing! 📸✨**


DONATE
If you’d like to support me and my work, please consider making a donation here:

SATISPAY: https://web.satispay.com/download/qrcode/S6Y-SVN--D276047B-5BC4-45FD-AC89-D78E7579EC4A?locale=it
Paypal: https://www.paypal.com/pool/9mSjn8OiuS?sr=wccr
