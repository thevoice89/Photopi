#!/usr/bin/env python3
"""
Simple HTTP server for viewing photos from the Raspberry Pi Camera
Serves photos on the local network
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
import os
from pathlib import Path
import socket
import json
import time
import datetime
import zipfile
import io
import subprocess

PHOTOS_DIR = Path.home() / "photos"
PORT = 8080
UDP_PORT = 12345
SHARED_MEM_PREVIEW = "/tmp/camera_preview.jpg"
SHARED_MEM_STATUS = "/tmp/camera_status.json"

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    daemon_threads = True

class PhotoHandler(SimpleHTTPRequestHandler):
    """Custom handler to serve photos"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PHOTOS_DIR), **kwargs)
    
    def do_GET(self):
        """Handle GET requests"""
        if self.path == '/':
            self.list_directory(self.path)
        elif self.path == '/live':
            self.serve_live_page()
        elif self.path == '/api/status':
            self.serve_status()
        elif self.path == '/stream.mjpg':
            self.serve_mjpeg_stream()
        elif self.path == '/preview.jpg':
            self.serve_preview()
        else:
            # Fallback to serving files
            super().do_GET()

    def do_POST(self):
        """Handle POST requests (for delete, download zip, commands, and system control)"""
        if self.path.startswith('/delete/'):
            filename = self.path.replace('/delete/', '')
            file_path = PHOTOS_DIR / filename
            
            try:
                if file_path.exists() and file_path.suffix.lower() == '.jpg':
                    file_path.unlink()
                    print(f"Deleted file: {filename}")
                    
                    # Redirect back to gallery
                    self.send_response(303)
                    self.send_header('Location', '/')
                    self.end_headers()
                else:
                    self.send_error(404, "File not found")
            except Exception as e:
                self.send_error(500, f"Error deleting file: {e}")

        elif self.path == '/delete_multiple':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
                files = data.get('files', [])
                deleted, errors = [], []
                for name in files:
                    fp = PHOTOS_DIR / Path(name).name  # sanitise
                    if fp.exists() and fp.suffix.lower() == '.jpg':
                        fp.unlink()
                        deleted.append(name)
                        print(f"Deleted: {name}")
                    else:
                        errors.append(name)
                response = json.dumps({'deleted': deleted, 'errors': errors}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(response)
            except Exception as e:
                self.send_error(500, f"Error in batch delete: {e}")

        elif self.path == '/download_zip':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(body)
                files = data.get('files', [])
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for name in files:
                        fp = PHOTOS_DIR / Path(name).name
                        if fp.exists() and fp.suffix.lower() == '.jpg':
                            zf.write(fp, fp.name)
                zip_bytes = buf.getvalue()
                ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f'foto_{ts}.zip'
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                self.send_header('Content-Length', str(len(zip_bytes)))
                self.end_headers()
                self.wfile.write(zip_bytes)
            except Exception as e:
                self.send_error(500, f"Error creating ZIP: {e}")

        elif self.path == '/system/shutdown':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"shutting down"}')
            print("Shutdown requested via web")
            subprocess.Popen(['sudo', 'shutdown', 'now'])

        elif self.path == '/system/reboot':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"rebooting"}')
            print("Reboot requested via web")
            subprocess.Popen(['sudo', 'reboot'])

        elif self.path == '/api/command':
            length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(length).decode('utf-8')
            try:
                data = json.loads(post_data)
                command = data.get('command')
                if command:
                    self.send_udp_command(command)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"ok"}')
                else:
                    self.send_error(400, "Missing command")
            except Exception as e:
                self.send_error(500, f"Error processing command: {e}")

    def send_udp_command(self, command):
        """Send command to camera app via UDP"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(command.encode('utf-8'), ('localhost', UDP_PORT))
        except Exception as e:
            print(f"UDP Error: {e}")

    def serve_status(self):
        """Serve camera status from shared memory"""
        try:
            if os.path.exists(SHARED_MEM_STATUS):
                with open(SHARED_MEM_STATUS, 'r') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(data.encode('utf-8'))
            else:
                # Default status if not running
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"iso":"--", "shutter":"--", "mode":"local"}')
        except Exception:
            self.send_error(500, "Error reading status")

    def serve_preview(self):
        """Serve preview image from shared memory"""
        try:
            if os.path.exists(SHARED_MEM_PREVIEW):
                with open(SHARED_MEM_PREVIEW, 'rb') as f:
                    data = f.read()
                self.send_response(200)
                self.send_header('Content-type', 'image/jpeg')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404, "No preview available")
        except Exception:
            self.send_error(500, "Error reading preview")

    def serve_mjpeg_stream(self):
        """Serve MJPEG stream from shared memory"""
        self.send_response(200)
        self.send_header('Age', '0')
        self.send_header('Cache-Control', 'no-cache, private')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
        self.end_headers()
        
        try:
            while True:
                if os.path.exists(SHARED_MEM_PREVIEW):
                    with open(SHARED_MEM_PREVIEW, 'rb') as f:
                        frame = f.read()
                    
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
                    
                    time.sleep(0.1) # Limit to ~10 FPS
                else:
                    time.sleep(0.5)
        except Exception:
            pass # Client disconnected

    def serve_live_page(self):
        """Serve the Live Control interface in Material Design 3 style"""
        html = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Live Control | Pi Camera</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,100..900;1,14..32,100..900&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" />
    <style>
        :root {
            --m3-surface: #121212;
            --m3-on-surface: #E6E1E5;
            --m3-primary: #D0BCFF;
            --m3-on-primary: #381E72;
            --m3-secondary: #CCC2DC;
            --m3-surface-variant: #49454F;
            --m3-on-surface-variant: #CAC4D0;
            --m3-outline: #938F99;
        }

        body { 
            font-family: 'Inter', sans-serif; 
            background: var(--m3-surface); 
            color: var(--m3-on-surface); 
            margin: 0;
            padding: 0;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            overflow: hidden;
        }

        /* Top App Bar */
        .app-bar {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 64px;
            display: flex;
            align-items: center;
            padding: 0 16px;
            box-sizing: border-box;
            background: var(--m3-surface);
            z-index: 100;
        }

        .icon-btn {
            background: transparent;
            border: none;
            color: var(--m3-on-surface);
            width: 48px;
            height: 48px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: background 0.2s;
            text-decoration: none;
        }
        .icon-btn:active { background: rgba(255,255,255,0.1); }
        .icon-btn svg { width: 24px; height: 24px; }
        .btn-tonal svg { width: 18px; height: 18px; }
        .fab-capture svg { width: 36px; height: 36px; }
        
        .app-title {
            margin-left: 8px;
            font-size: 22px;
            font-weight: 400;
        }

        /* Preview Container */
        .preview-wrapper {
            position: absolute;
            top: 64px; /* below app bar */
            bottom: 120px; /* above control panel */
            left: 0;
            right: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #000;
            z-index: 1;
        }

        #preview { 
            max-width: 100%; 
            max-height: 100%; 
            object-fit: contain; 
            background: #111;
            width: 100%;
            height: 100%;
        }

        .status-overlay {
            position: absolute;
            top: 16px;
            right: 16px;
            background: rgba(0,0,0,0.5);
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
            backdrop-filter: blur(4px);
        }

        .pulse {
            width: 8px;
            height: 8px;
            background: #ff5252;
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.5); opacity: 0.5; }
            100% { transform: scale(1); opacity: 1; }
        }

        /* Bottom Control Panel */
        .control-panel {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            background: #1C1B1F;
            /* Extra padding to lift controls above the mobile swipe bar / nav bar */
            padding: 16px 8px calc(24px + env(safe-area-inset-bottom, 0px)) 8px;
            box-sizing: border-box;
            border-radius: 28px 28px 0 0;
            display: flex;
            flex-direction: column;
            gap: 16px;
            box-shadow: 0 -4px 12px rgba(0,0,0,0.3);
            z-index: 10;
        }

        .controls-row {
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            align-items: center;
            gap: 8px;
            width: 100%;
        }

        .m3-card {
            background: var(--m3-surface-variant);
            border-radius: 12px;
            padding: 8px 4px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            min-width: 0;
        }

        .label {
            font-size: 10px;
            font-weight: 500;
            color: var(--m3-on-surface-variant);
            letter-spacing: 0.5px;
            text-transform: uppercase;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
        }

        .value {
            font-size: 16px;
            font-weight: 500;
            color: var(--m3-on-surface);
            min-width: 40px;
            text-align: center;
        }

        .adj-btns { display: flex; gap: 4px; }

        .btn-tonal {
            background: var(--m3-secondary);
            color: var(--m3-on-primary);
            border: none;
            height: 32px;
            width: 32px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .btn-tonal .material-symbols-outlined { font-size: 18px; }

        /* Capture Button - Rounded & Broken (Circle) */
        .fab-capture {
            width: 72px;
            height: 72px;
            border-radius: 50%;
            background: var(--m3-primary);
            color: var(--m3-on-primary);
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 4px 8px rgba(0,0,0,0.4);
            transition: transform 0.1s, box-shadow 0.2s;
            margin: 0 4px;
        }
        .fab-capture:active { transform: scale(0.95); }
        .fab-capture .material-symbols-outlined { font-size: 36px; }

        /* LANDSCAPE MODE & DESKTOP (Direct UI update) */
        @media (min-width: 600px), (orientation: landscape) {
            .app-bar { display: none !important; }
            .preview-wrapper { 
                position: fixed; 
                inset: 0; 
                z-index: 1; 
                height: 100vh;
                width: 100vw;
                background: #000;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            #preview { 
                max-width: 100%; 
                max-height: 100%; 
                width: 100%;
                height: 100%;
                object-fit: contain; 
            }
            
            .control-panel {
                position: fixed;
                bottom: 16px;
                left: 50%;
                transform: translateX(-50%);
                width: auto;
                min-width: 0;
                max-width: 95vw;
                border-radius: 40px;
                padding: 12px 24px;
                background: rgba(28, 27, 31, 0.85);
                backdrop-filter: blur(16px);
                border: 1px solid rgba(255,255,255,0.15);
                gap: 16px;
                z-index: 100;
                
                /* Fix alignment with flexbox */
                display: flex !important;
                flex-direction: row !important;
                align-items: center;
                justify-content: center;
                overflow-x: auto;
            }
            
            .controls-row { 
                display: flex !important;
                flex-direction: row;
                align-items: center;
                justify-content: center;
                gap: 16px;
            }
            
            .m3-card { 
                background: transparent; 
                padding: 0; 
                border: none; 
                box-shadow: none;
                min-width: auto;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 8px;
            }
            
            .fab-capture { 
                width: 64px; 
                height: 64px; 
                flex-shrink: 0;
            }
            
            .label { display: none; }
            .value { font-size: 14px; min-width: auto; }
            
            /* Persistent Float Back */
            .back-container {
                position: fixed;
                top: 16px;
                left: 16px;
                z-index: 1000;
                display: block !important;
            }
        }

        .offline-overlay {
            position: absolute;
            inset: 0;
            background: rgba(0,0,0,0.8);
            display: none;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 16px;
            z-index: 5;
        }

        .back-container { display: none; }
    </style>
</head>
<body>
    <header class="app-bar">
        <a href="/" class="icon-btn">
            <svg fill="currentColor" viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
        </a>
        <div class="app-title">Controllo Live</div>
    </header>

    <div class="back-container">
        <a href="/" class="icon-btn" style="background: rgba(0,0,0,0.5); backdrop-filter: blur(8px); border-radius: 50%;">
            <svg fill="white" viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
        </a>
    </div>

    <main class="preview-wrapper">
        <img id="preview" src="/stream.mjpg" alt="Stream Camera">
        
        <div class="status-overlay">
            <div class="pulse"></div>
            <span>LIVE <span id="res-info" style="opacity: 0.6; font-size: 10px; margin-left: 4px"></span></span>
        </div>

        <div id="offline-msg" class="offline-overlay">
            <svg width="48" height="48" fill="var(--m3-outline)" viewBox="0 0 24 24"><path d="M9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z"/><circle cx="12" cy="12" r="3"/></svg>
            <span style="font-weight:500">Telecomando non attivo</span>
            <span style="font-size: 14px; color: var(--m3-on-surface-variant)">Tocca lo schermo della camera</span>
        </div>
    </main>

    <footer class="control-panel">
        <div class="controls-row">
            <!-- ISO -->
            <div class="m3-card">
                <span class="label">ISO</span>
                <span id="iso-val" class="value">--</span>
                <div class="adj-btns">
                    <button onclick="sendCommand('ISO_DOWN')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13H5v-2h14v2z"/></svg></button>
                    <button onclick="sendCommand('ISO_UP')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></button>
                </div>
            </div>

            <!-- Capture -->
            <button onclick="sendCommand('CAPTURE')" class="fab-capture">
                <svg fill="currentColor" viewBox="0 0 24 24"><path d="M9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>

            <!-- Shutter -->
            <div class="m3-card">
                <span class="label">Otturatore</span>
                <span id="shutter-val" class="value">--</span>
                <div class="adj-btns">
                    <button onclick="sendCommand('SHUTTER_DOWN')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13H5v-2h14v2z"/></svg></button>
                    <button onclick="sendCommand('SHUTTER_UP')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></button>
                </div>
            </div>
        </div>
    </footer>


    <script>
        function sendCommand(cmd) {
            fetch('/api/command', {
                method: 'POST',
                body: JSON.stringify({command: cmd}),
                headers: {'Content-Type': 'application/json'}
            });
        }

        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('iso-val').textContent = data.iso;
                    document.getElementById('shutter-val').textContent = data.shutter;
                    
                    const offline = document.getElementById('offline-msg');
                    const preview = document.getElementById('preview');
                    const backBtn = document.querySelector('.back-container');
                    
                    // Show frame dimensions in the LIVE badge for diagnosis
                    if (preview.naturalWidth > 0) {
                        document.getElementById('res-info').textContent = `${preview.naturalWidth}x${preview.naturalHeight}`;
                    }
                    
                    const isLandscape = window.matchMedia("(orientation: landscape)").matches;
                    
                    if (data.mode === 'remote') {
                        offline.style.display = 'none';
                        preview.style.opacity = 1;
                    } else {
                        preview.style.opacity = 0.3;
                        offline.style.display = 'flex';
                    }
                    
                    if (isLandscape) {
                        backBtn.style.display = 'block';
                    } else {
                        backBtn.style.display = 'none';
                    }
                })
                .catch(e => console.log('Conn error'));
        }

        // Cleanup when page closes
        window.addEventListener('beforeunload', function() {
            sendCommand('STOP_REMOTE');
        });
        
        // Also cleanup when navigating away
        window.addEventListener('pagehide', function() {
            sendCommand('STOP_REMOTE');
        });

        // Heartbeats - reduced to 3s intervals for faster detection
        sendCommand('START_REMOTE');
        setInterval(updateStatus, 1000);
        setInterval(() => sendCommand('HEARTBEAT'), 3000);
    </script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def list_directory(self, path):
        """Override to show a beautiful Material Design 3 photo gallery"""
        try:
            photos = sorted(PHOTOS_DIR.glob("*.jpg"), reverse=True)
        except OSError:
            self.send_error(404, "Cannot list directory")
            return None
        
        try:
            result = subprocess.run(['hostname', '-I'], capture_output=True, text=True, timeout=3)
            ip_address = result.stdout.strip().split()[0]
        except Exception:
            ip_address = 'localhost'
        
        photo_count = len(photos)
        
        html = f"""
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Galleria | Pi Camera</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,100..900;1,14..32,100..900&display=swap" rel="stylesheet">
    <style>
        :root {{
            --m3-surface: #1C1B1F;
            --m3-on-surface: #E6E1E5;
            --m3-surface-container: #2B2930;
            --m3-primary: #D0BCFF;
            --m3-on-primary: #381E72;
            --m3-secondary-container: #4A4458;
            --m3-on-secondary-container: #E8DEF8;
            --m3-outline: #938F99;
            --m3-error: #F2B8B5;
            --m3-error-container: #8C1D18;
        }}
        
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{
            font-family: 'Inter', sans-serif;
            background: var(--m3-surface);
            color: var(--m3-on-surface);
            min-height: 100vh;
            padding-bottom: 120px;
        }}

        /* App Bar */
        .app-bar {{
            height: 64px;
            display: flex;
            align-items: center;
            padding: 0 4px 0 16px;
            position: sticky;
            top: 0;
            background: var(--m3-surface);
            z-index: 10;
            gap: 4px;
        }}

        .app-title {{ font-size: 22px; font-weight: 400; flex: 1; }}

        .icon-btn {{
            background: transparent;
            border: none;
            color: var(--m3-on-surface);
            width: 48px;
            height: 48px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: background 0.15s;
        }}
        .icon-btn:hover {{ background: rgba(255,255,255,0.08); }}
        .icon-btn.active {{ background: rgba(208,188,255,0.15); color: var(--m3-primary); }}
        .icon-btn.error {{ color: var(--m3-error); }}
        .icon-btn svg {{ width: 24px; height: 24px; }}

        /* Selection mode app bar */
        .sel-bar {{
            display: none;
            height: 64px;
            align-items: center;
            padding: 0 8px;
            position: sticky;
            top: 0;
            background: var(--m3-secondary-container);
            z-index: 10;
            gap: 4px;
        }}
        .sel-bar.visible {{ display: flex; }}
        .sel-count {{ flex: 1; font-size: 18px; font-weight: 500; padding-left: 8px; color: var(--m3-on-secondary-container); }}

        /* Info Banner */
        .info-header {{
            padding: 12px 16px;
            background: var(--m3-surface-container);
            margin: 16px;
            border-radius: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 14px;
            color: var(--m3-on-secondary-container);
        }}

        .gallery {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 16px;
            padding: 16px;
        }}

        /* Card */
        .m3-card {{
            background: var(--m3-surface-container);
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.15s, outline 0.15s;
            position: relative;
        }}
        .m3-card.selected {{
            outline: 3px solid var(--m3-primary);
        }}

        /* Checkbox overlay */
        .card-check {{
            position: absolute;
            top: 10px;
            left: 10px;
            width: 28px;
            height: 28px;
            border-radius: 50%;
            background: rgba(0,0,0,0.55);
            border: 2px solid rgba(255,255,255,0.8);
            display: none;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            z-index: 2;
            transition: background 0.15s, border-color 0.15s;
        }}
        .card-check svg {{ width: 16px; height: 16px; fill: white; display: none; }}
        .m3-card.selected .card-check {{
            background: var(--m3-primary);
            border-color: var(--m3-primary);
        }}
        .m3-card.selected .card-check svg {{ display: block; }}
        body.select-mode .card-check {{ display: flex; }}
        body.select-mode .m3-card img {{ cursor: default; }}

        .m3-card img {{
            width: 100%;
            height: 200px;
            object-fit: cover;
            cursor: pointer;
            display: block;
        }}

        .card-content {{ padding: 12px; flex: 1; }}
        .file-name {{ font-size: 14px; font-weight: 500; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .file-date {{ font-size: 12px; color: var(--m3-outline); }}

        .card-actions {{
            padding: 8px;
            display: flex;
            justify-content: flex-end;
            gap: 8px;
            border-top: 1px solid rgba(255,255,255,0.05);
        }}
        body.select-mode .card-actions {{ display: none; }}

        .text-btn {{
            background: transparent;
            border: none;
            color: var(--m3-primary);
            padding: 8px 12px;
            border-radius: 8px;
            font-weight: 500;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .text-btn.delete {{ color: var(--m3-error); }}
        .text-btn:hover {{ background: rgba(208, 188, 255, 0.08); }}
        .text-btn.delete:hover {{ background: rgba(242, 184, 181, 0.08); }}

        /* Bulk action bar */
        .bulk-bar {{
            display: none;
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            height: 80px;
            background: var(--m3-secondary-container);
            align-items: center;
            justify-content: center;
            gap: 12px;
            z-index: 200;
            padding: 0 24px;
            box-shadow: 0 -4px 16px rgba(0,0,0,0.4);
        }}
        .bulk-bar.visible {{ display: flex; }}

        .bulk-btn {{
            height: 48px;
            padding: 0 24px;
            border-radius: 24px;
            border: none;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: transform 0.1s, opacity 0.1s;
        }}
        .bulk-btn:active {{ transform: scale(0.97); }}
        .bulk-btn.download {{ background: var(--m3-primary); color: var(--m3-on-primary); }}
        .bulk-btn.del {{ background: var(--m3-error-container); color: var(--m3-error); }}
        .bulk-btn svg {{ width: 20px; height: 20px; fill: currentColor; }}
        .bulk-btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}

        /* FAB */
        .fab {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            height: 56px;
            padding: 0 24px;
            border-radius: 16px;
            background: var(--m3-primary);
            color: var(--m3-on-primary);
            display: flex;
            align-items: center;
            gap: 12px;
            text-decoration: none;
            font-weight: 500;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
            transition: box-shadow 0.2s, transform 0.2s;
            z-index: 100;
        }}
        .fab:active {{ transform: scale(0.95); }}
        body.select-mode .fab {{ display: none; }}

        /* Power dialog */
        .dialog-backdrop {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.6);
            z-index: 500;
            align-items: flex-end;
            justify-content: center;
            padding: 24px;
        }}
        .dialog-backdrop.open {{ display: flex; }}
        .power-dialog {{
            background: #2B2930;
            border-radius: 28px;
            padding: 24px;
            width: 100%;
            max-width: 360px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-bottom: env(safe-area-inset-bottom, 0);
        }}
        .dialog-title {{
            font-size: 18px;
            font-weight: 600;
            text-align: center;
            padding-bottom: 8px;
            color: var(--m3-on-surface);
        }}
        .power-option {{
            display: flex;
            align-items: center;
            gap: 16px;
            padding: 16px;
            border-radius: 16px;
            background: var(--m3-surface-container);
            border: none;
            color: var(--m3-on-surface);
            cursor: pointer;
            font-size: 16px;
            font-weight: 500;
            font-family: inherit;
            transition: background 0.15s;
        }}
        .power-option:hover {{ background: #3B3940; }}
        .power-option svg {{ width: 28px; height: 28px; flex-shrink: 0; }}
        .power-option.reboot svg {{ fill: #A8C7FA; }}
        .power-option.shutdown svg {{ fill: var(--m3-error); }}
        .power-option-label {{ text-align: left; }}
        .power-option-label span {{ display: block; font-size: 12px; color: var(--m3-outline); font-weight: 400; margin-top: 2px; }}
        .dialog-cancel {{
            background: transparent;
            border: 1px solid rgba(255,255,255,0.12);
            color: var(--m3-on-surface);
            border-radius: 12px;
            padding: 14px;
            font-size: 15px;
            font-family: inherit;
            cursor: pointer;
            margin-top: 4px;
            transition: background 0.15s;
        }}
        .dialog-cancel:hover {{ background: rgba(255,255,255,0.06); }}

        /* Lightbox */
        .lightbox {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.95);
            z-index: 1000;
            justify-content: center;
            align-items: center;
            padding: 24px;
        }}
        .lightbox.active {{ display: flex; }}
        .lightbox img {{ max-width: 100%; max-height: 100%; border-radius: 8px; }}
    </style>
</head>
<body>
    <!-- Normal app bar -->
    <header class="app-bar" id="normal-bar">
        <h1 class="app-title">Galleria</h1>
        <button class="icon-btn" onclick="toggleSelectMode()" id="select-btn" title="Seleziona foto">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 5h2V3c-1.1 0-2 .9-2 2zm0 8h2v-2H3v2zm4 8h2v-2H7v2zm-4-4h2v-2H3v2zm10-16H7v2h6V1zm6 0v2h2c0-1.1-.9-2-2-2zm-6 20h2v-2h-2v2zm-4-8h8V7H9v8zm2-6h4v4h-4V9zm8 12v-2h2v-2h-2v-2h-2v2h-2v2h2v2h2zm0-8h2v-2h-2v2zM3 17h2v-2H3v2zm14 0h2v-2h-2v2zm-4-14h-2v2h2V3z"/></svg>
        </button>
        <button class="icon-btn error" onclick="openPowerDialog()" title="Alimentazione">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42C17.99 7.86 19 9.81 19 12c0 3.87-3.13 7-7 7s-7-3.13-7-7c0-2.19 1.01-4.14 2.58-5.42L6.17 5.17C4.23 6.82 3 9.26 3 12c0 4.97 4.03 9 9 9s9-4.03 9-9c0-2.74-1.23-5.18-3.17-6.83z"/></svg>
        </button>
    </header>

    <!-- Selection mode app bar -->
    <header class="sel-bar" id="sel-bar">
        <button class="icon-btn" onclick="exitSelectMode()" title="Chiudi selezione">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
        </button>
        <span class="sel-count" id="sel-count">0 selezionate</span>
        <button class="icon-btn" onclick="selectAll()" title="Seleziona tutte">
            <svg viewBox="0 0 24 24"><path fill="currentColor" d="M18 7l-1.41-1.41-6.34 6.34-2.83-2.83L6 10.5l4.24 4.24L18 7zm-11.5 9H4v3h3v-2H5v-1zM4 7h3V4H4v3zm0 5h3v-3H4v3zm4 5h3v-3H8v3zm5 0h3v-3h-3v3z"/></svg>
        </button>
    </header>

    <div class="info-header">
        <span>{photo_count} Scatti salvati</span>
        <span style="font-size: 12px; color: var(--m3-outline);">http://{ip_address}:8080</span>
    </div>

    <main class="gallery">
"""
        if not photos:
            html += """<div style="grid-column: 1/-1; text-align: center; padding: 64px; color: var(--m3-outline)">
                <svg width="64" height="64" fill="currentColor" viewBox="0 0 24 24"><path d="M22 16V4c0-1.1-.9-2-2-2H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2zm-11-4l2.03 2.71L16 11l4 5H8l3-4zM2 6v14c0 1.1.9 2 2 2h14v-2H4V6H2z"/></svg>
                <p style="margin-top: 16px">Nessuna foto trovata</p>
            </div>"""
        else:
            for photo in photos:
                filename = photo.name
                timestamp = os.path.getmtime(photo)
                date_str = datetime.datetime.fromtimestamp(timestamp).strftime('%d %b %Y, %H:%M')
                
                html += f"""
        <div class="m3-card" data-filename="{filename}" onclick="toggleCard(this, '{filename}')">
            <div class="card-check">
                <svg viewBox="0 0 24 24"><path d="M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4L9 16.2z"/></svg>
            </div>
            <img src="/{filename}" onclick="if(!selectMode){{openLightbox('/{filename}');}} event.stopPropagation();" loading="lazy">
            <div class="card-content">
                <div class="file-name">{filename}</div>
                <div class="file-date">{date_str}</div>
            </div>
            <div class="card-actions">
                <a href="/{filename}" download class="text-btn" onclick="event.stopPropagation()">
                    <svg fill="currentColor" viewBox="0 0 24 24" width="18" height="18"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
                    Scarica
                </a>
                <form action="/delete/{filename}" method="post" style="display:contents" onsubmit="return confirm('Eliminare definitivamente questa foto?');" onclick="event.stopPropagation()">
                    <button type="submit" class="text-btn delete">
                        <svg fill="currentColor" viewBox="0 0 24 24" width="18" height="18"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                        Elimina
                    </button>
                </form>
            </div>
        </div>
"""

        html += f"""
    </main>

    <a href="/live" class="fab" id="live-fab">
        <svg fill="currentColor" viewBox="0 0 24 24" width="24" height="24"><path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>
        LIVE
    </a>

    <!-- Bulk action bar -->
    <div class="bulk-bar" id="bulk-bar">
        <button class="bulk-btn download" id="btn-download-sel" onclick="downloadSelected()" disabled>
            <svg viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
            Scarica
        </button>
        <button class="bulk-btn del" id="btn-delete-sel" onclick="deleteSelected()" disabled>
            <svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
            Elimina
        </button>
    </div>

    <!-- Lightbox -->
    <div id="lightbox" class="lightbox" onclick="closeLightbox()">
        <svg style="position: absolute; top: 24px; right: 24px; cursor: pointer;" width="32" height="32" fill="white" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
        <img id="lightbox-img" src="">
    </div>

    <!-- Power dialog -->
    <div class="dialog-backdrop" id="power-dialog" onclick="closePowerDialog(event)">
        <div class="power-dialog">
            <div class="dialog-title">Alimentazione</div>
            <button class="power-option reboot" onclick="systemReboot()">
                <svg viewBox="0 0 24 24"><path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/></svg>
                <div class="power-option-label">
                    Riavvia
                    <span>Il Raspberry Pi si riavvierà</span>
                </div>
            </button>
            <button class="power-option shutdown" onclick="systemShutdown()">
                <svg viewBox="0 0 24 24"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42C17.99 7.86 19 9.81 19 12c0 3.87-3.13 7-7 7s-7-3.13-7-7c0-2.19 1.01-4.14 2.58-5.42L6.17 5.17C4.23 6.82 3 9.26 3 12c0 4.97 4.03 9 9 9s9-4.03 9-9c0-2.74-1.23-5.18-3.17-6.83z"/></svg>
                <div class="power-option-label">
                    Spegni
                    <span>Il Raspberry Pi si spegnerà</span>
                </div>
            </button>
            <button class="dialog-cancel" onclick="closePowerDialog()">Annulla</button>
        </div>
    </div>

    <script>
        // ── Selection mode ────────────────────────────────────────────────
        let selectMode = false;
        const selected = new Set();

        function toggleSelectMode() {{
            selectMode ? exitSelectMode() : enterSelectMode();
        }}

        function enterSelectMode() {{
            selectMode = true;
            document.body.classList.add('select-mode');
            document.getElementById('normal-bar').style.display = 'none';
            document.getElementById('sel-bar').classList.add('visible');
            updateSelectionUI();
        }}

        function exitSelectMode() {{
            selectMode = false;
            selected.clear();
            document.body.classList.remove('select-mode');
            document.getElementById('normal-bar').style.display = '';
            document.getElementById('sel-bar').classList.remove('visible');
            document.getElementById('bulk-bar').classList.remove('visible');
            document.querySelectorAll('.m3-card.selected').forEach(c => c.classList.remove('selected'));
        }}

        function selectAll() {{
            document.querySelectorAll('.m3-card[data-filename]').forEach(card => {{
                selected.add(card.dataset.filename);
                card.classList.add('selected');
            }});
            updateSelectionUI();
        }}

        function toggleCard(card, filename) {{
            if (!selectMode) return;
            if (selected.has(filename)) {{
                selected.delete(filename);
                card.classList.remove('selected');
            }} else {{
                selected.add(filename);
                card.classList.add('selected');
            }}
            updateSelectionUI();
        }}

        function updateSelectionUI() {{
            const n = selected.size;
            document.getElementById('sel-count').textContent = n === 1 ? '1 selezionata' : n + ' selezionate';
            const hasAny = n > 0;
            document.getElementById('btn-download-sel').disabled = !hasAny;
            document.getElementById('btn-delete-sel').disabled = !hasAny;
            document.getElementById('bulk-bar').classList.toggle('visible', hasAny);
        }}

        // ── Download ZIP ──────────────────────────────────────────────────
        async function downloadSelected() {{
            if (!selected.size) return;
            const btn = document.getElementById('btn-download-sel');
            btn.disabled = true;
            btn.textContent = 'Preparazione…';
            try {{
                const res = await fetch('/download_zip', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{files: [...selected]}})
                }});
                if (!res.ok) throw new Error('Server error');
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                const cd = res.headers.get('Content-Disposition') || '';
                const m = cd.match(/filename="?([^"]+)"?/);
                a.download = m ? m[1] : 'foto.zip';
                a.click();
                URL.revokeObjectURL(url);
            }} catch(e) {{
                alert('Errore durante il download: ' + e.message);
            }} finally {{
                btn.disabled = false;
                btn.innerHTML = '<svg viewBox="0 0 24 24" style="width:20px;height:20px;fill:currentColor"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg> Scarica';
                updateSelectionUI();
            }}
        }}

        // ── Delete selected ───────────────────────────────────────────────
        async function deleteSelected() {{
            if (!selected.size) return;
            const n = selected.size;
            if (!confirm('Eliminare definitivamente ' + n + ' foto?')) return;
            const btn = document.getElementById('btn-delete-sel');
            btn.disabled = true;
            btn.textContent = 'Eliminazione…';
            try {{
                const res = await fetch('/delete_multiple', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{files: [...selected]}})
                }});
                const data = await res.json();
                data.deleted.forEach(name => {{
                    const card = document.querySelector('.m3-card[data-filename="' + name + '"]');
                    if (card) card.remove();
                    selected.delete(name);
                }});
                if (data.errors.length) {{
                    alert('Alcuni file non trovati: ' + data.errors.join(', '));
                }}
                const total = document.querySelectorAll('.m3-card[data-filename]').length;
                const ph = document.getElementById('photo-count');
                if (ph) ph.textContent = total + ' Scatti salvati';
            }} catch(e) {{
                alert('Errore: ' + e.message);
            }} finally {{
                exitSelectMode();
            }}
        }}

        // ── Lightbox ──────────────────────────────────────────────────────
        function openLightbox(src) {{
            if (selectMode) return;
            document.getElementById('lightbox-img').src = src;
            document.getElementById('lightbox').classList.add('active');
            document.body.style.overflow = 'hidden';
        }}
        function closeLightbox() {{
            document.getElementById('lightbox').classList.remove('active');
            document.body.style.overflow = 'auto';
        }}

        // ── Power dialog ──────────────────────────────────────────────────
        function openPowerDialog() {{
            document.getElementById('power-dialog').classList.add('open');
        }}
        function closePowerDialog(e) {{
            if (!e || e.target === document.getElementById('power-dialog'))
                document.getElementById('power-dialog').classList.remove('open');
        }}

        async function systemReboot() {{
            if (!confirm('Riavviare il Raspberry Pi?')) return;
            closePowerDialog();
            await fetch('/system/reboot', {{method: 'POST'}});
            alert('Riavvio in corso… la pagina non risponderà per qualche minuto.');
        }}

        async function systemShutdown() {{
            if (!confirm('Spegnere il Raspberry Pi?')) return;
            closePowerDialog();
            await fetch('/system/shutdown', {{method: 'POST'}});
            alert('Spegnimento in corso…');
        }}
    </script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

