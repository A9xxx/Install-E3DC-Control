import os
import shutil
import subprocess
from .core import register_command
from .utils import run_command
from .installer_config import get_home_dir, get_install_user, get_install_path
from .logging_manager import get_or_create_logger, log_task_completed

logger = get_or_create_logger("docker_install")

DOCKER_APT_PACKAGES = [
    "docker-ce",
    "docker-ce-cli",
    "containerd.io",
    "docker-buildx-plugin",
    "docker-compose-plugin",
]

DOCKER_CONFLICT_PACKAGES = [
    "docker.io",
    "docker-compose",
    "docker-compose-v2",
    "docker-doc",
    "podman-docker",
    "containerd",
    "runc",
]


def _failed_install_step(label, message):
    print(f"  ✗ {label}: {message}")
    return subprocess.CompletedProcess([label], 1, stderr=message)


def _read_os_release(path="/etc/os-release"):
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    return values


def _docker_apt_platform(os_release):
    distro_id = os_release.get("ID", "").lower()
    id_like = os_release.get("ID_LIKE", "").lower().split()

    if distro_id == "ubuntu" or "ubuntu" in id_like:
        codename = os_release.get("UBUNTU_CODENAME") or os_release.get("VERSION_CODENAME")
        repo_family = "ubuntu"
    elif distro_id in {"debian", "raspbian"} or "debian" in id_like:
        codename = os_release.get("VERSION_CODENAME") or os_release.get("DEBIAN_CODENAME")
        repo_family = "debian"
    else:
        raise RuntimeError(
            "Dieses System ist kein erkannter Debian-/Ubuntu-Ableger. "
            "Bitte Docker manuell nach offizieller Docker-Dokumentation installieren."
        )

    if not codename:
        raise RuntimeError("Kein Versions-Codename in /etc/os-release gefunden.")

    return repo_family, codename


def _run_docker_install_step(label, cmd, **kwargs):
    print(f"  → {label}...")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ✗ {label} fehlgeschlagen.")
    return result


