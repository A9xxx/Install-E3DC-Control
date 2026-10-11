import os
import time
import subprocess
from .core import register_command
from .utils import run_command
from .installer_config import get_home_dir, get_install_user, get_install_path
from .logging_manager import get_or_create_logger, log_task_completed

logger = get_or_create_logger("docker_uninstall")

def uninstall_docker_routine():
    print("\n" + "="*60)
    print("  🐳 Docker-Umgebung auflösen (Zurück zu Bare-Metal)")
    print("="*60 + "\n")

    install_user = get_install_user()
    home_dir = get_home_dir(install_user)
    install_path = get_install_path()
    docker_dir = os.path.join(home_dir, "e3dc-docker")
    data_dir = os.path.join(docker_dir, "data")

    if not os.path.exists(docker_dir):
        print("Keine Docker-Installation unter ~/e3dc-docker gefunden.")
        return

    print("Dieser Assistent beendet die Docker-Container, migriert deine")
    print("aktuellen Daten zurück auf das lokale System und reaktiviert")
    print("die lokalen Hintergrunddienste.\n")

    if input("Möchtest du Docker jetzt beenden und zum Host-System zurückkehren? (j/n): ").strip().lower() != 'j':
        print("Abbruch.")
        return

    # 1. Docker Container stoppen und löschen
    print("\n→ Beende und lösche Docker-Container...")
    # Nutze subprocess, damit der Stoppvorgang im Terminal sichtbar ist
    try:
        stopped = subprocess.run("sudo docker compose down", cwd=docker_dir, shell=True)
        containers_stopped = stopped.returncode == 0
    except OSError:
        containers_stopped = False
    if not containers_stopped:
        print("  ⚠ Container ließen sich nicht sauber beenden.")
        if input("Container ließen sich nicht sauber beenden – trotzdem fortfahren? (j/n): ").strip().lower() != "j":
            print("Abbruch. Daten und Host-Dienste wurden nicht verändert.")
            return None
    else:
        print("  ✓ Container entfernt.")

    # 2. Daten migrieren
    print("\n→ Kopiere Daten zurück auf das lokale System...")
    host_web_data = "/var/www/html/data"
    os.makedirs(host_web_data, exist_ok=True)

    if os.path.exists(data_dir):
        # Zuerst alles nach /var/www/html/data kopieren
        run_command(f"sudo cp -r {data_dir}/* {host_web_data}/ 2>/dev/null")
        
        # Dann Configs und Archive (*.txt, *.dat) zurück in den Installationsordner schieben
        run_command(f"sudo mv {host_web_data}/*.txt {install_path}/ 2>/dev/null")
        run_command(f"sudo mv {host_web_data}/*.dat {install_path}/ 2>/dev/null")
        print("  ✓ Daten erfolgreich migriert.")
    else:
        print("  ⚠ Keine Daten im Docker-Verzeichnis gefunden.")

    # 3. Docker-Ordner sichern
    print("\n→ Archiviere alten Docker-Ordner...")
    backup_dir = f"{docker_dir}_backup_{int(time.time())}"
    run_command(f"sudo mv {docker_dir} {backup_dir}")
    print(f"  ✓ Ordner umbenannt zu: {os.path.basename(backup_dir)}")

    # 4. Lokale Dienste wiederherstellen
    print("\n→ Reaktiviere lokale Host-Dienste...")
    run_command("sudo systemctl enable apache2 && sudo systemctl start apache2")
    run_command("sudo systemctl enable e3dc-notifier && sudo systemctl start e3dc-notifier")
    print("  ✓ Grunddienste (Apache, Notifier) gestartet.")

    # 5. Rechte-Wizard ausführen (Rechte setzen & weitere Dienste dynamisch starten)
    print("\n→ Führe finale Rechte- und Service-Prüfung aus...")
    try:
        from .permissions import run_permissions_wizard
        if run_permissions_wizard(headless=True) is not True:
            print("  ✗ Rückwechsel unvollständig. Rechte und Dienste über 3) Rechte prüfen & korrigieren oder 1) Installation / Update prüfen.")
            return False
    except Exception as e:
        print(f"  ✗ Fehler bei der Rechte-Reparatur: {e}")
        return False

    print("\n" + "="*60)
    print("✓ RÜCKWECHSEL ERFOLGREICH!")
    print("="*60)
    print("Das System läuft nun wieder nativ (Bare-Metal) auf deinem Host.")
    if containers_stopped:
        print("Die Docker-Container wurden entfernt und die Live-Daten wiederhergestellt.")
    else:
        print("Die Live-Daten wurden zurückgeführt. Den Zustand der Docker-Container bitte gesondert prüfen.")
    log_task_completed("Docker Uninstall & Revert")

register_command("32", "🐳 Docker auflösen & zum lokalen System zurückkehren", uninstall_docker_routine, sort_order=32)