def run_server():
    """Start the server"""
    server_address = ('', PORT)
    try:
        # Use custom ThreadedHTTPServer
        httpd = ThreadedHTTPServer(server_address, PhotoHandler)
        print(f"Starting photo server on port {PORT}")
        httpd.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except:
            pass

if __name__ == '__main__':
    run_server()
            background: transparent;
            border: none;
            color: var(--m3-on-surface);
            width: 48px;
            height: 48px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: background 0.2s;
            text-decoration: none;
        }
        .icon-btn:active { background: rgba(255,255,255,0.1); }
        .icon-btn svg { width: 24px; height: 24px; }
        .btn-tonal svg { width: 18px; height: 18px; }
        .fab-capture svg { width: 36px; height: 36px; }
        
        .app-title {
            margin-left: 8px;
            font-size: 22px;
            font-weight: 400;
        }

        /* Preview Container */
        .preview-wrapper {
            flex: 1;
            width: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            background: #000;
        }

        #preview { 
            max-width: 100%; 
            max-height: 100%; 
            object-fit: contain; 
            background: #111;
            width: 100%;
            height: 100%;
        }

        .status-overlay {
            position: absolute;
            top: 16px;
            right: 16px;
            background: rgba(0,0,0,0.5);
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 12px;
            display: flex;
            align-items: center;
            gap: 8px;
            backdrop-filter: blur(4px);
        }

        .pulse {
            width: 8px;
            height: 8px;
            background: #ff5252;
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.5); opacity: 0.5; }
            100% { transform: scale(1); opacity: 1; }
        }

        /* Bottom Control Panel */
        .control-panel {
            width: 100%;
            background: #1C1B1F;
            padding: 24px 16px 40px 16px;
            box-sizing: border-box;
            border-radius: 28px 28px 0 0;
            display: flex;
            flex-direction: column;
            gap: 24px;
            box-shadow: 0 -4px 12px rgba(0,0,0,0.3);
            z-index: 10;
        }

        .controls-row {
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            align-items: center;
            gap: 16px;
        }

        .m3-card {
            background: var(--m3-surface-variant);
            border-radius: 12px;
            padding: 8px 12px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 4px;
            min-width: 100px;
        }

        .label {
            font-size: 10px;
            font-weight: 500;
            color: var(--m3-on-surface-variant);
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }

        .value {
            font-size: 16px;
            font-weight: 500;
            color: var(--m3-on-surface);
            min-width: 60px;
            text-align: center;
        }

        .adj-btns { display: flex; gap: 4px; }

        .btn-tonal {
            background: var(--m3-secondary);
            color: var(--m3-on-primary);
            border: none;
            height: 32px;
            width: 32px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .btn-tonal .material-symbols-outlined { font-size: 18px; }

        /* Capture Button - Rounded & Broken (Circle) */
        .fab-capture {
            width: 72px;
            height: 72px;
            border-radius: 50%;
            background: var(--m3-primary);
            color: var(--m3-on-primary);
            border: none;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 4px 8px rgba(0,0,0,0.4);
            transition: transform 0.1s, box-shadow 0.2s;
        }
        .fab-capture:active { transform: scale(0.95); }
        .fab-capture .material-symbols-outlined { font-size: 36px; }

        /* LANDSCAPE MODE (Direct UI update) */
        @media (orientation: landscape) {
            .app-bar { display: none !important; }
            .preview-wrapper { 
                position: fixed; 
                inset: 0; 
                z-index: 1; 
                height: 100vh;
                width: 100vw;
                background: #000;
                display: flex;
                align-items: center;
                justify-content: center;
            }
            #preview { 
                max-width: 100%; 
                max-height: 100%; 
                width: auto;
                height: auto;
                object-fit: contain; 
            }
            
            .control-panel {
                position: fixed;
                bottom: 16px;
                left: 50%;
                transform: translateX(-50%);
                width: auto;
                min-width: 420px;
                border-radius: 40px;
                padding: 12px 24px;
                background: rgba(28, 27, 31, 0.85);
                backdrop-filter: blur(16px);
                border: 1px solid rgba(255,255,255,0.15);
                gap: 20px;
                z-index: 100;
                
                /* Fix alignment with flexbox */
                display: flex !important;
                flex-direction: row !important;
                align-items: center;
                justify-content: center;
            }
            
            .controls-row { 
                display: flex !important;
                flex-direction: row;
                align-items: center;
                justify-content: center;
                gap: 20px;
            }
            
            .m3-card { 
                background: transparent; 
                padding: 0; 
                border: none; 
                box-shadow: none;
                min-width: auto;
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 8px;
            }
            
            .fab-capture { 
                width: 64px; 
                height: 64px; 
                flex-shrink: 0;
            }
            
            .label { display: none; }
            .value { font-size: 14px; min-width: auto; }
            
            /* Persistent Float Back */
            .back-container {
                position: fixed;
                top: 16px;
                left: 16px;
                z-index: 1000;
                display: block !important;
            }
        }

        .offline-overlay {
            position: absolute;
            inset: 0;
            background: rgba(0,0,0,0.8);
            display: none;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 16px;
            z-index: 5;
        }

        .back-container { display: none; }
    </style>
