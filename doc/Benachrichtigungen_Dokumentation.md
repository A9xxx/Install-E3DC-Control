# Dokumentation: Benachrichtigungs-Dienst (Notifier)

Der E3DC-Control Benachrichtigungs-Dienst (`e3dc-notifier.service`) ist ein
schlanker Python-Hintergrundprozess, der zeitbasierte Ereignisse und
Langzeitaufgaben des Systems koordiniert.

## 1. Warum ein eigener Dienst?

Der **Notification Manager** liest die Einstellungen zentral aus
`e3dc_v4.json` und führt zeitgesteuerte Aufgaben nach Vorgabe des
Web-Interfaces aus. Dadurch nutzen Benachrichtigungen, Backups und
Langzeitaufgaben dieselbe Konfiguration.

## 2. Welche Aufgaben übernimmt der Dienst?

Der Daemon läuft in einer Endlosschleife und prüft regelmäßig:

1. **Boot-Benachrichtigung:** Einmalig beim Systemstart wird gemeldet, dass der
   Pi online ist.
2. **Täglicher Statusbericht:** Meldet zur konfigurierten Uhrzeit Laufzeit und
   CPU-Temperatur.
3. **Tägliche Statistik:** Sendet jeden Morgen eine Aufschlüsselung der
   Energieverteilung des Vortages inklusive kompakter Raspberry-Pi-Werte.
4. **Wöchentliche Statistik:** Sendet die aggregierten Statistiken der letzten
   sieben Tage.
5. **History-Backup:** Sichert täglich die E3DC-Livedaten für spätere
   Rückblicke.
6. **Datenbank-Archiv:** Schreibt die aggregierten Tageswerte
   ressourcenschonend in `e3dc_stats.db`.
7. **Live-Historie:** Speichert minütlich den aktuellen Anlagenstatus für die
   flüssige Darstellung der Diagramme.

## 3. Einrichtung und Konfiguration

`e3dc-notifier` ist ein Kerndienst aus dem Service-Katalog und wird bei
Installation, Update und Rechte-Reparatur mitgeprüft. Auf Altinstallationen
reicht normalerweise:

```bash
cd ~/Install
sudo python3 installer_main.py --fix-permissions
sudo python3 installer_main.py --update-e3dc
```

Die Einrichtung erfolgt danach im **Config Editor**:

1. Öffne die Kategorie **Benachrichtigungen (Telegram)**.
2. Trage `telegram_token` und `telegram_chat_id` ein.
3. Vergib einen Namen für dein System (`telegram_device_name`), z.B. "Mein Haus".
4. Aktiviere die gewünschten Berichte und stelle die Uhrzeiten ein.
5. Klicke auf **Alle Änderungen speichern**.

Der Dienst erkennt neue Einstellungen beim nächsten Durchlauf automatisch.

Tipp: Wenn du das Senden von Nachrichten temporär komplett stoppen möchtest,
kannst du den Bot-Token aus der Konfiguration löschen.
