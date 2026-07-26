import json
import os
import re
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


def _compose_selected_e3dc_image(docker_dir):
    """Liefert die von Compose aufgelöste E3DC-Image-Referenz."""

    result = subprocess.run(
        ["sudo", "docker", "compose", "config", "--images"],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, (
            "Compose konnte die gewählte Image-Referenz nicht auflösen: "
            f"{result.stderr.strip() or 'unbekannter Fehler'}"
        )

    candidates = [
        line.strip()
        for line in result.stdout.splitlines()
        if "/install-e3dc-control:" in line.strip().lower()
    ]
    if len(candidates) != 1:
        return None, (
            "Compose muss genau eine E3DC-Control-Image-Referenz liefern; "
            f"gefunden: {len(candidates)}."
        )
    return candidates[0], ""


def _normalise_release_version(value):
    return str(value or "").strip().lstrip("vV")


def _version_from_selected_image(selected_image):
    """Liefert die erwartete Version aus einem expliziten Release-Tag."""

    image_name = str(selected_image or "").split("@", 1)[0]
    last_segment = image_name.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None
    tag = last_segment.rsplit(":", 1)[-1].strip()
    if not re.fullmatch(r"v?\d+\.\d+\.\d+[a-z0-9.-]*", tag, flags=re.IGNORECASE):
        return None
    return _normalise_release_version(tag)


def _verify_started_e3dc_container(docker_dir, selected_image):
    """Bindet den gestarteten Container an das lokal gezogene Image und VERSION."""

    image_result = subprocess.run(
        ["sudo", "docker", "image", "inspect", selected_image],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    if image_result.returncode != 0:
        return False, (
            "Das gezogene E3DC-Control-Image ist lokal nicht inspizierbar: "
            f"{image_result.stderr.strip() or selected_image}"
        )
    try:
        image_details = json.loads(image_result.stdout)
        image_info = image_details[0]
        expected_image_id = str(image_info["Id"])
        image_labels = image_info.get("Config", {}).get("Labels") or {}
        image_version = _normalise_release_version(
            image_labels.get("org.opencontainers.image.version")
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        return False, f"Image-Metadaten sind unvollständig oder ungültig: {exc}"

    if not image_version or image_version == "unknown":
        return False, (
            "Das Image besitzt keine belastbare Release-Version "
            "(org.opencontainers.image.version)."
        )
    selected_version = _version_from_selected_image(selected_image)
    if selected_version is not None and image_version != selected_version:
        return False, (
            "Image-Tag und OCI-Release-Version widersprechen sich: "
            f"{selected_version} gegenüber {image_version}."
        )

    ps_result = subprocess.run(
        ["sudo", "docker", "compose", "ps", "-q", "e3dc-control"],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    container_ids = [
        line.strip() for line in ps_result.stdout.splitlines() if line.strip()
    ]
    if ps_result.returncode != 0 or len(container_ids) != 1:
        return False, (
            "Compose meldet nicht genau einen gestarteten E3DC-Control-Container: "
            f"{ps_result.stderr.strip() or len(container_ids)}."
        )

    inspect_result = subprocess.run(
        ["sudo", "docker", "inspect", container_ids[0]],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    if inspect_result.returncode != 0:
        return False, (
            "Der gestartete E3DC-Control-Container ist nicht inspizierbar: "
            f"{inspect_result.stderr.strip() or container_ids[0]}"
        )
    try:
        container_details = json.loads(inspect_result.stdout)
        container_info = container_details[0]
        running = container_info["State"]["Running"] is True
        started_image_id = str(container_info["Image"])
        started_image_ref = str(container_info["Config"]["Image"])
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        return False, f"Container-Metadaten sind unvollständig oder ungültig: {exc}"

    if not running:
        return False, "Der E3DC-Control-Container läuft nach dem Start nicht."
    if started_image_ref != selected_image:
        return False, (
            "Der Container verwendet nicht die von Compose gewählte Referenz: "
            f"{started_image_ref} statt {selected_image}."
        )
    if started_image_id != expected_image_id:
        return False, (
            "Der Container verwendet nicht das soeben gezogene Image: "
            f"{started_image_id} statt {expected_image_id}."
        )

    version_result = subprocess.run(
        [
            "sudo",
            "docker",
            "exec",
            container_ids[0],
            "cat",
            "/app/pi/Install/VERSION",
        ],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    runtime_version = _normalise_release_version(version_result.stdout)
    if version_result.returncode != 0 or not runtime_version:
        return False, (
            "Die gestartete Anwendungsversion ist nicht lesbar: "
            f"{version_result.stderr.strip() or 'VERSION fehlt'}"
        )
    if runtime_version != image_version:
        return False, (
            "Image-Label und gestartete Anwendungsversion widersprechen sich: "
            f"{image_version} gegenüber {runtime_version}."
        )

    return True, (
        f"Image {selected_image} ({expected_image_id[:19]}…, "
        f"Version {runtime_version}) läuft bestätigt."
    )


def _active_host_services(service_names):
    active_services = []
    for service_name in service_names:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            capture_output=True,
        )
        if result.returncode == 0:
            active_services.append(service_name)
    return active_services


def _enabled_host_services(service_names):
    enabled_services = []
    for service_name in service_names:
        result = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", service_name],
            capture_output=True,
        )
        if result.returncode == 0:
            enabled_services.append(service_name)
    return enabled_services


def _stop_active_host_services(active_services):
    stopped_services = []
    for service_name in active_services:
        result = subprocess.run(
            ["sudo", "systemctl", "stop", service_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, stopped_services, (
                f"{service_name} konnte nicht gestoppt werden: "
                f"{result.stderr.strip() or 'unbekannter Fehler'}"
            )
        stopped_services.append(service_name)
    return True, stopped_services, ""


def _disable_host_services(enabled_services):
    disabled_services = []
    for service_name in enabled_services:
        result = subprocess.run(
            ["sudo", "systemctl", "disable", service_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, disabled_services, (
                f"{service_name} konnte nicht deaktiviert werden: "
                f"{result.stderr.strip() or 'unbekannter Fehler'}"
            )
        disabled_services.append(service_name)
    return True, disabled_services, ""


def _enable_host_services(disabled_services):
    failures = []
    for service_name in disabled_services:
        result = subprocess.run(
            ["sudo", "systemctl", "enable", service_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append(
                f"{service_name}: {result.stderr.strip() or result.returncode}"
            )
    return failures


def _restore_host_services(active_services):
    failures = []
    restore_order = [
        service_name for service_name in active_services if service_name != "piguard"
    ]
    if "piguard" in active_services:
        restore_order.append("piguard")
    for service_name in restore_order:
        result = subprocess.run(
            ["sudo", "systemctl", "start", service_name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append(
                f"{service_name}: {result.stderr.strip() or result.returncode}"
            )
    return failures


def _stop_candidate_container(docker_dir):
    try:
        stop_result = subprocess.run(
            ["sudo", "docker", "compose", "stop", "e3dc-control"],
            cwd=docker_dir,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return False, f"Stoppbefehl für den Kandidatencontainer scheiterte: {exc}"
    if stop_result.returncode != 0:
        return False, (
            "Der Kandidatencontainer konnte nicht gestoppt werden: "
            f"{stop_result.stderr.strip() or stop_result.returncode}"
        )
    try:
        running_result = subprocess.run(
            [
                "sudo",
                "docker",
                "compose",
                "ps",
                "-q",
                "--status",
                "running",
                "e3dc-control",
            ],
            cwd=docker_dir,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return False, f"Container-Stoppprüfung scheiterte: {exc}"
    if running_result.returncode != 0:
        return False, (
            "Der Stopp des Kandidatencontainers ist nicht verifizierbar: "
            f"{running_result.stderr.strip() or running_result.returncode}"
        )
    if running_result.stdout.strip():
        return False, "Der Kandidatencontainer läuft trotz Stoppbefehl weiter."
    return True, ""


def install_docker_routine():
    print("\n" + "="*60)
    print("  🐳 Docker Auto-Install & Migration")
    print("="*60 + "\n")

    print("Dieser Assistent installiert Docker, beendet die lokalen Dienste")
    print("und migriert deine bestehende Installation (Daten & Config) vollautomatisch")
    print("in die isolierte Docker-Umgebung. Updates erfolgen standardmäßig bewusst")
    print("über Docker Compose; der nicht mehr gepflegte Watchtower bleibt nur")
    print("für bestehende Installationen als ausdrücklich optionales Profil erhalten.\n")

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

    services_to_stop = [
        "piguard",
        "e3dc", "energy_manager", "e3dc-lux-live", "e3dc-idm-live", "e3dc-stiebel-live", "e3dc-dimplex-live", "e3dc-ha",
        "e3dc-notifier", "e3dc-websocket", "e3dc-mqtt-hub", "e3dc-bluelink",
        "e3dc-forecast-evidence",
        "apache2"
    ]

    # 2. Verzeichnisse und Compose-Vertrag vorbereiten
    print("\n→ Erstelle Docker-Verzeichnisse...")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    # 3. docker-compose.yml generieren (Watchtower nur als Opt-in-Profil)
    compose_content = f"""services:
  e3dc-control:
    # Ohne E3DC_IMAGE_TAG folgt die Installation dem Stable-Tag "latest".
    # Ein bewusster Versions-Pin wird in der nebenliegenden .env gesetzt.
    image: "ghcr.io/a9xxx/install-e3dc-control:${{E3DC_IMAGE_TAG:-latest}}"
    container_name: e3dc-control
    restart: unless-stopped
    network_mode: host # Benötigt für stabile RSCP-Verbindung zum E3DC im LAN
    labels:
      - com.centurylinklabs.watchtower.enable=true
    volumes:
      - ./data:/var/www/html/data
      - ./logs:/var/www/html/logs
      - e3dc_ml:/var/lib/e3dc-control/ml
      - e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence
    tmpfs:
      - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775
    environment:
      - TZ=Europe/Berlin
      - E3DC_CONTAINER_MODE=1

  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    profiles:
      - auto-update
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TZ=Europe/Berlin
      - DOCKER_API_VERSION=1.40
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_LABEL_ENABLE=true
      - WATCHTOWER_POLL_INTERVAL=86400 # Prüft alle 24h, erster Lauf sofort

volumes:
  e3dc_ml:
  e3dc_forecast_evidence:
"""

    with open(os.path.join(docker_dir, "docker-compose.yml"), "w") as f:
        f.write(compose_content)
    print("  ✓ docker-compose.yml mit optionalem Watchtower-Profil erstellt.")

    # 4. Image vollständig laden, solange die Host-Dienste noch laufen.
    print("\n→ Lade Docker-Image...")

    selected_image, selected_image_error = _compose_selected_e3dc_image(docker_dir)
    if selected_image is None:
        print(f"\n✗ Docker-Migration gestoppt: {selected_image_error}")
        logger.error("Docker image selection error: %s", selected_image_error)
        return
    print(f"  → Gewählte Image-Referenz: {selected_image}")

    # Layer-Downloads bleiben sichtbar. Ein fehlgeschlagener Pull ist ein
    # hartes Gate: Ein noch lokal vorhandenes Altimage darf nicht als neue
    # erfolgreiche Migration gestartet und gemeldet werden.
    pull_result = subprocess.run(
        ["sudo", "docker", "compose", "pull", "e3dc-control"],
        cwd=docker_dir,
    )
    if pull_result.returncode != 0:
        print("\n✗ Docker-Migration gestoppt: Image konnte nicht geladen werden.")
        print("  Der vorhandene Container bzw. ein lokales Altimage gilt nicht als Update.")
        logger.error(
            "Docker pull failed for %s with rc=%s",
            selected_image,
            pull_result.returncode,
        )
        return

    # 5. Erst nach erfolgreichem Pull die zuvor aktiven Host-Dienste stoppen.
    # Ihre Boot-Aktivierung bleibt bis zum bestätigten Containerstart erhalten.
    active_host_services = _active_host_services(services_to_stop)
    enabled_host_services = _enabled_host_services(services_to_stop)
    print("\n→ Stoppe aktive Host-Dienste für den Containerstart...")
    stop_ok, stopped_services, stop_error = _stop_active_host_services(
        active_host_services
    )
    if not stop_ok:
        restore_failures = _restore_host_services(stopped_services)
        print(f"\n✗ Docker-Migration gestoppt: {stop_error}")
        if restore_failures:
            print("  Host-Dienste konnten nicht vollständig wiederhergestellt werden:")
            for failure in restore_failures:
                print(f"  - {failure}")
        logger.error("Docker host service stop error: %s", stop_error)
        return

    print("  ✓ Zuvor aktive Host-Dienste gestoppt.")
    config_found = False
    try:
        print("\n→ Migriere Daten in die gestoppte Docker-Instanz...")
        for txt_file in [
            "e3dc.config.txt",
            "e3dc.wallbox.txt",
            "e3dc.strompreise.txt",
        ]:
            src = os.path.join(install_path, txt_file)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(data_dir, txt_file))
                if txt_file == "e3dc.config.txt":
                    config_found = True

        for filename in os.listdir(install_path):
            if filename.endswith(".dat"):
                shutil.copy2(
                    os.path.join(install_path, filename),
                    os.path.join(data_dir, filename),
                )

        host_web_data = "/var/www/html/data"
        if os.path.exists(host_web_data):
            copy_result = subprocess.run(
                ["sudo", "cp", "-a", f"{host_web_data}/.", f"{data_dir}/"],
                capture_output=True,
                text=True,
            )
            if copy_result.returncode != 0:
                raise RuntimeError(
                    "Webportal-Daten konnten nicht vollständig kopiert werden: "
                    f"{copy_result.stderr.strip() or copy_result.returncode}"
                )

        chown_result = subprocess.run(
            [
                "sudo",
                "chown",
                "-R",
                f"{install_user}:{install_user}",
                docker_dir,
            ],
            capture_output=True,
            text=True,
        )
        if chown_result.returncode != 0:
            raise RuntimeError(
                "Docker-Datenrechte konnten nicht gesetzt werden: "
                f"{chown_result.stderr.strip() or chown_result.returncode}"
            )
        print("  ✓ Daten erfolgreich nach ~/e3dc-docker/data kopiert.")

        print("\n→ Starte E3DC-Control-Container...")
        up_result = subprocess.run(
            [
                "sudo",
                "docker",
                "compose",
                "up",
                "-d",
                "--force-recreate",
                "e3dc-control",
            ],
            cwd=docker_dir,
            capture_output=True,
            text=True,
        )
        res = {"success": up_result.returncode == 0, "stderr": up_result.stderr}
        if res["success"]:
            verified, verification_message = _verify_started_e3dc_container(
                docker_dir,
                selected_image,
            )
            res["success"] = verified
            if verified:
                print(f"  ✓ {verification_message}")
            else:
                res["stderr"] = verification_message
    except Exception as exc:
        res = {
            "success": False,
            "stderr": f"Containerstart oder Identitätsprüfung fehlgeschlagen: {exc}",
        }

    disabled_host_services = []
    if res["success"]:
        disable_ok, disabled_host_services, disable_error = _disable_host_services(
            enabled_host_services
        )
        if not disable_ok:
            res = {
                "success": False,
                "stderr": (
                    "Host-Dienste konnten nicht sicher deaktiviert werden: "
                    f"{disable_error}"
                ),
            }

    if res["success"]:
        print("\n" + "="*60)
        print("✓ DOCKER MIGRATION ERFOLGREICH!")
        print("="*60)
        print("Dein System läuft nun vollständig isoliert in Docker.")
        print("Updates werden standardmäßig bewusst über 'docker compose pull'")
        print("und 'docker compose up -d --force-recreate e3dc-control' installiert.")
        print("Das nicht mehr gepflegte Watchtower-Profil bleibt wegen seines")
        print("weitreichenden Docker-Socket-Zugriffs standardmäßig aus.")
        print(f"Deine persistenen Daten liegen sicher in: {data_dir}")
        print("Die optionale PV-Prognosediagnose startet im privaten Docker-Volume")
        print("bewusst mit einer neuen Vergleichshistorie; Bare-Metal-Rohdaten")
        print("werden nicht in den Container kopiert.")

        if not config_found:
            print("\n💡 WICHTIGER HINWEIS: Es wurde noch keine E3DC-Konfiguration gefunden!")
            print("   Öffne nun das Web-Dashboard, gehe in den 'Config Editor' und trage")
            print("   deine E3DC-Zugangsdaten ein. Klicke danach auf 'E3DC-Control Neustart'.")
        log_task_completed("Docker Auto-Install & Migration")
    else:
        print("\n✗ Fehler beim Starten der Container:")
        print(res["stderr"])
        container_stopped, container_stop_error = _stop_candidate_container(
            docker_dir
        )
        if not container_stopped:
            print("\n  ✗ SICHERHEITSSTOPP:")
            print(f"  {container_stop_error}")
            print("  Native Host-Dienste bleiben aus, um Doppelsteuerung zu verhindern.")
            logger.critical(
                "Candidate container stop unconfirmed; host restore blocked: %s",
                container_stop_error,
            )
        else:
            enable_failures = _enable_host_services(disabled_host_services)
            restore_failures = _restore_host_services(active_host_services)
            rollback_failures = enable_failures + restore_failures
            if rollback_failures:
                print("  Host-Vorzustand konnte nicht vollständig wiederhergestellt werden:")
                for failure in rollback_failures:
                    print(f"  - {failure}")
            else:
                print("  ✓ Der native Host-Vorzustand wurde wiederhergestellt.")
        logger.error("Docker start/identity error: %s", res["stderr"])

register_command("31", "🐳 Zu Docker wechseln (Auto-Install & Migration)", install_docker_routine, sort_order=31)
