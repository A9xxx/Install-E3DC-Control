#!/usr/bin/env python3
import os

def remove_bom(path):
    try:
        with open(path, 'rb') as f:
            content = f.read()
        # UTF-8 BOM Bytes: EF BB BF
        if content.startswith(b'\xef\xbb\xbf'):
            with open(path, 'wb') as f:
                f.write(content[3:])
            print(f"✓ BOM entfernt: {path}")
            return True
    except Exception as e:
        print(f"⚠ Fehler bei {path}: {e}")
    return False

def main():
    # Nutze das Verzeichnis, in dem das Skript liegt, als Basis
    base_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"Suche nach Dateien mit UTF-8 BOM in {base_dir}...")
    count = 0
    for root, dirs, files in os.walk(base_dir):
        if ".git" in dirs: dirs.remove(".git")
        if "__pycache__" in dirs: dirs.remove("__pycache__")
        for file in files:
            if file.endswith((".py", ".sh", ".php", ".txt", ".md", ".json", ".html", ".css", ".js")):
                if remove_bom(os.path.join(root, file)):
                    count += 1
    
    if count == 0:
        print("Keine Dateien mit BOM gefunden. Alles sauber.")
    else:
        print(f"Fertig. {count} Dateien bereinigt.")

if __name__ == "__main__":
    main()