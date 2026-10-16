#!/bin/bash
# Complete Setup Script for Pi Camera Application
# This script combines installation, autostart, and hotspot configuration

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
HOTSPOT_NAME="RaspiCam_Hotspot"
HOTSPOT_SSID="RaspiCam"
HOTSPOT_PASSWORD="raspicam_admin"

# Firmware config location. Bookworm/Trixie mount the boot partition at
# /boot/firmware; only pre-Bookworm images use /boot directly. Writing to the
# wrong one silently creates a file the firmware never reads.
if [ -f /boot/firmware/config.txt ]; then
    BOOT_CONFIG="/boot/firmware/config.txt"
    BOOT_CMDLINE="/boot/firmware/cmdline.txt"
elif [ -f /boot/config.txt ]; then
    BOOT_CONFIG="/boot/config.txt"
    BOOT_CMDLINE="/boot/cmdline.txt"
else
    BOOT_CONFIG=""
    BOOT_CMDLINE=""
fi

# Idempotently set "key=value" in config.txt (replaces any existing key).
set_boot_option() {
    local key="$1" value="$2"
    sudo sed -i "/^[[:space:]]*${key}=/d" "$BOOT_CONFIG"
    echo "${key}=${value}" | sudo tee -a "$BOOT_CONFIG" > /dev/null
}

# True when the kernel can mount / by itself: an ext4 partition on the SD card
# with the card controller and ext4 built in, as on Raspberry Pi OS kernels.
# Anything else (USB/NVMe root, encryption, LVM...) may need the initramfs.
root_needs_no_initramfs() {
    local kcfg="/boot/config-$(uname -r)"
    [[ "$(findmnt -no SOURCE /)" == /dev/mmcblk* ]] &&
        [ "$(findmnt -no FSTYPE /)" = ext4 ] &&
        [ -f "$kcfg" ] &&
        grep -q "^CONFIG_EXT4_FS=y" "$kcfg" &&
        grep -qE "^CONFIG_MMC_(BCM2835|SDHCI_IPROC|SDHCI_BRCMSTB)=y" "$kcfg"
}