def _capture_install_stdout(label, cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{label} fehlgeschlagen: {result.stderr.strip()}")
    return result.stdout.strip()


def _installed_docker_conflicts():
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${binary:Package}\n", *DOCKER_CONFLICT_PACKAGES],
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _install_docker_from_official_apt_repo():
    """Installiert Docker über das offizielle APT-Repository mit Keyring."""
    try:
        os_release = _read_os_release()
        repo_family, codename = _docker_apt_platform(os_release)
        arch = _capture_install_stdout("Architektur-Erkennung", ["dpkg", "--print-architecture"])
    except Exception as exc:
        return _failed_install_step("Docker-APT-Vorprüfung", str(exc))

    steps = [
        ("APT-Paketliste aktualisieren", ["sudo", "apt-get", "update"]),
        ("APT-Grundpakete installieren", ["sudo", "apt-get", "install", "-y", "ca-certificates", "curl"]),
        ("Docker-Keyring-Verzeichnis anlegen", ["sudo", "install", "-m", "0755", "-d", "/etc/apt/keyrings"]),
        (
            "Docker-GPG-Key laden",
            [
                "sudo",
                "curl",
                "-fsSL",
                "--proto",
                "=https",
                "--tlsv1.2",
                f"https://download.docker.com/linux/{repo_family}/gpg",
                "-o",
                "/etc/apt/keyrings/docker.asc",
            ],
        ),
        ("Docker-GPG-Key lesbar setzen", ["sudo", "chmod", "a+r", "/etc/apt/keyrings/docker.asc"]),
    ]

    for label, cmd in steps:
        result = _run_docker_install_step(label, cmd)
        if result.returncode != 0:
            return result

    source_content = (
        "Types: deb\n"
        f"URIs: https://download.docker.com/linux/{repo_family}\n"
        f"Suites: {codename}\n"
        "Components: stable\n"
        f"Architectures: {arch}\n"
        "Signed-By: /etc/apt/keyrings/docker.asc\n"
    )
    source_result = _run_docker_install_step(
        "Docker-APT-Quelle schreiben",
        ["sudo", "tee", "/etc/apt/sources.list.d/docker.sources"],
        input=source_content,
        text=True,
        stdout=subprocess.DEVNULL,
    )
    if source_result.returncode != 0:
        return source_result

    conflicts = _installed_docker_conflicts()
    if conflicts:
        conflict_result = _run_docker_install_step(
            "Konfliktpakete entfernen",
            ["sudo", "apt-get", "remove", "-y", *conflicts],
        )
        if conflict_result.returncode != 0:
            return conflict_result

    final_steps = [
        ("Docker-APT-Paketliste aktualisieren", ["sudo", "apt-get", "update"]),
        ("Docker Engine und Compose-Plugin installieren", ["sudo", "apt-get", "install", "-y", *DOCKER_APT_PACKAGES]),
        ("Docker-Dienst aktivieren", ["sudo", "systemctl", "enable", "--now", "docker"]),
        ("Docker Compose prüfen", ["sudo", "docker", "compose", "version"]),
    ]
    result = subprocess.CompletedProcess(["docker-install"], 0)
    for label, cmd in final_steps:
        result = _run_docker_install_step(label, cmd)
        if result.returncode != 0:
            return result

    return result

def install_docker_routine():
    print("\n" + "="*60)
    print("  🐳 Docker Auto-Install & Migration")
    print("="*60 + "\n")

    print("Dieser Assistent installiert Docker, beendet die lokalen Dienste")
    print("und migriert deine bestehende Installation (Daten & Config) vollautomatisch")
    print("in die isolierte Docker-Umgebung inklusive Auto-Updates (Watchtower).\n")

    if input("Möchtest du jetzt zu Docker wechseln? (j/n): ").strip().lower() != 'j':
        print("Abbruch.")
        return

    install_user = get_install_user()

    home_dir = get_home_dir(install_user)
    install_path = get_install_path()
    docker_dir = os.path.join(home_dir, "e3dc-docker")
    data_dir = os.path.join(docker_dir, "data")
    logs_dir = os.path.join(docker_dir, "logs")

    # 1. Docker Installation prüfen / ausführen
    print("\n→ Prüfe Docker-Installation...")
    res = run_command("command -v docker")
    if not res['success']:
        print("  Docker nicht gefunden. Installiere Docker aus dem offiziellen APT-Repository...")
        install_res = _install_docker_from_official_apt_repo()
        if install_res.returncode != 0:
            print("\n  ✗ Fehler bei der Docker-Installation! Bitte Internetverbindung prüfen.")
            return
        run_command(f"sudo usermod -aG docker {install_user}")
        print("  ✓ Docker installiert.")
    else:
        print("  ✓ Docker ist bereits installiert.")

    # 2. Lokale Dienste stoppen (Port 80 freimachen & Doppelbetrieb verhindern)
    print("\n→ Beende und deaktiviere lokale Host-Dienste (Konfliktvermeidung)...")
    services_to_stop = [
        "e3dc", "energy_manager", "e3dc-lux-live", "e3dc-idm-live", "e3dc-stiebel-live", "e3dc-dimplex-live", "e3dc-ha",
        "e3dc-notifier", "e3dc-websocket", "e3dc-mqtt-hub", "e3dc-bluelink",
        "apache2", "piguard"
    ]
    for srv in services_to_stop:
        run_command(f"sudo systemctl stop {srv} 2>/dev/null")
        run_command(f"sudo systemctl disable {srv} 2>/dev/null")
    print("  ✓ Lokale Dienste gestoppt.")

    # 3. Verzeichnisse erstellen & migrieren
    print("\n→ Erstelle Docker-Verzeichnisse und migriere Daten...")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    # Config & TXT Files
    config_found = False
    for txt_file in ["e3dc.config.txt", "e3dc.wallbox.txt", "e3dc.strompreise.txt"]:
        src = os.path.join(install_path, txt_file)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(data_dir, txt_file))
            if txt_file == "e3dc.config.txt": config_found = True

    # C++ Statistik-Dateien (.dat)
    for f in os.listdir(install_path):
        if f.endswith(".dat"): shutil.copy2(os.path.join(install_path, f), os.path.join(data_dir, f))

    # Webportal Daten (SQLite, Wallbox CSVs, Archive)
    host_web_data = "/var/www/html/data"
    if os.path.exists(host_web_data):
        run_command(f"sudo cp -r {host_web_data}/* {data_dir}/ 2>/dev/null")

    run_command(f"sudo chown -R {install_user}:{install_user} {docker_dir}")
    print("  ✓ Daten erfolgreich nach ~/e3dc-docker/data kopiert.")

    # 4. docker-compose.yml generieren (inkl. Watchtower)
    compose_content = f"""services:
  e3dc-control:
    # Ohne E3DC_IMAGE_TAG folgt die Installation dem Stable-Tag "latest".
    # Ein bewusster Versions-Pin wird in der nebenliegenden .env gesetzt.
    image: "ghcr.io/a9xxx/install-e3dc-control:${{E3DC_IMAGE_TAG:-latest}}"
    container_name: e3dc-control
    restart: unless-stopped
    network_mode: host # Benötigt für stabile RSCP-Verbindung zum E3DC im LAN
    volumes:
      - ./data:/var/www/html/data
      - ./logs:/var/www/html/logs
    tmpfs:
      - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775
    environment:
      - TZ=Europe/Berlin
      - E3DC_CONTAINER_MODE=1

  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TZ=Europe/Berlin
      - DOCKER_API_VERSION=1.40
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=86400 # Prüft alle 24h, erster Lauf sofort
"""

    with open(os.path.join(docker_dir, "docker-compose.yml"), "w") as f:
        f.write(compose_content)
    print("  ✓ docker-compose.yml (inkl. Watchtower) erstellt.")

    # 5. Docker Container starten
    print("\n→ Lade und starte Docker-Container...")
    
    # Nutze subprocess, damit man die Layer-Downloads in Echtzeit sieht
    subprocess.run(["sudo", "docker", "compose", "pull"], cwd=docker_dir)
    
    up_result = subprocess.run(
        ["sudo", "docker", "compose", "up", "-d"],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    res = {'success': up_result.returncode == 0, 'stderr': up_result.stderr}

    if res['success']:
        print("\n" + "="*60)
        print("✓ DOCKER MIGRATION ERFOLGREICH!")
        print("="*60)
        print("Dein System läuft nun vollständig isoliert in Docker.")
        print("Updates werden zukünftig durch 'Watchtower' jede Nacht um 04:00 Uhr")
        print("vollautomatisch heruntergeladen und installiert.")
        print(f"Deine persistenen Daten liegen sicher in: {data_dir}")

        if not config_found:
            print("\n💡 WICHTIGER HINWEIS: Es wurde noch keine E3DC-Konfiguration gefunden!")
            print("   Öffne nun das Web-Dashboard, gehe in den 'Config Editor' und trage")
            print("   deine E3DC-Zugangsdaten ein. Klicke danach auf 'E3DC-Control Neustart'.")
        log_task_completed("Docker Auto-Install & Migration")
    else:
        print("\n✗ Fehler beim Starten der Container:")
        print(res['stderr'])
        logger.error(f"Docker start error: {res['stderr']}")

register_command("31", "🐳 Zu Docker wechseln (Auto-Install & Migration)", install_docker_routine, sort_order=31)