</head>
<body>
    <header class="app-bar">
        <a href="/" class="icon-btn">
            <svg fill="currentColor" viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
        </a>
        <div class="app-title">Controllo Live</div>
    </header>

    <div class="back-container">
        <a href="/" class="icon-btn" style="background: rgba(0,0,0,0.5); backdrop-filter: blur(8px); border-radius: 50%;">
            <svg fill="white" viewBox="0 0 24 24"><path d="M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z"/></svg>
        </a>
    </div>

    <main class="preview-wrapper">
        <img id="preview" src="/stream.mjpg" alt="Stream Camera">
        
        <div class="status-overlay">
            <div class="pulse"></div>
            <span>LIVE <span id="res-info" style="opacity: 0.6; font-size: 10px; margin-left: 4px"></span></span>
        </div>

        <div id="offline-msg" class="offline-overlay">
            <svg width="48" height="48" fill="var(--m3-outline)" viewBox="0 0 24 24"><path d="M9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z"/><circle cx="12" cy="12" r="3"/></svg>
            <span style="font-weight:500">Telecomando non attivo</span>
            <span style="font-size: 14px; color: var(--m3-on-surface-variant)">Tocca lo schermo della camera</span>
        </div>
    </main>

    <footer class="control-panel">
        <div class="controls-row">
            <!-- ISO -->
            <div class="m3-card">
                <span class="label">ISO</span>
                <span id="iso-val" class="value">--</span>
                <div class="adj-btns">
                    <button onclick="sendCommand('ISO_DOWN')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13H5v-2h14v2z"/></svg></button>
                    <button onclick="sendCommand('ISO_UP')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></button>
                </div>
            </div>

            <!-- Capture -->
            <button onclick="sendCommand('CAPTURE')" class="fab-capture">
                <svg fill="currentColor" viewBox="0 0 24 24"><path d="M9 2L7.17 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2h-3.17L15 2H9zm3 15c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5z"/><circle cx="12" cy="12" r="3"/></svg>
            </button>

            <!-- Shutter -->
            <div class="m3-card">
                <span class="label">Otturatore</span>
                <span id="shutter-val" class="value">--</span>
                <div class="adj-btns">
                    <button onclick="sendCommand('SHUTTER_DOWN')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13H5v-2h14v2z"/></svg></button>
                    <button onclick="sendCommand('SHUTTER_UP')" class="btn-tonal"><svg fill="currentColor" viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg></button>
                </div>
            </div>
        </div>
    </footer>


    <script>
        function sendCommand(cmd) {
            fetch('/api/command', {
                method: 'POST',
                body: JSON.stringify({command: cmd}),
                headers: {'Content-Type': 'application/json'}
            });
        }

        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('iso-val').textContent = data.iso;
                    document.getElementById('shutter-val').textContent = data.shutter;
                    
                    const offline = document.getElementById('offline-msg');
                    const preview = document.getElementById('preview');
                    const backBtn = document.querySelector('.back-container');
                    
                    // Show frame dimensions in the LIVE badge for diagnosis
                    if (preview.naturalWidth > 0) {
                        document.getElementById('res-info').textContent = `${preview.naturalWidth}x${preview.naturalHeight}`;
                    }
                    
                    const isLandscape = window.matchMedia("(orientation: landscape)").matches;
                    
                    if (data.mode === 'remote') {
                        offline.style.display = 'none';
                        preview.style.opacity = 1;
                    } else {
                        preview.style.opacity = 0.3;
                        offline.style.display = 'flex';
                    }
                    
                    if (isLandscape) {
                        backBtn.style.display = 'block';
                    } else {
                        backBtn.style.display = 'none';
                    }
                })
                .catch(e => console.log('Conn error'));
        }

        // Cleanup when page closes
        window.addEventListener('beforeunload', function() {
            sendCommand('STOP_REMOTE');
        });
        
        // Also cleanup when navigating away
        window.addEventListener('pagehide', function() {
            sendCommand('STOP_REMOTE');
        });

        // Heartbeats - reduced to 3s intervals for faster detection
        sendCommand('START_REMOTE');
        setInterval(updateStatus, 1000);
        setInterval(() => sendCommand('HEARTBEAT'), 3000);
    </script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def list_directory(self, path):
        """Override to show a beautiful Material Design 3 photo gallery"""
        try:
            photos = sorted(PHOTOS_DIR.glob("*.jpg"), reverse=True)
        except OSError:
            self.send_error(404, "Cannot list directory")
            return None
        
        try:
            hostname = socket.gethostname()
            ip_address = socket.gethostbyname(hostname)
        except:
            ip_address = "localhost"
        
        photo_count = len(photos)
        
        html = f"""
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Galleria | Pi Camera</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,100..900;1,14..32,100..900&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" />
    <style>
        :root {{
            --m3-surface: #1C1B1F;
            --m3-on-surface: #E6E1E5;
            --m3-surface-container: #2B2930;
            --m3-primary: #D0BCFF;
            --m3-on-primary: #381E72;
            --m3-secondary-container: #4A4458;
            --m3-on-secondary-container: #E8DEF8;
            --m3-outline: #938F99;
            --m3-error: #F2B8B5;
        }}
        
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{
            font-family: 'Inter', sans-serif;
            background: var(--m3-surface);
            color: var(--m3-on-surface);
            min-height: 100vh;
            padding-bottom: 100px;
        }}

        /* App Bar */
        .app-bar {{
            height: 64px;
            display: flex;
            align-items: center;
            padding: 0 16px;
            position: sticky;
            top: 0;
            background: var(--m3-surface);
            z-index: 10;
        }}

        .app-title {{ font-size: 24px; font-weight: 400; flex: 1; }}

        /* Info Banner */
        .info-header {{
            padding: 16px;
            background: var(--m3-surface-container);
            margin: 16px;
            border-radius: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 14px;
            color: var(--m3-on-secondary-container);
        }}

        .gallery {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 16px;
            padding: 16px;
        }}

        .m3-card {{
            background: var(--m3-surface-container);
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.2s;
        }}

        .m3-card img {{
            width: 100%;
            height: 200px;
            object-fit: cover;
            cursor: pointer;
        }}

        .card-content {{ padding: 12px; flex: 1; }}
        .file-name {{ font-size: 14px; font-weight: 500; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; }}
        .file-date {{ font-size: 12px; color: var(--m3-outline); }}

        .card-actions {{
            padding: 8px;
            display: flex;
            justify-content: flex-end;
            gap: 8px;
            border-top: 1px solid rgba(255,255,255,0.05);
        }}

        .text-btn {{
            background: transparent;
            border: none;
            color: var(--m3-primary);
            padding: 8px 12px;
            border-radius: 8px;
            font-weight: 500;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .text-btn.delete {{ color: var(--m3-error); }}
        .text-btn:hover {{ background: rgba(208, 188, 255, 0.08); }}
        .text-btn.delete:hover {{ background: rgba(242, 184, 181, 0.08); }}

        /* FAB */
        .fab {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            height: 56px;
            padding: 0 24px;
            border-radius: 16px;
            background: var(--m3-primary);
            color: var(--m3-on-primary);
            display: flex;
            align-items: center;
            gap: 12px;
            text-decoration: none;
            font-weight: 500;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
            transition: box-shadow 0.2s, transform 0.2s;
            z-index: 100;
        }}
        .fab:active {{ transform: scale(0.95); }}

        /* Lightbox */
        .lightbox {{
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.95);
            z-index: 1000;
            justify-content: center;
            align-items: center;
            padding: 24px;
        }}
        .lightbox.active {{ display: flex; }}
        .lightbox img {{ max-width: 100%; max-height: 100%; border-radius: 8px; }}
        .close-btn {{
            position: absolute;
            top: 24px;
            right: 24px;
            color: white;
            cursor: pointer;
            font-size: 32px;
        }}
    </style>
</head>
<body>
    <header class="app-bar">
        <h1 class="app-title">Foto Camera</h1>
        <button onclick="remoteShutdown()" class="icon-btn" style="color: var(--m3-error);">
            <svg fill="currentColor" viewBox="0 0 24 24"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42C17.99 7.86 19 9.81 19 12c0 3.87-3.13 7-7 7s-7-3.13-7-7c0-2.19 1.01-4.14 2.58-5.42L6.17 5.17C4.23 6.82 3 9.26 3 12c0 4.97 4.03 9 9 9s9-4.03 9-9c0-2.74-1.23-5.18-3.17-6.83z"/></svg>
        </button>
    </header>

    <div class="info-header">
        <span>{photo_count} Scatti salvati</span>
    </div>

    <main class="gallery">
"""
        if not photos:
            html += """<div style="grid-column: 1/-1; text-align: center; padding: 64px; color: var(--m3-outline)">
                <svg width="64" height="64" fill="currentColor" viewBox="0 0 24 24"><path d="M22 16V4c0-1.1-.9-2-2-2H8c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2zm-11-4l2.03 2.71L16 11l4 5H8l3-4zM2 6v14c0 1.1.9 2 2 2h14v-2H4V6H2z"/></svg>
                <p style="margin-top: 16px">Nessuna foto trovata</p>
            </div>"""
        else:
            for photo in photos:
                filename = photo.name
                timestamp = os.path.getmtime(photo)
                date_str = datetime.datetime.fromtimestamp(timestamp).strftime('%d %b %Y, %H:%M')
                
                html += f"""
        <div class="m3-card">
            <img src="/{filename}" onclick="openLightbox('/{filename}')" loading="lazy">
            <div class="card-content">
                <div class="file-name">{filename}</div>
                <div class="file-date">{date_str}</div>
            </div>
            <div class="card-actions">
                <a href="/{filename}" download class="text-btn">
                    <svg fill="currentColor" viewBox="0 0 24 24" width="18" height="18"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
                    Scarica
                </a>
                <form action="/delete/{filename}" method="post" style="display:contents" onsubmit="return confirm('Eliminare definitivamente questa foto?');">
                    <button type="submit" class="text-btn delete">
                        <svg fill="currentColor" viewBox="0 0 24 24" width="18" height="18"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                        Elimina
                    </button>
                </form>
            </div>
        </div>
"""

        html += f"""
    </main>

    <div style="text-align: center; padding: 20px; font-size: 12px; color: var(--m3-outline); opacity: 0.6;">
        Indirizzo: http://{ip_address}:8080
    </div>

    <a href="/live" class="fab">
        <svg fill="currentColor" viewBox="0 0 24 24" width="24" height="24"><path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>
        LIVE
    </a>

    <div id="lightbox" class="lightbox" onclick="closeLightbox()">
        <svg style="position: absolute; top: 24px; right: 24px; cursor: pointer;" width="32" height="32" fill="white" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
        <img id="lightbox-img" src="">
    </div>

    <script>
        function remoteShutdown() {{
            if (confirm('Vuoi davvero spegnere il Raspberry Pi?')) {{
                fetch('/api/command', {{
                    method: 'POST',
                    body: JSON.stringify({{command: 'SHUTDOWN'}}),
                    headers: {{'Content-Type': 'application/json'}}
                }}).then(() => alert('Comando di spegnimento inviato!'));
            }}
        }}

        function openLightbox(src) {{
            document.getElementById('lightbox-img').src = src;
            document.getElementById('lightbox').classList.add('active');
            document.body.style.overflow = 'hidden';
        }}
        function closeLightbox() {{
            document.getElementById('lightbox').classList.remove('active');
            document.body.style.overflow = 'auto';
        }}
    </script>
</body>
</html>
"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

def run_server():
    """Start the server"""
    server_address = ('', PORT)
    try:
        # Use custom ThreadedHTTPServer
        httpd = ThreadedHTTPServer(server_address, PhotoHandler)
        print(f"Starting photo server on port {PORT}")
        httpd.serve_forever()
    except Exception as e:
        print(f"Server error: {e}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.server_close()
        except:
            pass

if __name__ == '__main__':
    run_server()
