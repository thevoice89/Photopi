# Raspberry Pi HQ Camera Application

Un'applicazione fotocamera leggera per Raspberry Pi Zero 2W con modulo HQ Camera, interfaccia touchscreen e controllo GPIO.

## 📋 Caratteristiche

- ✨ **Anteprima live** della fotocamera all'avvio
- 📸 **Scatto foto** tramite pulsante GPIO o touch
- 🎛️ **Controlli manuali** per ISO e tempi di scatto
- 🖼️ **Galleria integrata** per visualizzare le foto
- 🌐 **Server web** per accedere alle foto da smartphone/PC sulla rete locale e controllo remoto della fotocamera
- ⚡ **Menu power** per spegnere il Raspberry o disattivare il display


## 🔧 Requisiti Hardware

- Raspberry Pi Zero 2W (o superiore)
- Modulo HQ Camera
- Display anche touchscreen (qualsiasi risoluzione)
- Pulsante collegato a GPIO 26 e GND
- Raspberry Pi OS Trixie Lite (o superiore)

## 📦 Installazione

### 1. Trasferire i file sul Raspberry Pi


#Copia tutti i file nella directory `~/raspi/` sul tuo Raspberry Pi:
da terminale del computer

cd /Users/davide/Desktop/raspi
scp camera_app.py photo_server.py setup.sh  davide@192.168.1.51:~/raspi/

Collegarsi in ssh

davide@192.168.1.51

##Driver display

sudo apt-get install python3-evdev
sudo rm -rf LCD-show
sudo apt install git
git clone https://github.com/goodtft/LCD-show.git
chmod -R 755 LCD-show
cd LCD-show/
sudo ./MPI3508-show


### 2. Rendere eseguibili gli script

cd raspi
chmod +x setup.sh

./setup.sh
y

**After installation, reboot your Raspberry Pi.**



# 5. Reboot
sudo reboot



## 🎮 Utilizzo

### Interfaccia Camera

Dopo il riavvio, l'applicazione si avvia automaticamente mostrando:

- **Anteprima live della fotocamera**
- **Pulsante POWER** ⚡ (a sinistra) - menu per spegnere o disattivare il display
- **Pulsante GALLERY** 🖼️ - visualizza le foto scattate
- **Pulsante CAPTURE** 🔴 (al centro) - scatta una foto
- **Controlli ISO** (Auto, 100-3200) - usa i pulsanti ▲/▼
- **Controlli Shutter Speed** (Auto, 1/2000s - 1/30s) - usa i pulsanti ▲/▼

### Menu Power

Tocca il pulsante POWER per accedere al menu con le seguenti opzioni:
- **SHUTDOWN** - spegne completamente il Raspberry Pi
- **MONITOR OFF** - disattiva il display (si riaccende al tocco o movimento mouse)
- **START/STOP HOTSPOT** - Attiva/Disattiva la modalità Hotspot per uso fuori casa
- **CANCEL** - chiude il menu

### Scattare Foto

Puoi scattare foto in due modi:
1. **Pulsante fisico GPIO 26** - premi il pulsante collegato

Le foto vengono salvate in `~/photos/` con formato: `photo_YYYYMMDD_HHMMSS.jpg`

### Galleria Foto

Tocca il pulsante "GALLERY" per visualizzare le foto:
- **PREV/NEXT** - naviga tra le foto
- **BACK** - torna alla modalità camera

### Server Web

Il server web si avvia automaticamente sulla porta 8080. Per accedere alle foto:

1. **Trova l'indirizzo IP del Raspberry Pi**:

   hostname -I


2. **Apri il browser** sul tuo smartphone o PC (sulla stessa rete WiFi):

   http://[IP_RASPBERRY]:8080


   Esempio: `http://192.168.1.51:8080`

3. **Visualizza la galleria** - le foto vengono mostrate in una griglia responsive con:
   - Miniature delle foto
   - Data e ora di scatto
   - Lightbox per visualizzazione a schermo intero
   - Auto-refresh ogni 30 secondi
   - Possibilità di eliminarle o scaricarle

4. **Remote control**
   - possibilità di controllare da remoto lo scatto e i valori (si disattiverà lo schermo del raspberry)

### Modalità Outdoor (Hotspot) 🌳

