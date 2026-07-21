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
    git curl jq util-linux \
    apache2 php libapache2-mod-php php-curl php-sqlite3 php-mbstring \
    python3 python3-pip python3-venv python3-dev unzip python3-sklearn python3-numpy \
    ca-certificates pkg-config libffi-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Node.js, D-Bus und Avahi für die lokale Matter Bridge
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm avahi-daemon avahi-utils dbus && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. Apache PHP + Reverse Proxy für WebSockets
RUN PHP_APACHE_MOD="$(php -r 'echo "php".PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;')" && \
    (a2dismod mpm_event >/dev/null 2>&1 || true) && \
    (a2dismod mpm_worker >/dev/null 2>&1 || true) && \
    a2enmod mpm_prefork "$PHP_APACHE_MOD" proxy proxy_wstunnel && \
    echo "ServerName localhost" >> /etc/apache2/apache2.conf && \
    sed -i 's|</VirtualHost>|    ProxyPass "/ws" "ws://127.0.0.1:8765/"\n</VirtualHost>|' /etc/apache2/sites-available/000-default.conf

# 3. Python VENV mit allen Abhängigkeiten
RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip3 install --upgrade pip wheel setuptools && \
    pip3 install --prefer-binary paho-mqtt requests websocket-client websockets luxtronik hyundai_kia_connect_api pywebpush pycryptodome pymodbus

# 4. Verzeichnisse und statische Konfiguration
RUN mkdir -p /app/pi/Install /var/www/html/tmp /var/www/html/logs /var/www/html/data /var/www/html/ramdisk && \
    chown -R www-data:www-data /var/www/html

# Pfad-Konfiguration für PHP (statisch, kennt die Container-Struktur)
RUN echo '{"install_user": "root", "home_dir": "/app", "install_path": "/app/pi/Install", "venv_name": null, "venv_path": "/opt/venv"}' > /var/www/html/e3dc_paths.json && \
    chown www-data:www-data /var/www/html/e3dc_paths.json

# 5. Entrypoint-Bootloader
# Der gebackene Entrypoint delegiert beim Start an /app/pi/Install/entrypoint.sh,
# sobald dort Projektcode liegt. Mit Dev-Volume gewinnt der Host-Code, sonst
# gewinnt der im Image enthaltene Release-Code.
COPY entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    ln -sf /usr/local/bin/entrypoint.sh /app/entrypoint.sh

# 6. Anwendungscode für Production-/Image-only-Docker.
# Entwicklungsumgebungen dürfen /app/pi/Install weiterhin per Volume überlagern.
COPY . /app/pi/Install/
RUN test -f /app/pi/Install/Installer/matter/package-lock.json && \
    cd /app/pi/Install/Installer/matter && \
    npm ci --omit=dev --ignore-scripts && \
    chmod +x /app/pi/Install/entrypoint.sh && \
    find /app/pi/Install/Installer -name "*.py" -exec chmod 755 {} \;

WORKDIR /app/pi/Install

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
