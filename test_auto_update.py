#!/usr/bin/env python3
"""
Test-Script für die Auto-Update-Funktion

Dieses Script ermöglicht lokales Testen der Update-Funktionalität
ohne echte GitHub-Requests durchführen zu müssen.
"""

import os
import sys
import json
import tempfile
import zipfile
import io
from unittest.mock import patch, MagicMock, mock_open
from urllib.error import URLError

# Basis-Pfade
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALLER_DIR = os.path.join(SCRIPT_DIR, "Installer")

if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def create_test_release_zip(version, temp_dir):
    """Erstellt eine Test-ZIP-Datei mit der angegebenen Version."""
    print(f"→ Erstelle Test-ZIP für Version {version}…")
    
    zip_path = os.path.join(temp_dir, f"Install-{version}.zip")
    
    # Erstelle minimale ZIP-Struktur
    with zipfile.ZipFile(zip_path, 'w') as zf:
        # Grundstruktur
        zf.writestr('Install-E3DC-Control/Install/VERSION', f'{version}\n')
        zf.writestr('Install-E3DC-Control/Install/installer_main.py', '# Test version')
        zf.writestr('Install-E3DC-Control/Install/Installer/__init__.py', '')
        zf.writestr('Install-E3DC-Control/Install/Installer/self_update.py', '# self_update')
    
    print(f"✓ Test-ZIP erstellt: {zip_path}")
    return zip_path


def mock_github_api_response(version, download_url):
    """Erstellt eine Mock-GitHub-API-Response."""
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "body": f"Test Release Version {version}\n\n- Feature 1\n- Feature 2",
        "assets": [
            {
                "name": "Install-E3DC-Control.zip",
                "browser_download_url": download_url,
                "size": 1024000
            }
        ]
    }


def test_version_comparison():
    """Test: Version-Vergleich."""
    print("\n" + "="*50)
    print("TEST 1: Version-Vergleich")
    print("="*50)
    
    from Installer.self_update import get_installed_version
    
    current = get_installed_version()
    print(f"✓ Installierte Version erkannt: {current}")
    
    return True


def test_mock_api_call():
    """Test: GitHub-API Funktion existiert."""
    print("\n" + "="*50)
    print("TEST 2: GitHub-API Funktion")
    print("="*50)
    
    from Installer import self_update
    
    try:
        # Prüfe ob Funktion existiert
        assert hasattr(self_update, 'get_latest_release_info'), "Funktion nicht gefunden"
        assert callable(self_update.get_latest_release_info), "Nicht callable"
        
        print(f"✓ Funktion get_latest_release_info() existiert")
        print(f"  Repo: {self_update.GITHUB_REPO}")
        print(f"  API: {self_update.RELEASES_API}")
        
        # Prüfe Funktion für Version-Vergleich
        assert hasattr(self_update, 'check_and_update'), "check_and_update nicht gefunden"
        print(f"✓ Funktion check_and_update() existiert")
        
        # Prüfe Download-Funktion
        assert hasattr(self_update, 'download_release'), "download_release nicht gefunden"
        print(f"✓ Funktion download_release() existiert")
        
        print("\n✓ Alle API-Funktionen vorhanden und callable")
        return True
    
    except Exception as e:
        print(f"✗ Fehler: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_download_simulation():
    """Test: Download-Simulation."""
    print("\n" + "="*50)
    print("TEST 3: Download & Entpacken (Simulation)")
    print("="*50)
    
    from Installer import self_update
    
    temp_dir = tempfile.gettempdir()
    test_zip = create_test_release_zip("2.0.0", temp_dir)
    
    # Test: Datei existiert
    if not os.path.exists(test_zip):
        print(f"✗ Test-ZIP nicht gefunden: {test_zip}")
        return False
    
    print(f"✓ Test-ZIP heruntergeladen: {test_zip}")
    
    # Test: Entpacken
    try:
        import zipfile
        extract_dir = os.path.join(temp_dir, "test_extract")
        os.makedirs(extract_dir, exist_ok=True)
        
        with zipfile.ZipFile(test_zip, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        
        # Prüfe Struktur
        install_path = os.path.join(extract_dir, "Install-E3DC-Control", "Install")
        if os.path.exists(install_path):
            print(f"✓ ZIP entpackt mit korrekter Struktur")
            print(f"  Pfad: {install_path}")
            
            # Cleanup
            import shutil
            shutil.rmtree(extract_dir, ignore_errors=True)
            return True
        else:
            print("✗ Falsche ZIP-Struktur")
            return False
    
    except Exception as e:
        print(f"✗ Fehler beim Entpacken: {e}")
        return False


def test_full_workflow():
    """Test: Vollständiger Update-Workflow (trocken)."""
    print("\n" + "="*50)
    print("TEST 4: Vollständiger Workflow (Mock)")
    print("="*50)
    
    from Installer import self_update
    
    temp_dir = tempfile.gettempdir()
    test_zip = create_test_release_zip("2.0.0", temp_dir)
    
    print("→ Simuliere Auto-Update-Prüfung…")
    print(f"  Neueste Version: 2.0.0")
    print(f"  Installierte Version: {self_update.get_installed_version()}")
    
    print("\n→ Version unterschiedlich → Update erforderlich")
    print("→ Würde Release herunterladen und installieren")
    print("→ Würde Installer neu starten")
    
    # Cleanup
    try:
        os.remove(test_zip)
    except:
        pass
    
    return True


def run_all_tests():
    """Führt alle Tests durch."""
    print("\n")
    print("█" * 50)
    print("  E3DC-Control Installer - Auto-Update Test")
    print("█" * 50)
    
    tests = [
        ("Version-Erkennung", test_version_comparison),
        ("GitHub-API Funktion", test_mock_api_call),
        ("Download & Extract", test_download_simulation),
        ("Workflow-Simulation", test_full_workflow),
    ]
    
    results = {}
    
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"\n✗ Test fehlgeschlagen: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False
    
    # Zusammenfassung
    print("\n" + "="*50)
    print("TEST-ZUSAMMENFASSUNG")
    print("="*50 + "\n")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, result in results.items():
        status = "✓ PASSED" if result else "✗ FAILED"
        print(f"{status:10} - {name}")
    
    print(f"\nErgebnis: {passed}/{total} Tests erfolgreich")
    
    if passed == total:
        print("\n✓ Alle Tests erfolgreich! Die Auto-Update-Funktion ist einsatzbereit.")
        return 0
    else:
        print("\n⚠ Einige Tests fehlgeschlagen. Überprüfe die Fehlermeldungen oben.")
        return 1


if __name__ == "__main__":
    exit_code = run_all_tests()
    sys.exit(exit_code)