Per usare il Raspberry Pi fuori casa senza router:

1. Premi **POWER** → tocca **START HOTSPOT** (Pulsante Viola).
2. Attendi qualche secondo che la rete venga creata.
3. Con il tuo smartphone/PC connettiti alla rete WiFi:
   - **Nome (SSID)**: `RaspiCam`
   - **Password**: `raspicam_admin`
4. Apri il browser e vai su: `http://10.42.0.1:8080`

Per tornare al WiFi di casa, premi **POWER** → **STOP HOTSPOT** (Pulsante Arancione).

## 🔌 Schema Collegamento GPIO


Raspberry Pi GPIO 26 ──────┐
                           │
                      [Pulsante]
                           │
Raspberry Pi GND ──────────┘


## 🛠️ Gestione Servizi

### Controllare lo stato


# Camera app
sudo systemctl status camera-app

# Web server
sudo systemctl status photo-server


### Avviare/Fermare manualmente


# Avviare
sudo systemctl start camera-app
sudo systemctl start photo-server

# Fermare
sudo systemctl stop camera-app
sudo systemctl stop photo-server


### Visualizzare i log


# Camera app
sudo journalctl -u camera-app -f

# Web server
sudo journalctl -u photo-server -f


### Disabilitare l'avvio automatico

sudo systemctl disable camera-app
sudo systemctl disable photo-server


## 🎨 Personalizzazione

### Modificare la porta del server web

Modifica `photo_server.py`:
```python
PORT = 8080  # Cambia con la porta desiderata
```

Poi riavvia il servizio:
```bash
sudo systemctl restart photo-server
```

### Modificare il pin GPIO

Modifica `camera_app.py`:
```python
GPIO_BUTTON_PIN = 26  # Cambia con il pin desiderato
```

### Modificare la directory delle foto

Modifica entrambi i file `camera_app.py` e `photo_server.py`:
```python
PHOTOS_DIR = Path.home() / "photos"  # Cambia percorso
```

## 🐛 Troubleshooting

### La camera non si avvia

1. Verifica che il modulo camera sia abilitato:
   ```bash
   sudo raspi-config
   # Interface Options → Camera → Enable
   ```

2. Controlla i log:
   ```bash
   sudo journalctl -u camera-app -n 50
   ```

### Il display non mostra nulla

1. Verifica che il framebuffer sia configurato:
   ```bash
   ls -l /dev/fb0
   ```

2. Testa manualmente:
   ```bash
   cd ~/raspi
   python3 camera_app.py
   ```

### Il pulsante GPIO non funziona

1. Verifica il collegamento hardware
2. Testa il pin:
   ```bash
   python3 -c "from gpiozero import Button; b = Button(26); b.wait_for_press(); print('Pressed!')"
   ```

### Il server web non è accessibile

1. Verifica che il servizio sia attivo:
   ```bash
   sudo systemctl status photo-server
   ```

2. Controlla il firewall (se presente):
   ```bash
   sudo ufw allow 8080
   ```

3. Verifica l'IP del Raspberry Pi:
   ```bash
   hostname -I
   ```

### Errore "Permission denied" per GPIO

Aggiungi l'utente al gruppo gpio:
```bash
sudo usermod -a -G gpio $USER
```

Poi riavvia.

## 📝 Note

- L'applicazione è ottimizzata per Raspberry Pi Zero 2W ma funziona su qualsiasi Raspberry Pi
- La risoluzione dell'interfaccia si adatta automaticamente al display
- Le foto vengono salvate in formato JPEG con qualità alta
- Il server web è accessibile solo sulla rete locale (non da Internet)
- **Gestione alimentazione**: Lo script di installazione configura automaticamente i permessi per controllare spegnimento e display
- **Display off**: Usa il controllo del backlight tramite sysfs (compatibile con driver KMS/DRM)
- **Risveglio display**: Al tocco dello schermo o movimento del mouse, il display si riattiva automaticamente

## 🔒 Sicurezza

⚠️ **Importante**: L'autologin disabilita la richiesta di password all'avvio. Usa questa funzione solo su dispositivi in ambienti controllati e sicuri.

## 📄 Licenza

Questo progetto è fornito "as-is" per uso personale.



Buone foto! 📸✨
