# Raspberry Pi HQ Camera Application

A lightweight camera application for Raspberry Pi with HQ Camera module, featuring touchscreen interface, WiFi hotspot capability, and remote web control.

## ✨ Features

- 🎥 **Live Camera Preview** with hardware acceleration
- 📸 **Photo Capture** via GPIO button or touchscreen
- 🎛️ **Manual Controls** for ISO (100-3200) and Shutter Speed (1/4000s - 1/4s)
- 🖼️ **Built-in Gallery** with photo browsing and deletion
- 🌐 **Web Server** for remote access and camera control
- 📱 **Live Preview Stream** (MJPEG) accessible from any browser
- 🔋 **Battery Indicator** (compatible with UPS-Lite V1.3)
- ⚡ **Power Management** with shutdown, standby, and display control
- 🌳 **WiFi Hotspot Mode** for outdoor/portable use
- 🔌 **WiFi Manager** to connect to networks from the UI
- 🎨 **Material Design 3** web interface

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
4. Setting up directories and permissions
5. Configuring systemd services
6. Downloading UI icons
7. Configuring auto-login (optional)
8. Configuring camera app autostart (optional)
9. Configuring WiFi hotspot (optional)

### 5. Reboot

After installation completes, reboot your Raspberry Pi:

```bash
sudo reboot
```

## 🎮 Usage

### Camera Interface

After reboot, the application starts automatically showing:

- **Live Camera Preview** - Full-screen camera feed
- **POWER Button** ⚡  - Access power menu
- **GALLERY Button**  - View captured photos
- **ISO Controls** (Auto, 100-3200) - Adjust with ▲/▼ buttons
- **Shutter Speed Controls** (Auto, 1/4000s - 1/4s) - Adjust with ▲/▼ buttons
- **Battery Indicator** (top-right) - Shows battery percentage if UPS is connected

### Power Menu

Tap the POWER button to access the menu with these options:

- **SHUTDOWN** - Power off the Raspberry Pi completely
- **STANDBY** - Low-power mode with display off (wake with touch/mouse)
- **MONITOR OFF** - Turn off display backlight (wake with touch/mouse)
- **START/STOP HOTSPOT** - Toggle WiFi hotspot mode
- **WIFI** - Connect to WiFi networks
- **SERVER** - Start/stop the web server
- **CANCEL** - Close the menu

### Taking Photos

**GPIO Button (Pin 26)** - Press the physical button

Photos are saved to `~/photos/` with format: `photo_YYYYMMDD_HHMMSS.jpg`

### Photo Gallery

Tap the "GALLERY" button to view your photos:
- **PREV/NEXT** - Navigate between photos
- **DELETE** - Remove the current photo
- **BACK** - Return to camera mode

### Web Server

The web server starts automatically on port 8080 and provides:

1. **Photo Gallery** at `http://YOUR_PI_IP:8080`
   - Responsive grid layout with thumbnails
   - Date and time stamps
   - Full-screen lightbox view
   - Auto-refresh every 30 seconds
   - Download or delete photos

2. **Live Remote Control** at `http://YOUR_PI_IP:8080/live`
   - Live MJPEG preview stream
   - Remote capture button
   - ISO and shutter speed adjustment
   - Real-time camera status

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

1. Press **POWER** → tap **START HOTSPOT** (purple button)
2. Wait a few seconds for the network to initialize
3. From your smartphone/PC, connect to the WiFi network:
   - **SSID**: `RaspiCam`
   - **Password**: `raspicam_admin`
4. Open browser and navigate to: `http://10.42.0.1:8080`

**To return to home WiFi:**
Press **POWER** → **STOP HOTSPOT** (orange button)

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

### Display Shows Nothing

1. **Verify framebuffer:**
   ```bash
   ls -l /dev/fb0
   ```

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

3. **Check touch overlay in `/boot/config.txt`:**
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

Or manually restore KMS drivers in `/boot/config.txt`:
```bash
sudo nano /boot/config.txt
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
- **Display Backlight:** Uses sysfs backlight control (compatible with KMS/DRM drivers)
- **Wake from Standby:** Display automatically wakes on touch or mouse movement
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

## 📄 Project Structure

```
~/raspi/
├── camera_app.py       # Main camera application
├── photo_server.py     # Web server for photo gallery and remote control
├── setup.sh            # Complete installation script
├── assets/             # UI icons (downloaded during setup)
│   ├── icon_gallery.png
│   ├── icon_power.png
│   ├── icon_iso.png
│   ├── icon_shutter.png
│   └── ...
└── ~/photos/           # Photo storage directory (created automatically)
```

## 🚀 Quick Reference

| Action | Command |
|--------|---------|
| Start camera manually | `cd ~/raspi && python3 camera_app.py` |
| View server logs | `sudo journalctl -u photo-server -f` |
| Check WiFi IP | `hostname -I` |
| Enable hotspot | Power menu → START HOTSPOT |
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
6. **Display Orientation:** Adjust in `/boot/config.txt` with `display_rotate=X` (0,1,2,3)

## 🤝 Contributing

This is a personal project, but feel free to fork and modify for your needs!

## 📄 License

This project is provided "as-is" for personal use.

---

**Happy photographing! 📸✨**


DONATE
If you’d like to support me and my work, please consider making a donation here:

SATISPAY: https://web.satispay.com/download/qrcode/S6Y-SVN--D276047B-5BC4-45FD-AC89-D78E7579EC4A?locale=it
