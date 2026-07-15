# Web-Push Benachrichtigungen

E3DC-Control kann als PWA native Push-Nachrichten an Browser und Smartphones senden. Das ersetzt Telegram nicht zwingend, ist aber fuer lokale Statusmeldungen und Warnungen sehr praktisch.

## Konfiguration

Die VAPID-Schluessel und Push-Optionen liegen in:

```text
/var/www/html/data/e3dc_v4.json
```

Wichtige Keys:

```ini
push_vapid_public = ...
push_vapid_private = ...
push_enabled = 1
```

Die Schluessel werden automatisch erzeugt. Das Skript `Installer/generate_vapid.py` schreibt neue Keys direkt in `e3dc_v4.json`; ein expliziter Legacy-TXT-Pfad ist nur noch fuer alte Installationen gedacht.

## Ablauf

1. Browser registriert den Service Worker `sw.js`.
2. Browser erzeugt ein Push-Abonnement.
3. `webpush_api.php` speichert das Abonnement in der SQLite-Datenbank.
4. Backend-Dienste senden bei Ereignissen Push-Nachrichten.

## Typische Ereignisse

- Ziel-SoC erreicht
- Fahrzeug nicht angesteckt
- automatischer Lade- oder Speicherstart
- Update verfuegbar
- Notstrom oder Systemwarnung
- Watchdog-/HA-Failover

## Fehlerbehebung

- Browser muss Benachrichtigungen erlauben.
- PWA sollte einmal neu geladen werden, wenn VAPID-Keys geaendert wurden.
- Bei Docker nach Aktivierung einmal den Container neu starten.
- Logs liegen unter `/var/www/html/logs/`.
