import os
import subprocess

def run_command(cmd, timeout=10, use_shell=True):
    """Führt Shell-Kommando aus mit vollständiger Fehlerbehandlung."""
    try:
        result = subprocess.run(
            cmd, shell=use_shell, timeout=timeout,
            capture_output=True, text=True
        )
        return {
            'success': result.returncode == 0,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode
        }
    except subprocess.TimeoutExpired:
        return {'success': False, 'stdout': '', 'stderr': 'Timeout', 'returncode': -1}
    except Exception as e:
        return {'success': False, 'stdout': '', 'stderr': str(e), 'returncode': -1}


def replace_in_file(path, key, new_line):
    """Ersetzt eine Konfigurationszeile in einer Datei."""
    if not os.path.exists(path):
        return False
    
    try:
        lines = []
        found = False
        
        with open(path, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(key + " ") or stripped.startswith(key + "="):
                    lines.append(new_line + "\n")
                    found = True
                else:
                    lines.append(line)
        
        if not found:
            lines.append(new_line + "\n")
        
        with open(path, "w") as f:
            f.writelines(lines)
        
        return True
    except Exception as e:
        return False


def write_param(f, key, value, enabled=True):
    """Schreibt einen Parameter aktiv oder auskommentiert."""
    prefix = "" if enabled else "#"
    f.write(f"{prefix}{key} = {value}\n")


def apt_install(pkg):
    """Installiert apt-Paket wenn nicht vorhanden."""
    print(f"→ Prüfe {pkg}…")
    result = subprocess.run(
        f"dpkg -s {pkg}",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        print(f"→ Installiere {pkg}…")
        cmd_result = run_command(f"sudo apt-get install -y {pkg}", timeout=300)
        if cmd_result['success']:
            print(f"✓ {pkg} installiert.")
        else:
            print(f"⚠ {pkg} möglicherweise nicht korrekt installiert.")
    else:
        print(f"✓ {pkg} bereits installiert.")


def pip_install(pkg):
    """Installiert Python-Paket wenn nicht vorhanden."""
    print(f"→ Prüfe Python-Paket {pkg}…")
    result = subprocess.run(
        f"pip3 show {pkg}",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        print(f"→ Installiere {pkg}…")
        cmd_result = run_command(f"sudo pip3 install {pkg} --break-system-packages", timeout=60)
        if cmd_result['success']:
            print(f"✓ {pkg} installiert.")
        else:
            print(f"⚠ {pkg} möglicherweise nicht korrekt installiert.")
    else:
        print(f"✓ {pkg} bereits installiert.")


def ensure_dir(path):
    """Erstellt Verzeichnis wenn nicht vorhanden."""
    try:
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        return True
    except Exception:
        return False
