# 🐍 Python Virtual Environment (venv)

Seit März 2026 nutzt dieses Projekt standardmäßig ein **Python Virtual Environment (venv)**. Dies stellt sicher, dass alle Abhängigkeiten (wie `plotly`, `pandas`, `paho-mqtt`) isoliert installiert werden und nicht mit dem Betriebssystem kollidieren.

Dies ist besonders wichtig für moderne Linux-Distributionen (wie Raspberry Pi OS Bookworm), die die Installation von globalen Pip-Paketen blockieren (PEP 668).

---

## 1. Funktionsweise

Der Installer erstellt im Installationsverzeichnis (`/home/pi/E3DC-Control/`) einen Ordner, der die komplette Python-Umgebung enthält.

*   **Standard-Name:** `.venv_e3dc`
*   **Konfiguration:** Der Name wird in `Install/Installer/installer_config.json` gespeichert.
*   **Web-Interface:** Damit PHP weiß, welches Python genutzt werden soll, wird der Name zusätzlich in `/var/www/html/e3dc_paths.json` hinterlegt.

## 2. Installation & Auswahl

Wenn du `install_all.py` (Menüpunkt 18) ausführst, passiert folgendes:

1.  **Scan:** Der Installer sucht im Ordner nach existierenden Umgebungen (Ordner, die mit `.venv` beginnen).
2.  **Auswahl:**
    *   Wird genau eine Umgebung gefunden (z.B. `.venv_e3dc`), wird diese **automatisch** genutzt.
    *   Werden mehrere gefunden, kannst du wählen.
    *   Wird keine gefunden, wirst du gefragt, ob du eine erstellen möchtest (Empfohlen: Ja).
3.  **Setup:** Die benötigten Pakete werden in diesen Ordner installiert.

## 3. Manuelle Nutzung (Terminal)

Wenn du Skripte manuell testen möchtest (z.B. `plot_soc_changes.py`), darfst du **nicht** einfach `python3` tippen, da dies das System-Python nutzt, dem die Pakete fehlen.

### Methode A: Venv aktivieren (Empfohlen)
```bash
cd ~/E3DC-Control
source .venv_e3dc/bin/activate

# Jetzt kannst du ganz normal arbeiten:
python3 plot_soc_changes.py ...
pip list

# Beenden mit:
deactivate
