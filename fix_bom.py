#!/usr/bin/env python3
import os
import json

def remove_bom(path):
    try:
        with open(path, 'rb') as f:
            content = f.read()
        modified = False
        
        # Check for UTF-8 BOM
        if content.startswith(b'\xef\xbb\xbf'):
            content = content[3:]
            modified = True
        # Check for UTF-16 LE BOM (Windows PowerShell Standard)
        elif content.startswith(b'\xff\xfe'):
            content = content.decode('utf-16le').encode('utf-8')
            modified = True
        # Check for UTF-16 BE BOM
        elif content.startswith(b'\xfe\xff'):
            content = content.decode('utf-16be').encode('utf-8')
            modified = True
            
        if modified:
            with open(path, 'wb') as f:
                f.write(content)
            print(f"[OK] BOM entfernt / Encoding repariert: {path}")
            return True
    except Exception as e:
        print(f"[!] Fehler bei {path}: {e}")
    return False

def main():
    install_dir = os.path.dirname(os.path.abspath(__file__))

    # Scan-Verzeichnisse: Installation + deployte Web-App. Nicht das Home-Verzeichnis
    # scannen, sonst laufen alte Install-Backups und Archive in die BOM-Pruefung.
    scan_dirs = [install_dir]

    # /var/www/html/ hinzufügen (wo die PHP-Dateien nach dem Deploy liegen)
    if os.path.isdir("/var/www/html"):
        scan_dirs.append("/var/www/html")

    # Zusätzlich install_path aus e3dc_paths.json lesen (falls abweichend)
    try:
        paths_file = "/var/www/html/e3dc_paths.json"
        if os.path.exists(paths_file):
            with open(paths_file, "r") as f:
                paths = json.load(f)
            ip = paths.get("install_path", "")
            if ip and os.path.isdir(ip) and ip not in scan_dirs:
                scan_dirs.append(ip)
    except Exception:
        pass

    count = 0
    skip_dirs = {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "backups",
        "ramdisk",
        "tmp",
        "logs",
        "history_backups",
        "luxtronik_archive",
        "matter-storage",
    }
    seen = set()

    for base_dir in scan_dirs:
        base_dir = os.path.realpath(base_dir)
        if base_dir in seen:
            continue
        seen.add(base_dir)
        print(f"Suche in: {base_dir}")
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                if file.endswith((".py", ".sh", ".php", ".txt", ".md", ".json", ".html", ".css", ".js")) or file == "VERSION":
                    if remove_bom(os.path.join(root, file)):
                        count += 1

    if count == 0:
        print("Keine Dateien mit BOM gefunden. Alles sauber.")
    else:
        print(f"Fertig. {count} Dateien bereinigt.")

if __name__ == "__main__":
    main()
