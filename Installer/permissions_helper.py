"""
Standardisierter Permission-Helper für konsistente Rechtevergabe
Wird von allen Modulen genutzt um sicherzustellen, dass Berechtigungen
mit permissions.py Standard übereinstimmen.

STANDARDS:
- Install-Verzeichnisse:      install_user:install_user 755
- Webportal-Verzeichnisse:    install_user:www-data      775
- Web-Konfigdateien:          install_user:www-data      664
- Ausführbare Web-Dateien:    install_user:www-data      755
- Log-Dateien:                install_user:www-data      664
- Temp-Verzeichnisse:         install_user:www-data      775
"""

import os
import logging
from .utils import run_command
from .installer_config import get_install_user, get_user_ids, get_www_data_gid


def _resolve_owner_user(owner_user=None):
    return owner_user or get_install_user()


def set_file_ownership(path, owner_user=None, owner_group="www-data"):
    """
    Setzt Ownership einer Datei/Verzeichnis auf gewünschtem Benutzer.
    
    Args:
        path: Datei- oder Verzeichnispath
        owner_user: Besitzender Benutzer (Standard: install_user)
        owner_group: Besitzende Gruppe (Standard: "www-data")
    
    Returns:
        True bei erfolg, False bei Fehler
    """
    if not os.path.exists(path):
        return False

    owner_user = _resolve_owner_user(owner_user)
    
    try:
        result = run_command(f"sudo chown {owner_user}:{owner_group} {path}")
        return result['success']
    except Exception as e:
        logging.error(f"Fehler beim Setzen von Ownership für {path}: {e}")
        return False


def set_directory_ownership_recursive(path, owner_user=None, owner_group="www-data"):
    """
    Setzt Ownership eines Verzeichnisses rekursiv.
    
    Args:
        path: Verzeichnispath
        owner_user: Besitzender Benutzer (Standard: install_user)
        owner_group: Besitzende Gruppe (Standard: "www-data")
    
    Returns:
        True bei erfolg, False bei Fehler
    """
    if not os.path.exists(path):
        return False

    owner_user = _resolve_owner_user(owner_user)
    
    try:
        result = run_command(f"sudo chown -R {owner_user}:{owner_group} {path}")
        return result['success']
    except Exception as e:
        logging.error(f"Fehler beim Setzen von Ownership für {path}: {e}")
        return False


def set_file_permissions(path, mode):
    """
    Setzt Permissions einer Datei/Verzeichnis.
    
    Args:
        path: Datei- oder Verzeichnispath
        mode: Oktalzahl als String (z.B. "664", "755", "775")
    
    Returns:
        True bei erfolg, False bei Fehler
    """
    if not os.path.exists(path):
        return False
    
    try:
        result = run_command(f"sudo chmod {mode} {path}")
        return result['success']
    except Exception as e:
        logging.error(f"Fehler beim Setzen von Permissions für {path}: {e}")
        return False


def set_directory_permissions_recursive(path, mode):
    """
    Setzt Permissions eines Verzeichnisses rekursiv.
    
    Args:
        path: Verzeichnispath
        mode: Oktalzahl als String (z.B. "775")
    
    Returns:
        True bei erfolg, False bei Fehler
    """
    if not os.path.exists(path):
        return False
    
    try:
        result = run_command(f"sudo chmod -R {mode} {path}")
        return result['success']
    except Exception as e:
        logging.error(f"Fehler beim Setzen von Permissions für {path}: {e}")
        return False


def set_web_file(path, executable=False):
    """
    Setzt Standard Web-Datei Berechtigungen (install_user:www-data).
    
    Args:
        path: Dateipath
        executable: True wenn Datei ausführbar sein soll (755), False für 664
    
    Returns:
        True bei erfolg
    """
    mode = "755" if executable else "664"

    set_file_ownership(path, None, "www-data")
    set_file_permissions(path, mode)
    
    return True


def set_web_directory(path, recursive=False):
    """
    Setzt Standard Web-Verzeichnis Berechtigungen (install_user:www-data 775).
    
    Args:
        path: Verzeichnispath
        recursive: True um rekursiv zu machen
    
    Returns:
        True bei erfolg
    """
    if recursive:
        set_directory_ownership_recursive(path, None, "www-data")
        set_directory_permissions_recursive(path, "775")
    else:
        set_file_ownership(path, None, "www-data")
        set_file_permissions(path, "775")
    
    return True


def set_log_file(path):
    """
    Setzt Standard Log-Datei Berechtigungen (install_user:www-data 664).
    
    Args:
        path: Log-Dateipath
    
    Returns:
        True bei erfolg
    """
    set_file_ownership(path, None, "www-data")
    set_file_permissions(path, "664")
    
    return True


def set_log_directory(path, recursive=False):
    """
    Setzt Standard Log-Verzeichnis Berechtigungen (install_user:www-data 775).
    
    Args:
        path: Verzeichnispath
        recursive: True um rekursiv zu machen
    
    Returns:
        True bei erfolg
    """
    if recursive:
        set_directory_ownership_recursive(path, None, "www-data")
        set_directory_permissions_recursive(path, "775")
    else:
        set_file_ownership(path, None, "www-data")
        set_file_permissions(path, "775")
    
    return True


def set_executable_script(path):
    """
    Setzt Standard Python/Shell-Skript Berechtigungen (install_user:www-data 755).
    
    Args:
        path: Skriptpath
    
    Returns:
        True bei erfolg
    """
    set_file_ownership(path, None, "www-data")
    set_file_permissions(path, "755")
    
    return True


def safe_chmod(path, mode):
    """
    Sichere lokale chmod (wenn nicht als root).
    Fallback für Umgebungen wo sudo nicht funktioniert.
    
    Args:
        path: Dateipath
        mode: Oktalzahl als String oder Integer
    
    Returns:
        True bei erfolg
    """
    try:
        if isinstance(mode, str):
            mode_octal = int(mode, 8)
        else:
            mode_octal = mode
        
        os.chmod(path, mode_octal)
        return True
    except Exception:
        # Fallback: try sudo
        try:
            result = run_command(f"sudo chmod {mode} {path}")
            return result['success']
        except:
            return False


def safe_chown(path, uid, gid):
    """
    Sichere lokale chown (wenn nicht als root).
    
    Args:
        path: Dateipath
        uid: User ID
        gid: Group ID
    
    Returns:
        True bei erfolg
    """
    try:
        os.chown(path, uid, gid)
        return True
    except Exception:
        # Fallback: try sudo
        try:
            import pwd
            import grp
            user = pwd.getpwuid(uid).pw_name
            group = grp.getgrgid(gid).gr_name
            result = run_command(f"sudo chown {user}:{group} {path}")
            return result['success']
        except:
            return False
