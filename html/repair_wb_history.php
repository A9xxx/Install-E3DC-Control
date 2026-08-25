<?php
// E3DC-Control - Wallbox History Repair Tool
// Fixt fehlerhafte C++ Wallbox-Zählerstände in der SQLite Datenbank
// auf Basis der PHP wb_sessions.csv Aufzeichnungen und korrigiert den Hausverbrauch.

require_once __DIR__ . '/helpers.php';
requireWebAuth(false);

$dbPath = '/var/www/html/data/e3dc_stats.db';
$csvPath = '/var/www/html/data/wb_sessions.csv';

echo "<h2>E3DC-Control Wallbox History Repair</h2>";

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    echo "<p>Dieses Reparaturwerkzeug ändert die Langzeit-Datenbank. Bitte starte die Korrektur bewusst per POST.</p>";
    echo "<form method='post'>";
    echo e3dcCsrfInput();
    echo "<input type='hidden' name='confirm_repair_wb_history' value='1'>";
    echo "<button type='submit' style='padding:8px 14px;'>Wallbox-Historie jetzt reparieren</button>";
    echo "</form>";
    echo "<p><a href='index.php?seite=langzeit'>Zurück zur Langzeit-Auswertung -></a></p>";
    exit;
}

e3dcRequireCsrfToken(false);

if (($_POST['confirm_repair_wb_history'] ?? '') !== '1') {
    http_response_code(400);
    die("Fehler: Bestätigung fehlt. Es wurden keine Daten verändert.");
}

if (!file_exists($dbPath)) {
    die("Fehler: Datenbank $dbPath nicht gefunden.");
}

if (!file_exists($csvPath)) {
    die("Fehler: Sessions Log $csvPath nicht gefunden.");
}

$db = new SQLite3($dbPath);
$db->enableExceptions(true);

// 1. CSV einlesen und nach Tagen summieren
$lines = file($csvPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
$dailySums = [];

// Format: Timestamp;Start;End;kWh;WB
foreach ($lines as $idx => $line) {
    if ($idx === 0) continue; // Header
    
    $parts = explode(';', $line);
    if (count($parts) >= 4) {
        $date = substr($parts[1], 0, 10);
        $kwh = (float)$parts[3];
        
        if (!isset($dailySums[$date])) {
            $dailySums[$date] = 0;
        }
        $dailySums[$date] += $kwh;
    }
}

if (empty($dailySums)) {
    echo "<p>Keine gültigen Lade-Sessions in der CSV gefunden.</p>";
    exit;
}

$countUpdates = 0;
$resultRows = [];
$transactionOpen = false;
try {
    if (!$db->exec('BEGIN IMMEDIATE TRANSACTION;')) {
        throw new RuntimeException('Datenbanktransaktion konnte nicht gestartet werden.');
    }
    $transactionOpen = true;
    foreach ($dailySums as $date => $sumKwh) {
        $stmt = $db->prepare("SELECT wb_consumption, home_consumption FROM daily_stats WHERE date = :d");
        $stmt->bindValue(':d', $date, SQLITE3_TEXT);
        $res = $stmt->execute();
        $row = $res->fetchArray(SQLITE3_ASSOC);

        if ($row) {
            $oldWbKwh = (float)$row['wb_consumption'];
            $oldHomeKwh = (float)$row['home_consumption'];
            if (abs($oldWbKwh - $sumKwh) > 0.1) {
                $delta = $sumKwh - $oldWbKwh;
                $newHomeKwh = max(0, $oldHomeKwh - $delta);
                $sumKwhRND = round($sumKwh, 3);
                $newHomeKwhRND = round($newHomeKwh, 3);

                $upd = $db->prepare("UPDATE daily_stats SET wb_consumption = :wb, home_consumption = :home WHERE date = :d");
                $upd->bindValue(':wb', $sumKwhRND, SQLITE3_FLOAT);
                $upd->bindValue(':home', $newHomeKwhRND, SQLITE3_FLOAT);
                $upd->bindValue(':d', $date, SQLITE3_TEXT);
                if ($upd->execute() === false || $db->changes() !== 1) {
                    throw new RuntimeException('Der Datensatz für ' . $date . ' wurde nicht eindeutig aktualisiert.');
                }
                $countUpdates++;
                $resultRows[] = "<li><strong>" . htmlspecialchars($date) . "</strong>: Ersetze WB "
                    . htmlspecialchars((string)$oldWbKwh) . " kWh durch <span style='color:green;'>"
                    . htmlspecialchars((string)$sumKwhRND) . " kWh</span> | Korrigiere Haus "
                    . htmlspecialchars((string)$oldHomeKwh) . " -&gt; <span style='color:blue;'>"
                    . htmlspecialchars((string)$newHomeKwhRND) . " kWh</span></li>";
            } else {
                $resultRows[] = "<li>" . htmlspecialchars($date) . ": " . htmlspecialchars((string)$oldWbKwh) . " kWh ist bereits korrekt.</li>";
            }
        } else {
            $resultRows[] = "<li>" . htmlspecialchars($date) . ": Keine Basis-Daten für diesen Tag gefunden.</li>";
        }
    }
    if (!$db->exec('COMMIT;')) throw new RuntimeException('Datenbanktransaktion konnte nicht bestätigt werden.');
    $transactionOpen = false;
} catch (Throwable $error) {
    if ($transactionOpen) {
        try { $db->exec('ROLLBACK;'); } catch (Throwable $_ignored) {}
    }
    $db->close();
    http_response_code(500);
    echo "<h3 style='color:#dc3545'>Korrektur abgebrochen</h3>";
    echo "<p>Die Datenbanktransaktion wurde nicht bestätigt und zurückgerollt. Bitte Datenbankrechte und freien Speicher prüfen.</p>";
    echo "<p><a href='index.php?seite=langzeit'>Zurück zur Langzeit-Auswertung -&gt;</a></p>";
    exit;
}
$db->close();

echo "<h3>Berechnete echte Tages-Summen aus wb_sessions.csv:</h3><ul>" . implode('', $resultRows) . "</ul>";

// Lösche den Cache für HEUTE aus der Ramdisk, damit das UI die Änderungen direkt zieht!
$statsFile = '/var/www/html/ramdisk/daily_stats.json';
if (file_exists($statsFile)) {
    $cacheRemoval = e3dcRemoveRuntimeCommandFile($statsFile);
    if (empty($cacheRemoval['success'])) {
        http_response_code(500);
        echo "<h3 style='color:#d97706'>Daten korrigiert, Live-Cache nicht entfernt</h3>";
        echo "<p>Die Datenbankänderung ist gespeichert. Bitte führe im Installationscenter „Rechte reparieren“ aus und lade die Langzeitansicht danach neu.</p>";
        echo "<p><a href='index.php?seite=langzeit'>Zurück zur Langzeit-Auswertung -&gt;</a></p>";
        exit;
    }
    echo "<p><span style='color:orange;'>Info: Live-Cache für heute gelöscht (UI lädt nun aktuelle Werte).</span></p>";
}

echo "<h3><span style='color:green'>Erfolgreich! $countUpdates Tage in der Langzeit-Datenbank korrigiert.</span></h3>";
echo "<p><a href='index.php?seite=langzeit'>Zurück zur Langzeit-Auswertung -></a></p>";
?>
