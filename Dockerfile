FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df

ARG E3DC_VERSION=unknown
ARG E3DC_REVISION=unknown
ARG E3DC_TREE=unknown
ARG E3DC_CREATED=unknown
ARG E3DC_SOURCE_MANIFEST=unknown

LABEL org.opencontainers.image.title="E3DC-Control" \
      org.opencontainers.image.description="Lokales Energie- und Installationssystem für E3DC-Anlagen" \
      org.opencontainers.image.source="https://github.com/A9xxx/Install-E3DC-Control" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${E3DC_VERSION}" \
      org.opencontainers.image.revision="${E3DC_REVISION}" \
      org.opencontainers.image.created="${E3DC_CREATED}" \
      io.e3dc.git.tree="${E3DC_TREE}" \
      io.e3dc.source.manifest="${E3DC_SOURCE_MANIFEST}"

# 1. Systempakete (Laufzeitumgebung - ändert sich selten)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq util-linux logrotate \
    apache2 php libapache2-mod-php php-curl php-sqlite3 php-mbstring \
    python3 python3-pip python3-venv python3-dev unzip python3-sklearn python3-numpy \
    ca-certificates pkg-config libffi-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Node.js, D-Bus und Avahi für die lokale Matter Bridge
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm avahi-daemon avahi-utils dbus && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. Apache PHP + Reverse Proxy für WebSockets
COPY --chown=root:root --chmod=0644 Installer/apache/e3dc-control-security.conf /etc/apache2/conf-available/e3dc-control-security.conf
COPY --chown=root:root --chmod=0644 Installer/apache/e3dc-control-access-log.conf /etc/apache2/conf-available/e3dc-control-access-log.conf
RUN PHP_APACHE_MOD="$(php -r 'echo "php".PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')" && \
    (a2dismod mpm_event >/dev/null 2>&1 || true) && \
    (a2dismod mpm_worker >/dev/null 2>&1 || true) && \
    a2enmod mpm_prefork "$PHP_APACHE_MOD" proxy proxy_wstunnel && \
    a2enconf e3dc-control-security && \
    echo "ServerName localhost" >> /etc/apache2/apache2.conf && \
    sed -i 's|^[[:space:]]*CustomLog ${APACHE_LOG_DIR}/access.log combined$|    # E3DC-Control: erfolgreiche Live-POSTs nicht persistent protokollieren\n    IncludeOptional /etc/apache2/conf-available/e3dc-control-access-log.conf|' /etc/apache2/sites-available/000-default.conf && \
    sed -i 's|</VirtualHost>|    ProxyPass "/ws" "ws://127.0.0.1:8765/"\n</VirtualHost>|' /etc/apache2/sites-available/000-default.conf && \
    test "$(grep -Fc 'IncludeOptional /etc/apache2/conf-available/e3dc-control-access-log.conf' /etc/apache2/sites-available/000-default.conf)" = "1" && \
    test "$(grep -Fc 'CustomLog ${APACHE_LOG_DIR}/access.log combined' /etc/apache2/sites-available/000-default.conf)" = "0" && \
    apache2ctl configtest

# 3. Python VENV mit allen Abhängigkeiten
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV E3DC_CONTAINER_MODE=1
ENV E3DC_CONTAINER_INSTALL_USER=root
RUN pip3 install --upgrade pip wheel setuptools && \
    pip3 install --prefer-binary paho-mqtt requests websocket-client websockets luxtronik hyundai_kia_connect_api pywebpush pycryptodome pymodbus

# 4. Verzeichnisse und statische Konfiguration
RUN mkdir -p /app/pi/Install /var/www/html/tmp /var/www/html/logs /var/www/html/data /var/www/html/ramdisk && \
    install -d -o root -g root -m 0755 /etc/e3dc-control && \
    install -d -o root -g root -m 0700 /var/lib/e3dc-control/forecast-evidence && \
    chown -R www-data:www-data /var/www/html && \
    chmod 2775 /var/www/html/data

# Pfad-Konfiguration für PHP (statisch, kennt die Container-Struktur)
RUN echo '{"install_user": "root", "home_dir": "/app", "install_path": "/app/pi/Install", "venv_name": null, "venv_path": "/opt/venv"}' > /var/www/html/e3dc_paths.json && \
    chown www-data:www-data /var/www/html/e3dc_paths.json

# 5. Entrypoint-Bootloader
# Der gebackene Entrypoint delegiert beim Start an /app/pi/Install/entrypoint.sh,
# sobald dort Projektcode liegt. Mit Dev-Volume gewinnt der Host-Code, sonst
# gewinnt der im Image enthaltene Release-Code.
COPY --chown=root:root --chmod=0555 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=root:root --chmod=0555 Installer/docker_healthcheck.py /usr/local/bin/e3dc-docker-healthcheck
COPY --chown=root:root --chmod=0555 Installer/docker_logrotate_manager.py /usr/local/bin/e3dc-docker-logrotate
COPY --chown=root:root --chmod=0555 Installer/docker_matter_storage_guard.py /usr/local/bin/e3dc-docker-matter-storage-guard
COPY --chown=root:root --chmod=0644 Installer/docker-logrotate.conf /etc/logrotate.d/e3dc-control
RUN test "$(stat -c '%u:%g:%a' /usr/local/bin/entrypoint.sh)" = "0:0:555" && \
    test "$(stat -c '%u:%g:%a' /usr/local/bin/e3dc-docker-matter-storage-guard)" = "0:0:555" && \
    ln -sf /usr/local/bin/entrypoint.sh /app/entrypoint.sh

