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
        """Handle POST requests (for delete and commands)"""
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
            display: flex;
            flex-direction: column;
            align-items: center;
            height: 100vh;
            overflow: hidden;
        }

        /* Top App Bar */
        .app-bar {
            width: 100%;
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
