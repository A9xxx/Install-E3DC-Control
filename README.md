# E3DC-Control Installer

Modularer Installer für E3DC-Control auf Raspberry Pi.

## Voraussetzungen

- Raspberry Pi OS (Bullseye oder neuer)
- Python 3.7+
- sudo-Rechte
- Internetzugang (für Paketinstallation und optional Git-Clone)

## Installation

### Option 1 (empfohlen): Installation über GitHub

```bash
sudo apt update
sudo apt install -y git
cd ~
git clone https://github.com/A9xxx/Install-E3DC-Control.git Install
cd Install
sudo python3 installer_main.py
```

### Option 2: Lokale Installation (ohne GitHub-Download)

Diese Variante ist sinnvoll, falls kein GitHub-Zugriff verfügbar ist oder der Inhalt bereits lokal als Ordner/ZIP vorliegt.

1. Lokalen Install-Ordner bereitstellen, z. B. `/home/<user>/Install`
2. Sicherstellen, dass folgende Inhalte vorhanden sind:
   - `installer_main.py`
   - `Installer/` (mit den Python-Modulen)
3. Installer starten:

```bash
cd /home/<user>/Install
sudo python3 installer_main.py
```

## Wichtige Hinweise zum Ablauf

- Beim Start erfolgt zuerst eine Update-Abfrage.
- Die Abfrage des Installationsbenutzers erfolgt beim Erststart.
- Der Benutzer kann später über einen Menüpunkt geändert werden.
- Rechte können über den Menüpunkt **Rechte prüfen & korrigieren** oder über das Skript `check_permissions.sh` geprüft werden.

## Rechteprüfung per Skript

```bash
cd ~/Install
chmod +x check_permissions.sh
./check_permissions.sh
```

## Start des Installers

```bash
cd ~/Install
sudo python3 installer_main.py
```