# Helper functions
print_header() {
    echo -e "${BLUE}==========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}==========================================${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

# Step 1: System dependencies
install_dependencies() {
    print_header "Step 1: Installing System Dependencies"
    
    # Check if running on Raspberry Pi
    if [ ! -f /proc/device-tree/model ] || ! grep -q "Raspberry Pi" /proc/device-tree/model; then
        print_warning "This doesn't appear to be a Raspberry Pi"
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
    
    echo "📦 Updating package list..."
    sudo apt-get update -qq
    
    echo "📦 Installing Python packages..."
    sudo apt-get install -y \
        python3-pygame \
        python3-picamera2 \
        python3-gpiozero \
        python3-pip \
        python3-pil \
        python3-smbus \
        libts-bin \
        evtest \
        xserver-xorg-input-evdev
    
    print_success "System dependencies installed"
    echo ""
}

# Step 1.5: Configure Boot (GPU/Drivers)
setup_boot_config() {
    print_header "Step 1.5: Configuring Boot & Drivers"

    if [ -z "$BOOT_CONFIG" ]; then
        print_error "config.txt not found in /boot/firmware or /boot - skipping"
        echo ""
        return
    fi
    print_info "Using $BOOT_CONFIG"

    CONFIG="$BOOT_CONFIG"
    BACKUP="${BOOT_CONFIG}.bak_setup"

    # Backup
    if [ ! -f "$BACKUP" ]; then
        sudo cp "$CONFIG" "$BACKUP"
        echo "Backed up config.txt"
    fi

    echo "🔧 Setting GPU memory to 128MB..."
    set_boot_option gpu_mem 128

    echo "🔧 Enabling Display Drivers..."
    # Enable vc4-kms-v3d
    if ! grep -q "dtoverlay=vc4-kms-v3d" "$CONFIG"; then
        echo "dtoverlay=vc4-kms-v3d,noaudio" | sudo tee -a "$CONFIG"
    fi

    # Enable Touch
    if ! grep -q "dtoverlay=ads7846" "$CONFIG"; then
         echo "dtoverlay=ads7846,cs=1,penirq=25,speed=50000,keep_vref_on=0,swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900" | sudo tee -a "$CONFIG"
    fi

    # Disable start_x (legacy)
    sudo sed -i '/start_x=1/d' "$CONFIG"

    echo "🚀 Applying boot-time optimizations..."
    # boot_delay defaults to 1 second before the firmware loads the kernel.
    set_boot_option boot_delay 0
    # Skip the rainbow splash the firmware draws before the kernel starts.
    set_boot_option disable_splash 1
    # Hold the ARM at max clock for the first 30 s so the (CPU-bound) boot and
    # the camera/SDL init are not run at the idle frequency.
    set_boot_option initial_turbo 30

    # The initramfs only redoes what the kernel can already do here. Skipping
    # it saves reading ~12 MB in the firmware (~1.4 s) and ~6 s of initramfs
    # scripts before systemd starts (Pi Zero 2W).
    if root_needs_no_initramfs; then
        set_boot_option auto_initramfs 0
    else
        print_warning "Root is not a plain ext4 SD-card partition: keeping the initramfs"
    fi

    print_success "Boot configuration updated"
    echo ""
}

# Step 1.7: Kernel command line
setup_cmdline() {
    print_header "Step 1.7: Kernel Command Line"

    if [ -z "$BOOT_CMDLINE" ] || [ ! -f "$BOOT_CMDLINE" ]; then
        print_warning "cmdline.txt not found - skipping"
        echo ""
        return
    fi

    if [ ! -f "${BOOT_CMDLINE}.bak_setup" ]; then
        sudo cp "$BOOT_CMDLINE" "${BOOT_CMDLINE}.bak_setup"
    fi

    # cmdline.txt must be a single line: the firmware reads only the first
    # one, so options appended on a second line are silently ignored (that
    # is how "quiet" had ended up having no effect on a deployed camera).
    CMDLINE=$(tr '\n' ' ' < "$BOOT_CMDLINE" | tr -s ' ')
    CMDLINE="${CMDLINE% }"

    # The 3.5" MPI3508 HDMI display scales any input to its 480x320 panel,
    # which is 3:2. A 4:3 mode such as 640x480 came out stretched sideways
    # (12.5%); 720x480 has the panel's shape and scales evenly by 2/3. Its
    # EDID names the model.
    if grep -qa "MPI3508" /sys/class/drm/card*-HDMI-A-*/edid 2>/dev/null; then
        CMDLINE=$(echo "$CMDLINE" | sed -E 's/ ?video=HDMI-A-1:[^ ]+//')
        CMDLINE="$CMDLINE video=HDMI-A-1:720x480M-32@60"
        print_success "MPI3508 display: 720x480, the same 3:2 shape as its panel"
    fi

    # camera_app.py draws straight into the framebuffer, which the KMS driver
    # creates at 16 bpp unless the video= mode asks otherwise; at 16 bpp every
    # frame is converted and colour depth drops. Only an existing video= mode
    # without a bpp is extended (e.g. 640x480M@60 -> 640x480M-32@60).
    local video
    video=$(echo "$CMDLINE" | grep -oE 'video=[^ ]+' | head -1 || true)
    if [ -n "$video" ] && ! echo "$video" | grep -qE ':[0-9]+x[0-9]+[A-Za-z]*-[0-9]+'; then
        CMDLINE="${CMDLINE/$video/$(echo "$video" | sed -E 's/(:[0-9]+x[0-9]+[A-Za-z]*)/\1-32/')}"
        print_success "Framebuffer set to 32 bpp"
    fi

    print_info "Boot messages go to the serial console at 115200 baud and are"
    print_info "painted on the panel one line at a time, which costs seconds."
    print_info "Silencing them speeds up boot and leaves a clean screen."
    print_warning "Trade-off: boot errors will no longer be visible on screen"
    print_info "(they remain in 'journalctl -b')."
    read -p "Quieten kernel console output? (Y/n) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Nn]$ ]]; then
        print_warning "Console output left as is"
    else
        for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0; do
            key="${opt%%=*}"
            if ! echo "$CMDLINE" | grep -qw -- "$key"; then
                CMDLINE="$CMDLINE $opt"
            fi
        done
        print_success "Kernel console quietened"
    fi
    echo "$CMDLINE" | sudo tee "$BOOT_CMDLINE" > /dev/null

    print_info "Restore with: sudo cp ${BOOT_CMDLINE}.bak_setup $BOOT_CMDLINE"
    echo ""
}

# Step 1.8: Mount the boot partition on demand
setup_fstab() {
    print_header "Step 1.8: Boot Partition Mount"

    # The FAT boot partition is only written by kernel/firmware updates, yet by
    # default every boot waits for its device, an fsck (~3 s on a Zero 2W) and
    # the mount before local-fs.target. As an automount it is mounted on first
    # access instead, and a failed mount still fails loudly on that access.
    local mp=""
    for candidate in /boot/firmware /boot; do
        if awk -v mp="$candidate" '$1 !~ /^#/ && $2 == mp && $3 == "vfat"' /etc/fstab | grep -q .; then
            mp="$candidate"
            break
        fi
    done
    if [ -z "$mp" ]; then
        print_warning "No vfat boot partition in /etc/fstab - skipping"
        echo ""
        return
    fi
    if awk -v mp="$mp" '$1 !~ /^#/ && $2 == mp' /etc/fstab | grep -q "x-systemd.automount"; then
        print_success "$mp is already mounted on demand"
        echo ""
        return
    fi

    [ -f /etc/fstab.bak_setup ] || sudo cp -p /etc/fstab /etc/fstab.bak_setup
    local new
    new=$(mktemp)
    # Only the boot partition line is rewritten (and reformatted by awk).
    awk -v mp="$mp" 'BEGIN { OFS = "\t" }
        $1 !~ /^#/ && $2 == mp { $4 = $4 ",x-systemd.automount"; $6 = 0 }
        { print }' /etc/fstab > "$new"
    if sudo findmnt --verify --tab-file "$new" &> /dev/null; then
        sudo cp "$new" /etc/fstab
        sudo systemctl daemon-reload
        print_success "$mp mounted on first access, without fsck"
        print_info "Restore with: sudo cp /etc/fstab.bak_setup /etc/fstab"
    else
        print_error "Edited fstab did not verify - left unchanged"
    fi
    rm -f "$new"
    echo ""
}

# Step 1.6: Fix Display Issues (from LCD-show drivers)
fix_display_config() {
    print_header "Step 1.6: Fix Display Issues (Optional)"
    
    print_info "This step repairs common issues caused by LCD-show drivers"
    print_info "Run this if you're experiencing display or touch problems"
    read -p "Do you want to run display fix? (y/N) " -n 1 -r
    echo
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Skipping display fix"
        echo ""
        return
    fi
    
    if [ -z "$BOOT_CONFIG" ]; then
        print_error "config.txt not found in /boot/firmware or /boot - skipping"
        echo ""
        return
    fi
    CONFIG="$BOOT_CONFIG"

    # Backup config
    if [ ! -f "${CONFIG}.pre-fix" ]; then
        sudo cp "$CONFIG" "${CONFIG}.pre-fix"
        print_success "Backed up config.txt to ${CONFIG}.pre-fix"
    fi

    # Check for vc4-kms-v3d (Required for libcamera)
    echo "🔧 Checking graphics drivers..."
    if grep -q "^#.*dtoverlay=vc4-kms-v3d" "$CONFIG"; then
        echo "Uncommenting vc4-kms-v3d..."
        sudo sed -i 's/^#.*dtoverlay=vc4-kms-v3d/dtoverlay=vc4-kms-v3d/' "$CONFIG"
        print_success "Restored vc4-kms-v3d overlay"
    elif ! grep -q "dtoverlay=vc4-kms-v3d" "$CONFIG"; then
        echo "Adding vc4-kms-v3d..."
        echo "dtoverlay=vc4-kms-v3d,noaudio" | sudo tee -a "$CONFIG"
        print_success "Added vc4-kms-v3d overlay"
    else
        print_success "vc4-kms-v3d is already active"
    fi
    
    # Check Touch Overlay
    echo "🔧 Checking touch configuration..."
    if grep -q "dtoverlay=ads7846" "$CONFIG"; then
        print_success "Touch overlay (ads7846) found"
    else
        print_warning "Touch overlay missing! Adding generic XPT2046/ADS7846 config..."
        echo "dtoverlay=ads7846,cs=1,penirq=25,speed=50000,keep_vref_on=0,swapxy=0,pmax=255,xohms=150,xmin=200,xmax=3900,ymin=200,ymax=3900" | sudo tee -a "$CONFIG"
    fi
    
    # Fix GPU Memory
    echo "🔧 Checking GPU memory..."
    if ! grep -q "gpu_mem=128" "$CONFIG"; then
        set_boot_option gpu_mem 128
        print_success "Set GPU memory to 128MB"
    fi

    print_success "Display fix complete"
    print_info "If screen is blank after reboot, SSH in and restore backup:"
    print_info "sudo cp ${CONFIG}.pre-fix $CONFIG"
    echo ""
}

# Step 2: Create directories and set permissions
setup_directories() {
    print_header "Step 2: Setting Up Directories"
    
    echo "📁 Creating photos directory..."
    mkdir -p ~/photos
    chmod 755 ~/photos
    
    print_success "Directories created"
    echo ""
}

# Step 3: Configure services
setup_services() {
    print_header "Step 3: Configuring System Services"
    
    echo "Scaling privileges..."
    sudo usermod -aG input,video,render $USER
    
    echo "🔧 Setting up photo server service..."
    # The web server is not on the critical path to a live viewfinder, so it is
    # explicitly deprioritised: on a Pi Zero 2W it would otherwise compete with
    # the camera UI for CPU and SD-card I/O during boot.
    sudo tee /etc/systemd/system/photo-server.service > /dev/null <<EOF
[Unit]
Description=Pi Camera Photo Web Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
ExecStart=/usr/bin/python3 photo_server.py
Restart=on-failure
RestartSec=5

# Yield CPU and disk to camera-ui.service while both are starting.
Nice=10
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable photo-server.service
    print_success "Photo server service enabled"
    
    echo ""
    echo "🔐 Configuring sudo permissions..."
    sudo tee /etc/sudoers.d/camera-app > /dev/null <<SUDOEOF
# Allow camera app to control system power and display
$USER ALL=(ALL) NOPASSWD: /sbin/shutdown
$USER ALL=(ALL) NOPASSWD: /sbin/reboot
$USER ALL=(ALL) NOPASSWD: /usr/bin/vcgencmd
$USER ALL=(ALL) NOPASSWD: /bin/bash
$USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
$USER ALL=(ALL) NOPASSWD: /usr/bin/tee
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl suspend
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl reboot
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl poweroff
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start photo-server
$USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop photo-server
SUDOEOF
    sudo chmod 0440 /etc/sudoers.d/camera-app
    
    print_success "System services configured"
    echo ""
}

# Step 5: Configure autologin
setup_autologin() {
    print_header "Step 5: Configure Auto-login (Optional)"
    
    print_info "Auto-login allows the camera app to start automatically on boot"
    read -p "Do you want to enable auto-login? (y/N) " -n 1 -r
    echo
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Skipping auto-login setup"
        echo ""
        return
    fi
    
    echo "🔧 Configuring autologin..."
    sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
    
    sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf > /dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $USER --noclear %I \$TERM
EOF
    
    sudo systemctl daemon-reload
    print_success "Auto-login configured for user: $USER"
    echo ""
}

# Step 6: Configure camera app autostart
setup_autostart() {
    print_header "Step 6: Configure Camera App Autostart (Optional)"
    
    print_info "This makes the camera app start automatically on boot using systemd"
    read -p "Do you want to enable camera app autostart? (y/N) " -n 1 -r
    echo
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Skipping camera app autostart"
        echo ""
        return
    fi
    
    # Clean up old bashrc method if present
    BASHRC="$HOME/.bashrc"
    if grep -q "Auto-start camera app" "$BASHRC" 2>/dev/null; then
        echo "Removing old .bashrc autostart..."
        sed -i '/# Auto-start camera app/,/fi/d' "$BASHRC"
    fi
    
    echo "🔧 Creating systemd camera-ui service..."
    CURRENT_USER=$USER
    
    # Ordering notes (this unit is the whole point of the device, so it starts
    # as early as systemd allows):
    #  - DefaultDependencies=no: the app starts as soon as the root filesystem
    #    is writable, instead of after sysinit.target/basic.target. Nothing in
    #    between is needed to show the viewfinder: not network, not swap (which
    #    /tmp waits for), not tmpfiles, sockets or timers. Saves 2-5 s.
    #  - Devices may still be settling that early; camera_app.py waits for the
    #    framebuffer and the camera pipeline and retries touch, GPIO and I2C.
    #  - It no longer orders itself after getty@tty1 (disabled below), nor
    #    after systemd-user-sessions/network or plymouth as it once did.
    sudo tee /etc/systemd/system/camera-ui.service > /dev/null <<EOF
[Unit]
Description=Camera UI Environment
DefaultDependencies=no
After=systemd-remount-fs.service systemd-udev-trigger.service
Conflicts=getty@tty1.service shutdown.target
Before=shutdown.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$(pwd)
# The app draws straight to /dev/fb0; SDL's KMSDRM driver is only the
# fallback when the framebuffer is unusable (see FramebufferOutput).
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=XDG_RUNTIME_DIR=/run/user/1000
# Skip SDL's ALSA probe and gpiozero's pin-factory search (see camera_app.py)
Environment=SDL_AUDIODRIVER=dummy
Environment=GPIOZERO_PIN_FACTORY=lgpio

# Imported as a module rather than run as a script, so its bytecode is cached
# in __pycache__ instead of being recompiled on every boot.
ExecStart=/usr/bin/python3 -c "import camera_app; camera_app.main()"

# First to the CPU and the SD card during the boot stampede. The SD card is
# the bottleneck (~50 MB of Python modules and libraries to page in), and the
# mq-deadline scheduler serves the realtime class ahead of the services that
# start alongside (NetworkManager, avahi, dbus...).
Nice=-5
IOSchedulingClass=realtime
IOSchedulingPriority=4

# Logs go to the journal ("journalctl -u camera-ui -f"); tty1 is attached so
# the app can switch it to graphics mode and keep the console off the panel.
StandardInput=tty
StandardOutput=journal
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVTDisallocate=yes
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable camera-ui.service

    # Display and camera drivers normally load during udev's coldplug, while
    # the app is reading its libraries off the same SD card, and were bound
    # only ~5 s after it started. Loaded by systemd-modules-load instead they
    # are ready first. vc4 (HDMI DDC) and the sensor (behind an I2C mux) both
    # wait for the I2C controller driver. Only modules loaded right now, i.e.
    # for the hardware actually present, are listed.
    local mods=()
    for m in i2c_bcm2835 i2c_mux_pinctrl vc4 bcm2835_unicam_legacy bcm2835_unicam \
             imx477 bcm2835_isp; do
        if awk '{print $1}' /proc/modules | grep -qx "$m"; then
            mods+=("$m")
        fi
    done
    if [ ${#mods[@]} -gt 0 ]; then
        printf '%s\n' "# Display and camera drivers for camera-ui (see setup.sh)" "${mods[@]}" |
            sudo tee /etc/modules-load.d/camera-ui.conf > /dev/null
        print_success "Early driver load: ${mods[*]}"
    else
        print_warning "Camera/display drivers not loaded now: early load skipped"
    fi
    # Python writes the cached bytecode on first import; do it now, as the
    # service user, so even the first boot finds it.
    python3 -c "import py_compile; py_compile.compile('camera_app.py', doraise=True)" ||
        print_warning "Could not precompile camera_app.py"

    # camera-ui takes over tty1, so letting getty start there first only to be
    # torn down is wasted boot work (and makes auto-login pointless).
    sudo systemctl disable getty@tty1.service &> /dev/null || true

    print_success "Camera app autostart configured (systemd)"
    print_info "getty on tty1 disabled (camera-ui owns that console)"
    echo ""
}

# Step 6.5: Trim services that delay boot
setup_boot_services() {
    print_header "Step 6.5: Disable Boot-Delaying Services (Optional)"

    print_info "These services are not used by the camera app and each one"
    print_info "adds to boot time. NetworkManager itself is kept - only its"
    print_info "'wait-online' unit is disabled, which blocks boot until a"
    print_info "network is up (WiFi and the hotspot keep working)."
    echo ""
    print_warning "Disables: NetworkManager-wait-online, ModemManager,"
    print_warning "triggerhappy, dphys-swapfile (swap), apt/man-db/dpkg-backup"
    print_warning "timers, e2scrub (LVM only; ~10 s per boot on a Zero 2W),"
    print_warning "bluetooth, and rpi-eeprom-update on boards without an EEPROM."
    print_warning "Skip this if you use Bluetooth or rely on swap."
    read -p "Disable these services? (y/N) " -n 1 -r
    echo

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Skipping service trimming"
        echo ""
        return
    fi

    # Each entry is disabled only if present, so this works across images.
    for unit in \
        NetworkManager-wait-online.service \
        systemd-networkd-wait-online.service \
        ModemManager.service \
        triggerhappy.service \
        triggerhappy.socket \
        dphys-swapfile.service \
        bluetooth.service \
        hciuart.service \
        apt-daily.timer \
        apt-daily-upgrade.timer \
        man-db.timer \
        dpkg-db-backup.timer \
        e2scrub_all.timer \
        e2scrub_reap.service
    do
        if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "$unit"; then
            sudo systemctl disable --now "$unit" &> /dev/null && echo "  ✓ disabled $unit"
        fi
    done

    # Only Pi 4/400/5/500 and CM4/CM5 have a bootloader EEPROM to update.
    if ! grep -qE "Raspberry Pi (4|400|5|500)|Compute Module (4|5)" /proc/device-tree/model 2>/dev/null; then
        if systemctl list-unit-files rpi-eeprom-update.service 2>/dev/null | grep -q rpi-eeprom-update; then
            sudo systemctl disable rpi-eeprom-update.service &> /dev/null && echo "  ✓ disabled rpi-eeprom-update.service"
        fi
    fi

    # Cap how long a hung unit can stall shutdown (default is 90 s): the camera
    # app used to take the full 90 s to stop when the sensor had hung. No start
    # cap: NetworkManager alone was measured at 15 s on a busy boot.
    echo "🔧 Capping systemd stop timeouts..."
    sudo mkdir -p /etc/systemd/system.conf.d
    sudo tee /etc/systemd/system.conf.d/timeout.conf > /dev/null <<'EOF'
[Manager]
DefaultTimeoutStopSec=10s
EOF

    # journald flushing to the SD card is a measurable boot cost, and the log
    # is only useful for the current session on an appliance like this.
    echo "🔧 Switching journal to volatile storage..."
    sudo mkdir -p /etc/systemd/journald.conf.d
    sudo tee /etc/systemd/journald.conf.d/volatile.conf > /dev/null <<'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF
    print_info "Logs are now RAM-only and cleared on reboot ('journalctl -u camera-ui')."

    sudo systemctl daemon-reload

    print_success "Boot services trimmed"
    print_info "Re-enable any of them with: sudo systemctl enable --now <unit>"
    echo ""
}

# Step 7: Configure WiFi hotspot
setup_hotspot() {
    print_header "Step 7: Configure WiFi Hotspot (Optional)"
    
    print_info "The hotspot allows remote control when not connected to WiFi"
    print_info "Default SSID: $HOTSPOT_SSID | Password: $HOTSPOT_PASSWORD"
    read -p "Do you want to configure the WiFi hotspot? (y/N) " -n 1 -r
    echo
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_warning "Skipping hotspot setup"
        echo ""
        return
    fi
    
    # Check if NetworkManager is available
    if ! command -v nmcli &> /dev/null; then
        print_error "nmcli not found. NetworkManager required for hotspot."
        echo ""
        return
    fi
    
    # Delete existing hotspot if it exists
    if nmcli connection show "$HOTSPOT_NAME" &> /dev/null; then
        echo "Removing old hotspot profile..."
        sudo nmcli connection delete "$HOTSPOT_NAME"
    fi
    
    echo "🔧 Creating hotspot profile..."
    sudo nmcli con add type wifi ifname wlan0 con-name "$HOTSPOT_NAME" autoconnect no ssid "$HOTSPOT_SSID"
    sudo nmcli con modify "$HOTSPOT_NAME" 802-11-wireless.mode ap
    sudo nmcli con modify "$HOTSPOT_NAME" 802-11-wireless.band bg
    sudo nmcli con modify "$HOTSPOT_NAME" wifi-sec.key-mgmt wpa-psk
    sudo nmcli con modify "$HOTSPOT_NAME" wifi-sec.psk "$HOTSPOT_PASSWORD"
    sudo nmcli con modify "$HOTSPOT_NAME" ipv4.method shared
    
    print_success "Hotspot configured (IP: 10.42.0.1)"
    print_info "Hotspot can be toggled from the camera app power menu"
    echo ""
}

# Main installation flow
main() {
    clear
    print_header "Pi Camera Application - Complete Setup"
    
    echo "This script will:"
    echo "  1. Install system dependencies"
    echo "  1.5 Configure boot (GPU memory, drivers, boot speed)"
    echo "  1.6 Fix display issues (optional)"
    echo "  1.7 Kernel command line (quiet console, 32 bpp framebuffer)"
    echo "  1.8 Mount the boot partition on demand"
    echo "  2. Set up directories and permissions"
    echo "  3. Configure systemd services"
    echo "  5. Configure auto-login (optional)"
    echo "  6. Configure camera app autostart (optional)"
    echo "  6.5 Disable boot-delaying services (optional)"
    echo "  7. Configure WiFi hotspot (optional)"
    echo ""

    read -p "Press Enter to begin installation..."
    echo ""

    # Run all steps
    install_dependencies
    setup_boot_config
    fix_display_config
    setup_cmdline
    setup_fstab
    setup_directories
    setup_services
    setup_autologin
    setup_autostart
    setup_boot_services
    setup_hotspot
    
    # Final summary
    print_header "Installation Complete!"
    
    echo "📝 Summary:"
    echo ""
    echo "Services:"
    echo "  • Photo server: systemctl status photo-server"
    echo ""
    echo "Manual controls:"
    echo "  • Start camera: cd ~/raspi && python3 camera_app.py"
    echo "  • Start server: sudo systemctl start photo-server"
    echo "  • View logs: sudo journalctl -u photo-server -f"
    echo ""
    echo "Network access:"
    echo "  • Local: http://$(hostname -I | awk '{print $1}'):8080"
    echo "  • Hotspot: http://10.42.0.1:8080 (when hotspot active)"
    echo ""
    
    print_warning "IMPORTANT: Reboot required for all changes to take effect"
    echo ""
    
    read -p "Do you want to reboot now? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Rebooting..."
        sudo reboot
    else
        echo "Please reboot manually when ready: sudo reboot"
    fi
}

# Run main installation
main
