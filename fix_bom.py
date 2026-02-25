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
    print("Suche nach Dateien mit UTF-8 BOM...")
    count = 0
    for root, dirs, files in os.walk("."):
        if ".git" in dirs: dirs.remove(".git")
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