# 6. Anwendungscode für Production-/Image-only-Docker.
# Entwicklungsumgebungen dürfen /app/pi/Install weiterhin per Volume überlagern.
COPY --chown=root:root . /app/pi/Install/
RUN find -P /app/pi/Install -xdev -type d -exec chmod 0755 -- {} + && \
    find -P /app/pi/Install -xdev -type f -exec chmod 0644 -- {} + && \
    chmod 0755 \
        /app/pi/Install/entrypoint.sh \
        /app/pi/Install/e3dc-bootstrap \
        /app/pi/Install/e3dc-setup \
        /app/pi/Install/installer_main.py \
        /app/pi/Install/Installer/installer_wrapper.sh \
        /app/pi/Install/Installer/service_wrapper.sh \
        /app/pi/Install/Installer/web_update_launcher.sh && \
    find -P /app/pi/Install/Installer -xdev -type f -name "*.py" -exec chmod 0755 -- {} + && \
    test -f /app/pi/Install/Installer/matter/package-lock.json && \
    test -f /app/pi/Install/Installer/apache/e3dc-control-security.conf && \
    test -f /app/pi/Install/Installer/apache/e3dc-control-access-log.conf && \
    test -f /app/pi/Install/Installer/Storage/process_singleton.py && \
    test -f /app/pi/Install/Installer/Wallbox/process_singleton.py && \
    test -f /app/pi/Install/Installer/Wallbox/start_hold.py && \
    test -f /app/pi/Install/Installer/control_time.py && \
    test -f /app/pi/Install/Installer/direct_marketing_actions.py && \
    test -f /app/pi/Install/Installer/docker_healthcheck.py && \
    test -f /app/pi/Install/Installer/ha_writer_admission.py && \
    test -f /app/pi/Install/Installer/secure_file_transaction.py && \
    test -f /app/pi/Install/Installer/storage_owner_paths.py && \
    test -f /app/pi/Install/Installer/docker_compose_update.py && \
    test -f /app/pi/Install/Installer/docker_logrotate_manager.py && \
    test -f /app/pi/Install/Installer/docker_matter_storage_guard.py && \
    test -f /app/pi/Install/Installer/docker-logrotate.conf && \
    chown root:root /app/pi/Install /app/pi/Install/Installer /app/pi/Install/Installer/apache /app/pi/Install/Installer/apache/e3dc-control-security.conf /app/pi/Install/Installer/apache/e3dc-control-access-log.conf && \
    chmod 0755 /app/pi/Install /app/pi/Install/Installer /app/pi/Install/Installer/apache && \
    chmod 0644 /app/pi/Install/Installer/apache/e3dc-control-security.conf /app/pi/Install/Installer/apache/e3dc-control-access-log.conf && \
    test "$(stat -c '%u:%g:%a' /app/pi/Install)" = "0:0:755" && \
    test "$(stat -c '%u:%g:%a' /app/pi/Install/Installer)" = "0:0:755" && \
    test "$(stat -c '%u:%g:%a' /app/pi/Install/Installer/apache)" = "0:0:755" && \
    test "$(stat -c '%u:%g:%a' /app/pi/Install/Installer/apache/e3dc-control-security.conf)" = "0:0:644" && \
    test "$(stat -c '%u:%g:%a' /app/pi/Install/Installer/apache/e3dc-control-access-log.conf)" = "0:0:644" && \
    cd /app/pi/Install/Installer/matter && \
    npm ci --omit=dev --ignore-scripts && \
    chown -R root:root /app/pi/Install/Installer/matter/node_modules && \
    test -z "$(find /app/pi/Install/Installer/matter/node_modules \( ! -uid 0 -o ! -gid 0 \) -print -quit)" && \
    find -P /app/pi/Install/Installer/matter/node_modules -xdev \( -type f -o -type d \) -exec chmod go-w -- {} + && \
    test -x /app/pi/Install/entrypoint.sh && \
    test -x /app/pi/Install/e3dc-bootstrap && \
    test -x /app/pi/Install/e3dc-setup && \
    test -x /app/pi/Install/installer_main.py && \
    test -x /app/pi/Install/Installer/installer_wrapper.sh && \
    test -x /app/pi/Install/Installer/service_wrapper.sh && \
    test -x /app/pi/Install/Installer/web_update_launcher.sh && \
    test -z "$(find -P /app/pi/Install -xdev \( ! -uid 0 -o ! -gid 0 \) -print -quit)" && \
    test -z "$(find -P /app/pi/Install -xdev \( -type f -o -type d \) -perm /0022 -print -quit)"

WORKDIR /app/pi/Install

HEALTHCHECK --interval=5s --timeout=5s --start-period=120s --retries=36 \
    CMD ["/opt/venv/bin/python3", "-I", "-B", "/usr/local/bin/e3dc-docker-healthcheck"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
