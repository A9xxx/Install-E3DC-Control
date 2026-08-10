import json
import os
import re
import shutil
import stat
import subprocess
import time

from .core import register_command
from .utils import run_command
from .installer_config import get_home_dir, get_install_user, get_install_path
from .logging_manager import get_or_create_logger, log_task_completed
from .service_catalog import allowed_services

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

SUPPORTED_DOCKER_ARCHITECTURES = {"amd64", "arm64"}

CONTAINER_READINESS_ATTEMPTS = 30
CONTAINER_READINESS_POLL_SECONDS = 2
CONTAINER_READINESS_EXEC_TIMEOUT_SECONDS = 5
DOCKER_COMPOSE_WAIT_TIMEOUT_SECONDS = 300
CONTAINER_HEALTHCHECK_COMMAND = (
    "/opt/venv/bin/python3",
    "-I",
    "-B",
    "/usr/local/bin/e3dc-docker-healthcheck",
)

HOST_QUIESCE_ATTEMPTS = 10
HOST_QUIESCE_POLL_SECONDS = 1
HOST_SUPERVISOR_SERVICES = (
    "piguard.service",
    "e3dc-ha.service",
    "e3dc-watchdog.service",
)
HOST_HISTORICAL_SERVICES = (
    "e3dc.service",
    "e3dc-grabber.service",
)
HOST_INACTIVE_STATES = {"inactive", "failed"}
HOST_RESTORABLE_ENABLED_STATES = {"enabled", "enabled-runtime"}
HOST_NON_ENABLED_STATES = {
    "disabled",
    "static",
    "indirect",
    "generated",
    "transient",
    "masked",
    "masked-runtime",
}

HOST_WRITER_PYTHON_SCRIPTS = {
    "storage_manager.py",
    "storage_manager_legacy.py",
    "storage_manager_next.py",
    "wallbox_manager.py",
    "energy_manager.py",
    "heizstab_manager.py",
}
HOST_WRITER_PYTHON_MODULES = {
    "Installer.storage_manager",
    "Installer.storage_manager_legacy",
    "Installer.wallbox_manager",
    "Installer.luxtronik.energy_manager",
    "Installer.heizstab_manager",
}
MAX_HOST_CMDLINE_BYTES = 64 * 1024

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


def _normalise_docker_architecture(value):
    architecture = str(value or "").strip().lower()
    aliases = {
        "x86_64": "amd64",
        "x86-64": "amd64",
        "aarch64": "arm64",
        "arm64/v8": "arm64",
    }
    return aliases.get(architecture, architecture)


def _require_supported_docker_architecture(value, source="Docker-Architektur"):
    raw_value = str(value or "").strip() or "unbekannt"
    architecture = _normalise_docker_architecture(value)
    if architecture not in SUPPORTED_DOCKER_ARCHITECTURES:
        raise RuntimeError(
            f"{source} '{raw_value}' wird vom E3DC-Control-Image nicht unterstützt. "
            "Docker benötigt ein 64-Bit-System mit amd64 oder arm64; "
            "armhf, arm/v7 und andere 32-Bit-Architekturen sind nicht freigegeben."
        )
    return architecture


def _docker_server_architecture():
    architecture = _capture_install_stdout(
        "Docker-Server-Architektur",
        ["sudo", "docker", "info", "--format", "{{.Architecture}}"],
    )
    return _require_supported_docker_architecture(
        architecture,
        source="Docker-Server-Architektur",
    )


