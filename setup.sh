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
    
    CONFIG="/boot/config.txt"
    BACKUP="/boot/config.txt.bak_setup"

    # Backup
    if [ ! -f "$BACKUP" ]; then
        sudo cp "$CONFIG" "$BACKUP"
        echo "Backed up config.txt"
    fi

    echo "🔧 Setting GPU memory to 128MB..."
    # Remove existing to avoid conflicts
    sudo sed -i '/gpu_mem/d' "$CONFIG"
    echo "gpu_mem=128" | sudo tee -a "$CONFIG"

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
    
    print_success "Boot configuration updated"
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
    
    CONFIG="/boot/config.txt"
    
    # Backup config
    if [ ! -f /boot/config.txt.pre-fix ]; then
        sudo cp /boot/config.txt /boot/config.txt.pre-fix
        print_success "Backed up config.txt to config.txt.pre-fix"
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
        sudo sed -i '/gpu_mem=/d' "$CONFIG"
        echo "gpu_mem=128" | sudo tee -a "$CONFIG"
        print_success "Set GPU memory to 128MB"
    fi
    
    print_success "Display fix complete"
    print_info "If screen is blank after reboot, SSH in and restore backup:"
    print_info "sudo cp /boot/config.txt.pre-fix /boot/config.txt"
    echo ""
}

# Step 2: Create directories and set permissions
setup_directories() {
    print_header "Step 2: Setting Up Directories"
    
    echo "📁 Creating photos directory..."
    mkdir -p ~/photos
    chmod 755 ~/photos
    
    echo "📁 Creating assets directory..."
    mkdir -p assets
    
    print_success "Directories created"
    echo ""
}

# Step 3: Configure services
setup_services() {
    print_header "Step 3: Configuring System Services"
    
    echo "Scaling privileges..."
    sudo usermod -aG input,video,render $USER
    
    echo "🔧 Setting up photo server service..."
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
$USER ALL=(ALL) NOPASSWD: /usr/bin/vcgencmd
$USER ALL=(ALL) NOPASSWD: /bin/bash
$USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
$USER ALL=(ALL) NOPASSWD: /usr/bin/tee
SUDOEOF
    sudo chmod 0440 /etc/sudoers.d/camera-app
    
    print_success "System services configured"
    echo ""
}

# Step 4: Download UI icons
download_icons() {
    print_header "Step 4: Downloading UI Icons"
    
    BASE_URL="https://material-icons.github.io/material-icons-png/png/white"
    SUFFIX="round-4x.png"
    
    download_icon() {
        local name=$1
        local url_part=$2
        local filename="icon_${name}.png"
        if curl -s -f -o "assets/$filename" "$BASE_URL/$url_part/$SUFFIX" 2>/dev/null; then
            echo "  ✓ $name"
        else
            echo "  ⚠️  Failed: $name (optional)"
        fi
    }
    
    download_icon "gallery" "photo_library"
    download_icon "power" "power_settings_new"
    download_icon "iso" "iso"
    
    if curl -s -f -o "assets/icon_shutter.png" "$BASE_URL/shutter_speed/$SUFFIX" 2>/dev/null; then
        echo "  ✓ shutter"
    else
        curl -s -f -o "assets/icon_shutter.png" "$BASE_URL/timer/$SUFFIX" 2>/dev/null && echo "  ✓ shutter (timer)" || echo "  ⚠️  Failed: shutter (optional)"
    fi
    
    download_icon "arrow_left" "chevron_left"
    download_icon "arrow_right" "chevron_right"
    download_icon "back" "arrow_back"
    
    print_success "Icons downloaded"
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
    
    # Note: We use KMSDRM driver which works best with vc4-kms-v3d overlay
    sudo tee /etc/systemd/system/camera-ui.service > /dev/null <<EOF
[Unit]
Description=Camera UI Environment
After=systemd-user-sessions.service plymouth-quit-wait.service ant.service
Conflicts=getty@tty1.service

[Service]
User=$CURRENT_USER
WorkingDirectory=$(pwd)
# KMSDRM for hardware acceleration
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=XDG_RUNTIME_DIR=/run/user/1000

# Input Configuration
# Force TSLib if needed (uncomment if touch fails)
# Environment=SDL_MOUSEDRV=TSLIB
# Environment=TSLIB_FBDEVICE=/dev/fb0
# Environment=TSLIB_TSDEVICE=/dev/input/event0
ExecStart=/usr/bin/python3 camera_app.py
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable camera-ui.service
    
    print_success "Camera app autostart configured (systemd)"
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
    echo "  1.5 Configure boot (GPU memory, drivers)"
    echo "  1.6 Fix display issues (optional)"
    echo "  2. Set up directories and permissions"
    echo "  3. Configure systemd services"
    echo "  4. Download UI icons"
    echo "  5. Configure auto-login (optional)"
    echo "  6. Configure camera app autostart (optional)"
    echo "  7. Configure WiFi hotspot (optional)"
    echo ""
    
    read -p "Press Enter to begin installation..."
    echo ""
    
    # Run all steps
    install_dependencies
    setup_boot_config
    fix_display_config
    setup_directories
    setup_services
    download_icons
    setup_autologin
    setup_autostart
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