def _existing_docker_migration_blockers(docker_dir):
    """Inventarisiert vorhandenen E3DC-Docker-Bestand vor jeder Mutation."""

    target = os.path.abspath(str(docker_dir or ""))
    if not target or target == os.path.sep:
        return ("Docker-Zielpfad ist unzulässig.",)

    blockers = []
    if os.path.lexists(target):
        try:
            target_info = os.lstat(target)
        except OSError as exc:
            blockers.append(f"Docker-Zielpfad ist nicht sicher lesbar: {exc}")
        else:
            if not os.path.isdir(target) or os.path.islink(target):
                blockers.append(
                    "Docker-Zielpfad ist kein eindeutiges reales Verzeichnis: "
                    + target
                )
            else:
                try:
                    with os.scandir(target) as entries:
                        existing_entries = sorted(entry.name for entry in entries)
                except OSError as exc:
                    blockers.append(
                        f"Docker-Zielbestand ist nicht sicher inventarisierbar: {exc}"
                    )
                else:
                    if existing_entries:
                        visible = ", ".join(existing_entries[:8])
                        if len(existing_entries) > 8:
                            visible += f", … (+{len(existing_entries) - 8})"
                        blockers.append(
                            "Docker-Zielverzeichnis enthält bereits Compose-/Datenbestand: "
                            + visible
                        )

    docker_binary = shutil.which("docker")
    if not docker_binary:
        return tuple(blockers)

    project_name = os.path.basename(target).strip().lower()

    def _e3dc_image_reference(image):
        reference = str(image or "").strip().lower().split("@", 1)[0]
        image_name = reference.rsplit("/", 1)[-1].split(":", 1)[0]
        return image_name in {"e3dc-control", "install-e3dc-control"}

    container_inventory = subprocess.run(
        [
            "sudo",
            docker_binary,
            "container",
            "ls",
            "-a",
            "--format",
            '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}',
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if container_inventory.returncode != 0:
        blockers.append(
            "Vorhandene Docker-Container sind nicht sicher inventarisierbar: "
            + (
                container_inventory.stderr.strip()
                or str(container_inventory.returncode)
            )
        )
    else:
        for line in container_inventory.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 5:
                blockers.append("Docker-Containerinventar ist strukturell ungültig.")
                break
            container_id, name, image, compose_project, compose_service = fields
            if (
                name == "e3dc-control"
                or compose_service == "e3dc-control"
                or (project_name and compose_project == project_name)
                or _e3dc_image_reference(image)
            ):
                blockers.append(
                    "E3DC-Docker-Container besteht bereits: "
                    f"{name or container_id} ({image or 'Image unbekannt'})."
                )

    volume_inventory = subprocess.run(
        [
            "sudo",
            docker_binary,
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if volume_inventory.returncode != 0:
        blockers.append(
            "Vorhandene Docker-Volumes sind nicht sicher inventarisierbar: "
            + (volume_inventory.stderr.strip() or str(volume_inventory.returncode))
        )
    else:
        managed_volume_suffixes = (
            "e3dc_data",
            "e3dc_logs",
            "e3dc_ml",
            "e3dc_forecast_evidence",
            "e3dc_instance_role",
        )
        managed_volumes = sorted(
            name.strip()
            for name in volume_inventory.stdout.splitlines()
            if name.strip()
            and any(
                name.strip() == suffix or name.strip().endswith("_" + suffix)
                for suffix in managed_volume_suffixes
            )
        )
        if managed_volumes:
            blockers.append(
                "Verwalteter E3DC-Docker-Datenbestand besteht bereits: "
                + ", ".join(managed_volumes[:8])
            )

    return tuple(dict.fromkeys(blockers))


def _pre_engine_docker_state_blockers(docker_root="/var/lib/docker"):
    """Sperrt unbekannten Engine-Bestand, bevor APT Docker erstmals startet."""

    blockers = []
    absolute = os.path.abspath(str(docker_root or ""))
    if absolute != "/var/lib/docker":
        return ("Docker-Datenroot ist nicht kanonisch gebunden.",)
    if os.path.lexists(absolute):
        try:
            info = os.lstat(absolute)
        except OSError as exc:
            blockers.append(f"Docker-Datenroot ist nicht sicher lesbar: {exc}")
        else:
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                blockers.append("/var/lib/docker ist kein eindeutiges reales Verzeichnis.")
            else:
                try:
                    with os.scandir(absolute) as entries:
                        names = sorted(entry.name for entry in entries)
                except OSError as exc:
                    blockers.append(f"Docker-Datenroot ist nicht inventarisierbar: {exc}")
                else:
                    if names:
                        visible = ", ".join(names[:8])
                        if len(names) > 8:
                            visible += f", … (+{len(names) - 8})"
                        blockers.append(
                            "Docker ist noch nicht aufrufbar, aber /var/lib/docker "
                            "enthält unbekannten Bestand: " + visible
                        )
    if os.path.lexists("/run/docker.sock"):
        blockers.append(
            "Docker ist noch nicht aufrufbar, aber /run/docker.sock existiert; "
            "der Engine-Zustand ist nicht eindeutig."
        )
    return tuple(dict.fromkeys(blockers))


def _post_install_docker_state_blockers(docker_dir):
    """Bindet den nach einem frischen Engine-Start neu sichtbaren Bestand."""

    blockers = list(_existing_docker_migration_blockers(docker_dir))
    docker_binary = shutil.which("docker")
    if not docker_binary:
        blockers.append("Docker ist nach der Installation nicht aufrufbar.")
        return tuple(dict.fromkeys(blockers))

    inventories = (
        (
            "Container",
            ["sudo", docker_binary, "container", "ls", "-aq"],
        ),
        (
            "Volume",
            ["sudo", docker_binary, "volume", "ls", "-q"],
        ),
    )
    for label, command in inventories:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            blockers.append(
                f"{label}-Bestand nach Docker-Installation ist nicht inventarisierbar: {exc}"
            )
            continue
        if result.returncode != 0:
            blockers.append(
                f"{label}-Bestand nach Docker-Installation ist nicht inventarisierbar: "
                + (result.stderr.strip() or str(result.returncode))
            )
            continue
        entries = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        if entries:
            blockers.append(
                f"Unerwarteter {label}-Bestand nach frischer Docker-Installation: "
                + ", ".join(entries[:8])
            )
    return tuple(dict.fromkeys(blockers))


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
        arch = _require_supported_docker_architecture(
            _capture_install_stdout(
                "Architektur-Erkennung",
                ["dpkg", "--print-architecture"],
            ),
            source="Debian-Paketarchitektur",
        )
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


def _compose_wait_contract_available(docker_dir):
    """Prüft den für den Freigabestart nötigen Compose-Wartevertrag."""

    result = subprocess.run(
        ["sudo", "docker", "compose", "up", "--help"],
        cwd=docker_dir,
        capture_output=True,
        text=True,
        timeout=15,
    )
    help_text = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        return False, (
            "Docker Compose konnte seinen Startvertrag nicht ausgeben: "
            + (result.stderr.strip() or str(result.returncode))
        )
    missing = tuple(
        option
        for option in ("--wait", "--wait-timeout", "--pull")
        if option not in help_text
    )
    if missing:
        return False, (
            "Docker Compose unterstützt den verpflichtenden Health-Wartevertrag nicht: "
            + ", ".join(missing)
        )
    return True, ""


def _verify_image_healthcheck_contract(docker_dir, selected_image):
    """Bindet den Healthcheck an das gezogene Image statt an Hostskripte."""

    result = subprocess.run(
        ["sudo", "docker", "image", "inspect", selected_image],
        cwd=docker_dir,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return False, (
            "Gezogenes Image ist für den Healthcheck nicht inspizierbar: "
            + (result.stderr.strip() or selected_image)
        )
    try:
        details = json.loads(result.stdout)
        if len(details) != 1 or not isinstance(details[0], dict):
            raise ValueError("Image-Inventar ist nicht eindeutig")
        healthcheck = (
            (details[0].get("Config") or {}).get("Healthcheck") or {}
        )
        actual_test = tuple(healthcheck.get("Test") or ())
    except (AttributeError, TypeError, ValueError) as exc:
        return False, f"Image-Healthcheck-Metadaten sind ungültig: {exc}"
    expected_test = ("CMD", *CONTAINER_HEALTHCHECK_COMMAND)
    if actual_test != expected_test:
        return False, (
            "Gezogenes Image besitzt nicht den gebundenen E3DC-Healthcheck: "
            f"{actual_test!r}."
        )
    return True, ""


def _pulled_image_contract(docker_dir, selected_image):
    """Bindet das soeben gezogene Image vor dem Kandidatenstart an ID/Version."""

    result = subprocess.run(
        ["sudo", "docker", "image", "inspect", selected_image],
        cwd=docker_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Das gezogene Image ist nicht inspizierbar: "
            + (result.stderr.strip() or selected_image)
        )
    try:
        details = json.loads(result.stdout)
        if len(details) != 1 or not isinstance(details[0], dict):
            raise ValueError("kein eindeutiges Image")
        info = details[0]
        image_id = str(info["Id"])
        image_labels = (info.get("Config") or {}).get("Labels") or {}
        image_version = _normalise_release_version(
            image_labels.get("org.opencontainers.image.version")
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Image-Metadaten sind unvollständig: {exc}") from exc
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Das gezogene Image besitzt keine gebundene sha256-ID.")
    if not image_version or image_version == "unknown":
        raise RuntimeError("Das gezogene Image besitzt keine OCI-Release-Version.")
    if not re.fullmatch(r"\d+\.\d+\.\d+[A-Za-z0-9._-]*", image_version):
        raise RuntimeError("Die OCI-Release-Version besitzt kein gültiges Format.")
    selected_version = _version_from_selected_image(selected_image)
    if selected_version is not None and selected_version != image_version:
        raise RuntimeError(
            "Image-Tag und OCI-Release-Version widersprechen sich: "
            f"{selected_version} gegenüber {image_version}."
        )
    return {
        "image": selected_image,
        "image_id": image_id,
        "version": image_version,
    }


def _require_pre_start_image_contract(docker_dir, expected_contract):
    """Bindet Compose unmittelbar vor ``up`` erneut an das geprüfte Image."""

    selected_image, selection_error = _compose_selected_e3dc_image(docker_dir)
    if selected_image is None:
        raise RuntimeError(
            "Compose-Image ist unmittelbar vor dem Start nicht eindeutig: "
            + selection_error
        )
    expected_image = str((expected_contract or {}).get("image") or "")
    if selected_image != expected_image:
        raise RuntimeError(
            "Compose hat die gebundene Image-Referenz vor dem Start verändert: "
            f"{selected_image} statt {expected_image}."
        )

    actual_contract = _pulled_image_contract(docker_dir, selected_image)
    if actual_contract != expected_contract:
        raise RuntimeError(
            "Die lokale Image-ID oder OCI-Release-Version hat sich zwischen "
            "Pull und Containerstart verändert."
        )

    health_contract_ok, health_contract_error = _verify_image_healthcheck_contract(
        docker_dir,
        actual_contract["image_id"],
    )
    if not health_contract_ok:
        raise RuntimeError(health_contract_error)
    return actual_contract


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


def _require_docker_standalone_ha_mode(config, source):
    """Erlaubt Docker ausschließlich im kanonischen Standalone-Modus."""

    if not isinstance(config, dict):
        raise RuntimeError(f"{source} muss ein JSON-Objekt enthalten.")
    ha_mode = config.get("ha_mode")
    if ha_mode != "off":
        raise RuntimeError(
            f"{source} enthält ha_mode={ha_mode!r}. "
            "Docker ist nur mit ha_mode=off zulässig; HA- und Shadow-Betrieb "
            "bleiben Bare-Metal-Funktionen."
        )
    return ha_mode


def _verify_docker_ha_config_candidates(config_paths):
    """Prüft alle bereits vorhandenen Quell- und Zielkonfigurationen."""

    checked_paths = []
    for config_path in dict.fromkeys(os.path.abspath(path) for path in config_paths):
        if not os.path.exists(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Docker-HA-Vorprüfung kann {config_path} nicht sicher lesen: {exc}"
            ) from exc
        _require_docker_standalone_ha_mode(config, config_path)
        checked_paths.append(config_path)
    return tuple(checked_paths)


def _host_services_for_docker_migration():
    """Liefert Supervisoren zuerst, dann historische und aktuelle Host-Units."""

    ordered_services = (
        *HOST_SUPERVISOR_SERVICES,
        *HOST_HISTORICAL_SERVICES,
        *allowed_services(),
        "apache2.service",
    )
    return tuple(dict.fromkeys(ordered_services))


def _read_proc_stat_identity(proc_root, pid):
    with open(
        os.path.join(proc_root, str(pid), "stat"),
        "r",
        encoding="ascii",
        errors="replace",
    ) as handle:
        payload = handle.read(16 * 1024)
    closing = payload.rfind(")")
    if closing < 0:
        raise RuntimeError(f"Prozessstatus für PID {pid} ist ungültig")
    fields = payload[closing + 2 :].split()
    if len(fields) < 20:
        raise RuntimeError(f"Prozessstatus für PID {pid} ist unvollständig")
    return fields[0], int(fields[19])


def _classify_host_writer_process(argv):
    if not argv:
        return ""
    executable = os.path.basename(argv[0])
    if executable == "E3DC-Control":
        return "Legacy E3DC-Control"
    if executable.startswith("python"):
        for index, token in enumerate(argv[1:]):
            if os.path.basename(token) in HOST_WRITER_PYTHON_SCRIPTS:
                return os.path.basename(token)
            if token == "-m" and index + 2 < len(argv):
                module = argv[index + 2]
                if module in HOST_WRITER_PYTHON_MODULES:
                    return module
    if executable in {"sh", "bash", "dash"} and any(
        os.path.basename(token) == "E3DC.sh" for token in argv[1:]
    ):
        return "Legacy E3DC.sh"
    if executable.lower() == "screen":
        for index, token in enumerate(argv[1:], start=1):
            if token in {"-S", "-dmS", "-dms"} and index + 1 < len(argv):
                if argv[index + 1] in {"E3DC", "e3dc"}:
                    return "Legacy screen E3DC"
            if token in {"E3DC", "e3dc"}:
                return "Legacy screen E3DC"
    return ""


def _host_writer_process_snapshot(proc_root="/proc"):
    """Inventarisiert Writer unabhängig von systemd, ohne Prozesse zu beenden."""

    matches = []
    errors = []
    try:
        entries = tuple(os.scandir(proc_root))
    except OSError as exc:
        return {"complete": False, "matches": (), "errors": (str(exc),)}
    for entry in entries:
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            state_before, start_before = _read_proc_stat_identity(
                proc_root,
                entry.name,
            )
            with open(
                os.path.join(proc_root, entry.name, "cmdline"),
                "rb",
                buffering=0,
            ) as handle:
                payload = handle.read(MAX_HOST_CMDLINE_BYTES + 1)
            state_after, start_after = _read_proc_stat_identity(
                proc_root,
                entry.name,
            )
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            errors.append(f"pid={entry.name}: {exc}")
            continue
        if len(payload) > MAX_HOST_CMDLINE_BYTES:
            errors.append(f"pid={entry.name}: cmdline_too_large")
            continue
        if (state_before, start_before) != (state_after, start_after):
            errors.append(f"pid={entry.name}: process_drift")
            continue
        argv = tuple(
            token.decode("utf-8", errors="replace")
            for token in payload.split(b"\0")
            if token
        )
        label = _classify_host_writer_process(argv)
        if label:
            matches.append(
                {
                    "pid": int(entry.name),
                    "start_time": start_after,
                    "state": state_after,
                    "writer": label,
                }
            )
    return {
        "complete": not errors,
        "matches": tuple(
            sorted(matches, key=lambda value: (value["pid"], value["writer"]))
        ),
        "errors": tuple(errors[:8]),
    }


def _verify_no_host_hardware_writers(
    *,
    attempts=HOST_QUIESCE_ATTEMPTS,
    poll_seconds=HOST_QUIESCE_POLL_SECONDS,
):
    """Verlangt zwei stabile, vollständige /proc-Snapshots ohne Host-Writer."""

    stable = 0
    previous_signature = None
    last_detail = "kein vollständiger Prozesssnapshot"
    for attempt in range(max(1, int(attempts))):
        snapshot = _host_writer_process_snapshot()
        if not snapshot["complete"]:
            stable = 0
            previous_signature = None
            last_detail = "Prozessinventar unvollständig: " + "; ".join(
                snapshot["errors"]
            )
        elif snapshot["matches"]:
            stable = 0
            previous_signature = None
            last_detail = "manuelle/alte Hardware-Writer aktiv: " + ", ".join(
                f"{item['writer']} (PID {item['pid']})"
                for item in snapshot["matches"]
            )
        else:
            signature = json.dumps(snapshot, sort_keys=True, default=list)
            if signature == previous_signature:
                stable += 1
            else:
                previous_signature = signature
                stable = 1
            if stable >= 2:
                return True, (
                    "Zwei vollständige /proc-Snapshots bestätigen: kein nativer "
                    "Hardware-Writer und keine Legacy-E3DC-Screen-Sitzung läuft."
                )
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(max(0.0, float(poll_seconds)))
    return False, last_detail


def _verify_container_core_readiness(
    docker_dir,
    container_id,
    *,
    attempts=CONTAINER_READINESS_ATTEMPTS,
    poll_seconds=CONTAINER_READINESS_POLL_SECONDS,
):
    """Verlangt zwei identische grüne Image-Health-Snapshots."""

    total_attempts = max(1, int(attempts))
    poll_delay = max(0.0, float(poll_seconds))
    stable_snapshots = 0
    previous_signature = None
    last_detail = "keine Readiness-Antwort"
    for attempt in range(total_attempts):
        try:
            inspect_result = subprocess.run(
                ["sudo", "docker", "inspect", container_id],
                cwd=docker_dir,
                capture_output=True,
                text=True,
                timeout=CONTAINER_READINESS_EXEC_TIMEOUT_SECONDS,
            )
            if inspect_result.returncode != 0:
                raise RuntimeError(
                    inspect_result.stderr.strip()
                    or f"docker inspect meldete {inspect_result.returncode}"
                )
            details = json.loads(inspect_result.stdout)
            if len(details) != 1 or not isinstance(details[0], dict):
                raise RuntimeError("docker inspect lieferte keinen eindeutigen Container")
            container = details[0]
            state = container.get("State") or {}
            health = state.get("Health") or {}
            if state.get("Running") is not True:
                raise RuntimeError("Container läuft nicht")
            if health.get("Status") != "healthy":
                raise RuntimeError(
                    "Image-Health ist nicht grün: "
                    + str(health.get("Status") or "fehlt")
                )

            result = subprocess.run(
                [
                    "sudo",
                    "docker",
                    "exec",
                    container_id,
                    *CONTAINER_HEALTHCHECK_COMMAND,
                ],
                cwd=docker_dir,
                capture_output=True,
                text=True,
                timeout=CONTAINER_READINESS_EXEC_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            stable_snapshots = 0
            previous_signature = None
            last_detail = "Readiness-Prüfung im Container lief in den Timeout"
        except Exception as exc:
            stable_snapshots = 0
            previous_signature = None
            last_detail = f"Readiness-Prüfung konnte nicht ausgeführt werden: {exc}"
        else:
            if result.returncode == 0:
                process_snapshot = result.stdout.strip()
                try:
                    parsed_snapshot = json.loads(process_snapshot)
                except (TypeError, ValueError) as exc:
                    stable_snapshots = 0
                    previous_signature = None
                    last_detail = f"Health-Snapshot ist kein gültiges JSON: {exc}"
                    parsed_snapshot = None
                if not isinstance(parsed_snapshot, dict) or not parsed_snapshot:
                    stable_snapshots = 0
                    previous_signature = None
                    if parsed_snapshot is not None:
                        last_detail = "Health-Snapshot enthält keinen Dienstsatz"
                else:
                    signature = (
                        str(container.get("Id") or ""),
                        str(container.get("Image") or ""),
                        str((container.get("Config") or {}).get("Image") or ""),
                        int(container.get("RestartCount") or 0),
                        str(state.get("StartedAt") or ""),
                        int(state.get("Pid") or 0),
                        json.dumps(
                            parsed_snapshot,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    if signature == previous_signature:
                        stable_snapshots += 1
                    else:
                        previous_signature = signature
                        stable_snapshots = 1
                if stable_snapshots >= 2:
                    return True, (
                        "Containeridentität, Apache, Docker-Kerndienste und "
                        "konfigurierte Zusatzdienste sind über zwei "
                        "Snapshots stabil bereit."
                    )
            else:
                stable_snapshots = 0
                previous_signature = None
                last_detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"Image-Healthcheck meldete Rückgabecode {result.returncode}"
                )

        if attempt + 1 < total_attempts:
            time.sleep(poll_delay)

    return False, (
        "Die gebundene Container-Readiness wurde nicht erreicht: "
        f"{last_detail}"
    )


def _verify_started_e3dc_container(docker_dir, image_contract):
    """Bindet Containeridentität, VERSION und laufende Kerndienste."""

    selected_image = str((image_contract or {}).get("image") or "")
    expected_image_id = str((image_contract or {}).get("image_id") or "")
    image_version = _normalise_release_version(
        (image_contract or {}).get("version")
    )
    if (
        not selected_image
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id)
        or not image_version
    ):
        return False, "Der vor dem Start gebundene Image-Vertrag ist unvollständig."

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

    ready, readiness_message = _verify_container_core_readiness(
        docker_dir,
        container_ids[0],
    )
    if not ready:
        return False, readiness_message

    return True, (
        f"Image {selected_image} ({expected_image_id[:19]}…, "
        f"Version {runtime_version}) läuft bestätigt; {readiness_message}"
    )


def _read_host_service_state(service_name):
    """Liest systemd-Zustand ohne mehrdeutige is-active-Rückgabecodes."""

    result = subprocess.run(
        [
            "systemctl",
            "show",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=UnitFileState",
            service_name,
        ],
        capture_output=True,
        text=True,
    )
    properties = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value.strip()

    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl show {service_name} fehlgeschlagen: "
            f"{result.stderr.strip() or result.returncode}"
        )

    load_state = properties.get("LoadState", "")
    if load_state == "not-found":
        return {
            "present": False,
            "active_state": "inactive",
            "unit_file_state": "not-found",
        }

    required = {"LoadState", "ActiveState", "UnitFileState"}
    missing = sorted(required.difference(properties))
    if missing:
        raise RuntimeError(
            f"systemctl show {service_name} lieferte keine eindeutigen Felder: "
            + ", ".join(missing)
        )
    if load_state not in {"loaded", "masked", "stub", "merged"}:
        raise RuntimeError(
            f"{service_name} besitzt den nicht freigegebenen LoadState={load_state}."
        )

    active_state = properties["ActiveState"]
    if active_state not in {
        "inactive",
        "failed",
        "active",
        "reloading",
        "activating",
        "deactivating",
    }:
        raise RuntimeError(
            f"{service_name} besitzt den nicht freigegebenen ActiveState={active_state}."
        )

    unit_file_state = properties["UnitFileState"]
    allowed_unit_file_states = (
        HOST_RESTORABLE_ENABLED_STATES | HOST_NON_ENABLED_STATES
    )
    if unit_file_state not in allowed_unit_file_states:
        raise RuntimeError(
            f"{service_name} besitzt den nicht rollback-sicher unterstützten "
            f"UnitFileState={unit_file_state or 'leer'}."
        )
    return {
        "present": True,
        "active_state": active_state,
        "unit_file_state": unit_file_state,
    }


def _snapshot_host_services(service_names):
    return {
        service_name: _read_host_service_state(service_name)
        for service_name in service_names
    }


def _active_host_services(service_names, service_states=None):
    states = service_states or _snapshot_host_services(service_names)
    return [
        service_name
        for service_name in service_names
        if states[service_name]["present"]
        and states[service_name]["active_state"] not in HOST_INACTIVE_STATES
    ]


def _enabled_host_services(service_names, service_states=None):
    states = service_states or _snapshot_host_services(service_names)
    return [
        service_name
        for service_name in service_names
        if states[service_name]["present"]
        and states[service_name]["unit_file_state"]
        in HOST_RESTORABLE_ENABLED_STATES
    ]


def _stop_active_host_services(active_services):
    stopped_services = []
    for service_name in active_services:
        # Auch ein fehlgeschlagener systemctl-Aufruf kann den Dienst bereits
        # gestoppt haben. Der Rollback muss ihn deshalb sicher berücksichtigen.
        stopped_services.append(service_name)
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "stop", service_name],
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            return False, stopped_services, (
                f"{service_name} konnte nicht gestoppt werden: {exc}"
            )
        if result.returncode != 0:
            return False, stopped_services, (
                f"{service_name} konnte nicht gestoppt werden: "
                f"{result.stderr.strip() or 'unbekannter Fehler'}"
            )
    return True, stopped_services, ""


def _disable_host_services(enabled_services, original_states=None):
    disabled_services = []
    for service_name in enabled_services:
        # disable kann Symlinks entfernt haben, bevor systemctl einen Fehler
        # meldet. Daher wird jeder Versuch in den Rollback aufgenommen.
        disabled_services.append(service_name)
        original_state = (original_states or {}).get(service_name, {})
        disable_command = ["sudo", "systemctl", "disable"]
        if original_state.get("unit_file_state") == "enabled-runtime":
            disable_command.append("--runtime")
        disable_command.append(service_name)
        try:
            result = subprocess.run(
                disable_command,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            return False, disabled_services, (
                f"{service_name} konnte nicht deaktiviert werden: {exc}"
            )
        if result.returncode != 0:
            return False, disabled_services, (
                f"{service_name} konnte nicht deaktiviert werden: "
                f"{result.stderr.strip() or 'unbekannter Fehler'}"
            )
    return True, disabled_services, ""


def _enable_host_services(disabled_services, original_states=None):
    failures = []
    for service_name in disabled_services:
        original_state = (original_states or {}).get(service_name, {})
        enable_command = ["sudo", "systemctl", "enable"]
        if original_state.get("unit_file_state") == "enabled-runtime":
            enable_command.append("--runtime")
        enable_command.append(service_name)
        try:
            result = subprocess.run(
                enable_command,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            failures.append(f"{service_name}: {exc}")
            continue
        if result.returncode != 0:
            failures.append(
                f"{service_name}: {result.stderr.strip() or result.returncode}"
            )
    return failures


def _verify_host_services_quiesced(
    service_names,
    *,
    attempts=HOST_QUIESCE_ATTEMPTS,
    poll_seconds=HOST_QUIESCE_POLL_SECONDS,
):
    """Verlangt zwei stabile Snapshots ohne aktive oder aktivierte Host-Unit."""

    total_attempts = max(1, int(attempts))
    poll_delay = max(0.0, float(poll_seconds))
    stable_snapshots = 0
    last_detail = "keine Host-Dienstantwort"
    for attempt in range(total_attempts):
        try:
            states = _snapshot_host_services(service_names)
        except Exception as exc:
            stable_snapshots = 0
            last_detail = f"Host-Dienststatus nicht sicher lesbar: {exc}"
        else:
            active = [
                service_name
                for service_name in service_names
                if states[service_name]["present"]
                and states[service_name]["active_state"] not in HOST_INACTIVE_STATES
            ]
            enabled = [
                service_name
                for service_name in service_names
                if states[service_name]["present"]
                and states[service_name]["unit_file_state"]
                in HOST_RESTORABLE_ENABLED_STATES
            ]
            if not active and not enabled:
                stable_snapshots += 1
                if stable_snapshots >= 2:
                    return True, (
                        "Alle nativen Host-Units sind stabil inaktiv und "
                        "persistierend nicht aktiviert."
                    )
            else:
                stable_snapshots = 0
                details = []
                if active:
                    details.append("noch aktiv: " + ", ".join(active))
                if enabled:
                    details.append("noch aktiviert: " + ", ".join(enabled))
                last_detail = "; ".join(details)

        if attempt + 1 < total_attempts:
            time.sleep(poll_delay)

    return False, (
        "Die Host-Quieszenz wurde nicht stabil erreicht: " + last_detail
    )


def _restore_host_services(active_services):
    failures = []
    restore_order = [
        service_name
        for service_name in active_services
        if service_name not in HOST_SUPERVISOR_SERVICES
    ]
    restore_order.extend(
        service_name
        for service_name in active_services
        if service_name in HOST_SUPERVISOR_SERVICES
    )
    for service_name in restore_order:
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "start", service_name],
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            failures.append(f"{service_name}: {exc}")
            continue
        if result.returncode != 0:
            failures.append(
                f"{service_name}: {result.stderr.strip() or result.returncode}"
            )
    return failures


def _verify_host_services_restored(
    original_states,
    *,
    attempts=HOST_QUIESCE_ATTEMPTS,
    poll_seconds=HOST_QUIESCE_POLL_SECONDS,
):
    """Belegt nach einem Rückfall den ursprünglichen Start-/Enable-Vertrag."""

    service_names = tuple(original_states)
    total_attempts = max(1, int(attempts))
    poll_delay = max(0.0, float(poll_seconds))
    stable_snapshots = 0
    last_detail = "keine Host-Dienstantwort"
    for attempt in range(total_attempts):
        try:
            current_states = _snapshot_host_services(service_names)
        except Exception as exc:
            stable_snapshots = 0
            last_detail = f"Rollback-Status nicht sicher lesbar: {exc}"
        else:
            differences = []
            for service_name in service_names:
                expected = original_states[service_name]
                current = current_states[service_name]
                if current["present"] is not expected["present"]:
                    differences.append(f"{service_name}: Präsenz abweichend")
                    continue
                if not expected["present"]:
                    continue
                if current["unit_file_state"] != expected["unit_file_state"]:
                    differences.append(
                        f"{service_name}: UnitFileState="
                        f"{current['unit_file_state']} statt {expected['unit_file_state']}"
                    )
                expected_active = expected["active_state"] not in HOST_INACTIVE_STATES
                current_active = current["active_state"] == "active"
                if expected_active != current_active:
                    differences.append(
                        f"{service_name}: ActiveState={current['active_state']}"
                    )

            if not differences:
                stable_snapshots += 1
                if stable_snapshots >= 2:
                    return True, "Der native Host-Vorzustand ist stabil wiederhergestellt."
            else:
                stable_snapshots = 0
                last_detail = "; ".join(differences)

        if attempt + 1 < total_attempts:
            time.sleep(poll_delay)

    return False, "Host-Rollback nicht stabil bestätigt: " + last_detail


def _rollback_host_service_state(original_states, stopped_services, disabled_services):
    failures = _enable_host_services(disabled_services, original_states)
    failures.extend(_restore_host_services(stopped_services))
    restored, restore_detail = _verify_host_services_restored(original_states)
    if not restored:
        failures.append(restore_detail)
    return failures


def _stop_candidate_container(docker_dir):
    stop_error = ""
    try:
        stop_result = subprocess.run(
            ["sudo", "docker", "compose", "stop", "e3dc-control"],
            cwd=docker_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        stop_error = f"Stoppbefehl scheiterte: {exc}"
    else:
        if stop_result.returncode != 0:
            stop_error = stop_result.stderr.strip() or str(stop_result.returncode)
    for snapshot_number in (1, 2):
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
                timeout=15,
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
        if snapshot_number == 1:
            time.sleep(1)
    try:
        down_result = subprocess.run(
            [
                "sudo",
                "docker",
                "compose",
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            cwd=docker_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return False, f"Compose-Kandidatenabbau scheiterte: {exc}"
    if down_result.returncode != 0:
        return False, (
            "Der gestoppte Kandidat konnte nicht vollständig abgebaut werden: "
            f"{down_result.stderr.strip() or stop_error or down_result.returncode}"
        )
    for snapshot_number in (1, 2):
        try:
            remaining_result = subprocess.run(
                [
                    "sudo",
                    "docker",
                    "compose",
                    "ps",
                    "-q",
                    "-a",
                    "e3dc-control",
                ],
                cwd=docker_dir,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            return False, f"Container-Abbauprüfung scheiterte: {exc}"
        if remaining_result.returncode != 0 or remaining_result.stdout.strip():
            return False, (
                "Der Kandidatenabbau ist nicht verifiziert: "
                f"{remaining_result.stderr.strip() or remaining_result.stdout.strip() or remaining_result.returncode}"
            )
        if snapshot_number == 1:
            time.sleep(1)
    return True, ""


def _capture_empty_docker_target(docker_dir):
    target = os.path.abspath(str(docker_dir or ""))
    parent = os.path.dirname(target)
    if (
        target == os.path.sep
        or os.path.basename(target) != "e3dc-docker"
        or os.path.realpath(parent) != parent
        or not os.path.isdir(parent)
    ):
        raise RuntimeError("Docker-Zielpfad ist nicht transaktionssicher gebunden.")
    parent_info = os.stat(parent, follow_symlinks=False)
    snapshot = {
        "target": target,
        "parent": parent,
        "parent_dev": parent_info.st_dev,
        "parent_ino": parent_info.st_ino,
        "existed": False,
    }
    if not os.path.lexists(target):
        return snapshot
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("Docker-Ziel ist kein eindeutiges reales Verzeichnis.")
    with os.scandir(target) as entries:
        if any(True for _entry in entries):
            raise RuntimeError("Docker-Ziel ist vor Transaktionsbeginn nicht leer.")
    snapshot.update(
        {
            "existed": True,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
        }
    )
    return snapshot


def _restore_empty_docker_target(snapshot):
    target = snapshot["target"]
    parent_info = os.stat(snapshot["parent"], follow_symlinks=False)
    if (parent_info.st_dev, parent_info.st_ino) != (
        snapshot["parent_dev"],
        snapshot["parent_ino"],
    ):
        raise RuntimeError("Elternpfad des Docker-Ziels driftete während der Migration.")
    if os.path.lexists(target):
        info = os.lstat(target)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Docker-Ziel wurde während der Migration ersetzt.")
        cleanup = subprocess.run(
            [
                "sudo",
                "find",
                "-P",
                target,
                "-xdev",
                "-depth",
                "-mindepth",
                "1",
                "-delete",
            ],
            capture_output=True,
            text=True,
        )
        if cleanup.returncode != 0:
            raise RuntimeError(
                "Docker-Zielinhalt konnte nicht entfernt werden: "
                + (cleanup.stderr.strip() or str(cleanup.returncode))
            )
        with os.scandir(target) as entries:
            if any(True for _entry in entries):
                raise RuntimeError("Docker-Ziel ist nach dem Rückfall nicht leer.")
    if snapshot["existed"]:
        if not os.path.isdir(target):
            os.mkdir(target, snapshot["mode"])
        ownership = subprocess.run(
            [
                "sudo",
                "chown",
                f"{snapshot['uid']}:{snapshot['gid']}",
                target,
            ],
            capture_output=True,
            text=True,
        )
        mode = subprocess.run(
            ["sudo", "chmod", f"{snapshot['mode']:04o}", target],
            capture_output=True,
            text=True,
        )
        if ownership.returncode != 0 or mode.returncode != 0:
            raise RuntimeError("Metadaten des leeren Docker-Ziels sind nicht restauriert.")
    elif os.path.lexists(target):
        os.rmdir(target)
    if snapshot["existed"] != os.path.isdir(target):
        raise RuntimeError("Docker-Zielzustand stimmt nach dem Rückfall nicht.")


class _DockerMigrationCleanupError(RuntimeError):
    pass


class _DockerTargetTransaction:
    def __init__(self, docker_dir):
        self.snapshot = _capture_empty_docker_target(docker_dir)
        self.docker_dir = self.snapshot["target"]
        self.candidate_may_exist = False
        self.committed = False
        self.host_states = None
        self.stopped_services = []
        self.disabled_services = []

    def __enter__(self):
        os.makedirs(self.docker_dir, exist_ok=True)
        return self

    def bind_host_states(self, host_states):
        self.host_states = host_states

    def commit(self):
        self.committed = True

    def __exit__(self, exc_type, exc, traceback):
        if self.committed:
            return False
        failures = []
        candidate_stopped = True
        if self.candidate_may_exist:
            candidate_stopped, detail = _stop_candidate_container(self.docker_dir)
            if not candidate_stopped:
                failures.append(detail)
        if candidate_stopped and self.host_states is not None:
            failures.extend(
                _rollback_host_service_state(
                    self.host_states,
                    self.stopped_services,
                    self.disabled_services,
                )
            )
        elif not candidate_stopped:
            failures.append(
                "Native Host-Dienste bleiben zum Schutz vor Doppelsteuerung aus."
            )
        if candidate_stopped:
            try:
                _restore_empty_docker_target(self.snapshot)
            except Exception as cleanup_exc:
                failures.append(str(cleanup_exc))
        if failures:
            original = f" Ausgangsfehler: {exc}" if exc is not None else ""
            raise _DockerMigrationCleanupError(
                "Docker-Migrationsrückfall unvollständig: "
                + "; ".join(failures)
                + original
            ) from exc
        return False


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
        return False

    install_user = get_install_user()

    home_dir = get_home_dir(install_user)
    install_path = get_install_path()
    docker_dir = os.path.join(home_dir, "e3dc-docker")
    data_dir = os.path.join(docker_dir, "data")
    logs_dir = os.path.join(docker_dir, "logs")

    try:
        existing_docker_blockers = _existing_docker_migration_blockers(docker_dir)
    except Exception as exc:
        existing_docker_blockers = (
            f"Docker-Bestandsinventar konnte nicht sicher ausgeführt werden: {exc}",
        )
    if existing_docker_blockers:
        print("\n  ✗ Docker-Migration vor der ersten Änderung gestoppt:")
        for blocker in existing_docker_blockers:
            print(f"  - {blocker}")
        print("  Nutze für eine bestehende Docker-Installation den dokumentierten")
        print("  Compose-Updateweg; Menüpunkt 31 ist ausschließlich die Erstmigration.")
        logger.error(
            "Existing Docker state blocked migration: %s",
            "; ".join(existing_docker_blockers),
        )
        return False

    try:
        checked_ha_configs = _verify_docker_ha_config_candidates(
            (
                "/var/www/html/data/e3dc_v4.json",
                os.path.join(install_path, "data", "e3dc_v4.json"),
                os.path.join(data_dir, "e3dc_v4.json"),
            )
        )
    except Exception as exc:
        print(f"\n  ✗ Docker-Migration gestoppt: {exc}")
        logger.error("Docker HA preflight failed: %s", exc)
        return False
    if checked_ha_configs:
        print("  ✓ Vorhandene Konfiguration ist für Docker-Standalone freigegeben.")

    # 1. Docker Installation prüfen / ausführen
    print("\n→ Prüfe Docker-Installation...")
    res = run_command("command -v docker")
    docker_installed_now = False
    if not res['success']:
        pre_engine_blockers = _pre_engine_docker_state_blockers()
        if pre_engine_blockers:
            print("\n  ✗ Docker-Installation vor dem ersten Engine-Start gesperrt:")
            for blocker in pre_engine_blockers:
                print(f"  - {blocker}")
            print("  Bestehenden Docker-Datenroot zuerst administrativ inventarisieren;")
            print("  der Installer löscht oder übernimmt unbekannten Bestand nicht.")
            return False
        print("  Docker nicht gefunden. Installiere Docker aus dem offiziellen APT-Repository...")
        install_res = _install_docker_from_official_apt_repo()
        if install_res.returncode != 0:
            print("\n  ✗ Fehler bei der Docker-Installation! Bitte Internetverbindung prüfen.")
            # Auch ein fehlgeschlagener Paketlauf kann den Docker-Daemon bereits
            # gestartet und dadurch Bestand sichtbar gemacht haben. Der Zustand
            # wird nur gemeldet; unbekannter Bestand wird weder übernommen noch
            # gelöscht.
            try:
                partial_install_blockers = (
                    _post_install_docker_state_blockers(docker_dir)
                    if run_command("command -v docker").get("success")
                    else _pre_engine_docker_state_blockers()
                )
            except Exception as exc:
                partial_install_blockers = (
                    f"Docker-Bestand nach fehlgeschlagener Installation ist nicht sicher inventarisierbar: {exc}",
                )
            for blocker in partial_install_blockers:
                print(f"  - {blocker}")
            return False
        docker_installed_now = True
        print("  ✓ Docker installiert.")
    else:
        print("  ✓ Docker ist bereits installiert.")

    # APT kann den Docker-Dienst bereits während der Paketinstallation starten.
    # Deshalb wird der vollständige Container-/Volume-/Zielbestand unmittelbar
    # danach erneut inventarisiert, bevor ein E3DC-Zielbaum entsteht.
    try:
        post_engine_blockers = (
            _post_install_docker_state_blockers(docker_dir)
            if docker_installed_now
            else _existing_docker_migration_blockers(docker_dir)
        )
    except Exception as exc:
        post_engine_blockers = (
            f"Docker-Bestand nach dem Engine-Start ist nicht sicher inventarisierbar: {exc}",
        )
    if post_engine_blockers:
        print("\n  ✗ Docker-Migration nach dem Engine-Start gesperrt:")
        for blocker in post_engine_blockers:
            print(f"  - {blocker}")
        return False

    # Erst das neu sichtbare Engine-Inventar binden, dann den Nutzerzugang
    # ändern. Ein Fehler hier hinterlässt weder Compose-Baum noch Kandidat.
    if docker_installed_now:
        group_result = run_command(f"sudo usermod -aG docker {install_user}")
        if not group_result.get("success"):
            print("\n  ✗ Docker-Gruppe konnte dem Installationsnutzer nicht zugeordnet werden.")
            logger.error(
                "Docker group assignment failed: %s",
                group_result.get("stderr") or group_result.get("returncode"),
            )
            return False

    try:
        docker_architecture = _docker_server_architecture()
    except Exception as exc:
        print(f"\n  ✗ Docker-Migration gestoppt: {exc}")
        logger.error("Docker architecture preflight failed: %s", exc)
        return False
    print(f"  ✓ Unterstützte Docker-Architektur: {docker_architecture}")

    services_to_stop = _host_services_for_docker_migration()

    with _DockerTargetTransaction(docker_dir) as migration_transaction:
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
    hostname: e3dc-control
    restart: unless-stopped
    network_mode: host # Benötigt für stabile RSCP-Verbindung zum E3DC im LAN
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    labels:
      - com.centurylinklabs.watchtower.enable=${{E3DC_WATCHTOWER_ENABLE:-false}}
    volumes:
      - ./data:/var/www/html/data
      - ./logs:/var/www/html/logs
      - e3dc_ml:/var/lib/e3dc-control/ml
      - e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence
      - e3dc_instance_role:/etc/e3dc-control
    tmpfs:
      - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775
    environment:
      - TZ=Europe/Berlin
      - E3DC_CONTAINER_MODE=1
  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
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
  e3dc_instance_role:
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
            return False
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
            return False

        image_contract = _pulled_image_contract(docker_dir, selected_image)
        print(
            "  ✓ Gezogene Image-Identität gebunden: "
            f"{image_contract['image_id']} / Version {image_contract['version']}"
        )

        wait_contract_ok, wait_contract_error = _compose_wait_contract_available(
            docker_dir
        )
        if not wait_contract_ok:
            print(f"\n✗ Docker-Migration gestoppt: {wait_contract_error}")
            logger.error("Docker Compose wait contract missing: %s", wait_contract_error)
            return False

        health_contract_ok, health_contract_error = _verify_image_healthcheck_contract(
            docker_dir,
            image_contract["image_id"],
        )
        if not health_contract_ok:
            print(f"\n✗ Docker-Migration gestoppt: {health_contract_error}")
            logger.error("Docker image health contract failed: %s", health_contract_error)
            return False
        print("  ✓ Compose-Wartevertrag und imagegebundener Healthcheck bestätigt.")

        # 5. Erst nach erfolgreichem Pull alle Host-Entscheider quieszieren. Die
        # Supervisoren stehen absichtlich am Anfang, damit sie keine Worker erneut
        # starten. Vor compose up müssen zwei Snapshots inaktiv und nicht aktiviert
        # sein; andernfalls wird der Host-Vorzustand rollback-sicher restauriert.
        try:
            host_service_states = _snapshot_host_services(services_to_stop)
            active_host_services = _active_host_services(
                services_to_stop,
                host_service_states,
            )
            enabled_host_services = _enabled_host_services(
                services_to_stop,
                host_service_states,
            )
        except Exception as exc:
            print(f"\n✗ Docker-Migration gestoppt: Host-Dienststatus ist nicht lesbar: {exc}")
            logger.error("Docker host service inventory failed: %s", exc)
            return False
        migration_transaction.bind_host_states(host_service_states)
        print("\n→ Stoppe aktive Host-Dienste für den Containerstart...")
        stop_ok, stopped_services, stop_error = _stop_active_host_services(
            active_host_services
        )
        migration_transaction.stopped_services = list(stopped_services)
        if not stop_ok:
            print(f"\n✗ Docker-Migration gestoppt: {stop_error}")
            logger.error("Docker host service stop error: %s", stop_error)
            return False

        print("  ✓ Zuvor aktive Host-Dienste gestoppt.")
        print("\n→ Deaktiviere native Host-Dienste vor dem Containerstart...")
        disable_ok, disabled_host_services, disable_error = _disable_host_services(
            enabled_host_services,
            host_service_states,
        )
        migration_transaction.disabled_services = list(disabled_host_services)
        if not disable_ok:
            print(f"\n✗ Docker-Migration gestoppt: {disable_error}")
            logger.error("Docker host service disable error: %s", disable_error)
            return False

        quiesced, quiesce_message = _verify_host_services_quiesced(services_to_stop)
        if not quiesced:
            print(f"\n✗ Docker-Migration gestoppt: {quiesce_message}")
            logger.error("Docker host quiescence failed: %s", quiesce_message)
            return False
        print(f"  ✓ {quiesce_message}")

        writers_quiesced, writer_message = _verify_no_host_hardware_writers()
        if not writers_quiesced:
            print(f"\n✗ Docker-Migration gestoppt: {writer_message}")
            print("  Der Installer beendet unmanaged Writer bewusst nicht.")
            logger.error("Docker host writer inventory failed: %s", writer_message)
            return False
        print(f"  ✓ {writer_message}")

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

            update_helper_source = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "docker_compose_update.py",
            )
            update_helper_target = os.path.join(docker_dir, "docker_compose_update.py")
            shutil.copy2(update_helper_source, update_helper_target)
            os.chmod(update_helper_target, 0o755)

            print("\n→ Starte E3DC-Control-Container...")
            image_contract = _require_pre_start_image_contract(
                docker_dir,
                image_contract,
            )
            migration_transaction.candidate_may_exist = True
            up_result = subprocess.run(
                [
                    "sudo",
                    "docker",
                    "compose",
                    "up",
                    "-d",
                    "--pull",
                    "never",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    str(DOCKER_COMPOSE_WAIT_TIMEOUT_SECONDS),
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
                    image_contract,
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

        if res["success"]:
            print("\n" + "="*60)
            print("✓ DOCKER MIGRATION ERFOLGREICH!")
            print("="*60)
            print("Dein System läuft nun vollständig isoliert in Docker.")
            print("Updates werden über den mitkopierten, fail-closed Host-Helfer installiert:")
            print("sudo python3 ./docker_compose_update.py --compose-dir . --sudo")
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
            migration_transaction.commit()
            return True
        else:
            print("\n✗ Fehler beim Starten der Container:")
            print(res["stderr"])
            logger.error("Docker start/identity error: %s", res["stderr"])
            return False

register_command("31", "🐳 Zu Docker wechseln (Auto-Install & Migration)", install_docker_routine, sort_order=31)
