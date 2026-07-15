<?php
/* =====================================================================
   history.php - Historie mit Zeitauswahl
   ===================================================================== */
$paths = getInstallPaths();
$backups = getHistoryBackupFiles();

// Die Aktualisierung erfolgt auf Anforderung über den Update-Button.
?>

<div id="historyHeader" class="d-flex justify-content-between align-items-center mb-3 px-1">
    <h5 class="m-0 fw-bold text-info">Live-Verlauf</h5>
    
    <div class="btn-group btn-group-sm mx-2" role="group" id="timeFilterGroup">
        <button type="button" class="btn btn-outline-info active" data-hours="6">6h</button>
        <button type="button" class="btn btn-outline-info" data-hours="12">12h</button>
        <button type="button" class="btn btn-outline-info" data-hours="24">24h</button>
        <button type="button" class="btn btn-outline-info" data-hours="48">48h</button>
    </div>

    <div>
        <button class="btn btn-sm btn-outline-secondary btn-chart-flip me-1" onclick="toggleChartFlip()" title="Werte klappen (Absolutwerte anzeigen)">
            <i class="fas fa-arrows-alt-v"></i>
        </button>
        <span id="historyUpdateStatus" class="me-2 text-body-secondary small"></span>
        <button id="historyUpdateBtn" class="btn btn-sm btn-outline-secondary">
            <i class="bi bi-arrow-clockwise"></i> Update
        </button>
    </div>
</div>

<!-- Ansicht Auswahl -->
<div class="mb-3 px-1">
    <div class="input-group input-group-sm">
        <label class="input-group-text bg-body-secondary text-info border-info" for="historyViewSelect">Ansicht</label>
        <select class="form-select bg-body-secondary text-body border-info" id="historyViewSelect">
            <option value="normal" selected>Standard (Leistung)</option>
            <option value="pv">PV Details (Strings)</option>
            <option value="bat">Batterie Details</option>
            <option value="grid">Netz Details (Phasen)</option>
            <option value="price">Strompreis & Kosten</option>
            <?php if ($wbEnabled): ?>
            <option value="wb">Wallbox Details</option>
            <?php endif; ?>
            <?php if ($luxtronikEnabled): ?>
            <option value="wp">Wärmepumpe Details</option>
            <?php endif; ?>
        </select>
    </div>
</div>

<!-- Archiv Dropdown -->
<div class="mb-3 px-1">
    <div class="input-group input-group-sm">
        <label class="input-group-text bg-body-secondary text-info border-info" for="historyArchiveSelect">Archiv (24h)</label>
        <select class="form-select bg-body-secondary text-body border-info" id="historyArchiveSelect">
            <option value="live" selected>Aktuelle Live-Daten</option>
            <?php foreach ($backups as $b): ?>
                <option value="<?= htmlspecialchars($b['file']) ?>"><?= htmlspecialchars($b['label']) ?></option>
            <?php endforeach; ?>
        </select>
    </div>
</div>

<div class="ratio ratio-1x1 w-100 position-relative" style="min-height: 400px; max-height: 70vh; border-radius: 8px; overflow: hidden; border: 1px solid var(--border-card);">
    <!-- Live JS Chart Overlay -->
    <div id="liveChartContainer" class="w-100 h-100 position-absolute top-0 start-0 p-2" style="background-color: var(--bg-card); z-index: 10;">
        <canvas id="liveChartCanvas"></canvas>
    </div>
</div>

<script>
(function(){
    var btn = document.getElementById('historyUpdateBtn');
    var archiveSelect = document.getElementById('historyArchiveSelect');
    var viewSelect = document.getElementById('historyViewSelect');
    var status = document.getElementById('historyUpdateStatus');
    var filterGroup = document.getElementById('timeFilterGroup');
    var timeBtns = document.querySelectorAll('#timeFilterGroup .btn');
    var currentHours = 6; // Standardwert
    var currentFile = '';
    var currentView = 'normal';
    window.triggerHistoryUpdate = triggerUpdate;

    // Button Click-Logik für Zeitauswahl
    timeBtns.forEach(function(tBtn) {
        tBtn.addEventListener('click', function() {
            // Aktiven Status umschalten
            timeBtns.forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            
            // Stunden setzen und sofort Update triggern
            currentHours = this.getAttribute('data-hours');
            currentFile = '';
            archiveSelect.value = 'live';
            triggerUpdate();
        });
    });

    // Dropdown Logik für Archiv-Dateien
    archiveSelect.addEventListener('change', function() {
        if (this.value === 'live') {
            // Zurück zu Live-Daten
            currentFile = '';
            filterGroup.classList.remove('d-none');
            // Aktuelle Stunden vom aktiven Button holen
            var activeBtn = document.querySelector('#timeFilterGroup .btn.active');
            currentHours = activeBtn ? activeBtn.getAttribute('data-hours') : 6;
            triggerUpdate();
        } else if (this.value) {
            // Archiv-Datei gewählt
            currentFile = this.value;
            currentHours = 24;
            filterGroup.classList.add('d-none');
            triggerUpdate();
        }
    });

    // Dropdown Logik für Ansicht
    viewSelect.addEventListener('change', function() {
        currentView = this.value;
        if (typeof CURRENT_VIEW !== 'undefined') CURRENT_VIEW = this.value;
        triggerUpdate();
    });

    btn.addEventListener('click', triggerUpdate);

    function triggerUpdate(){
        btn.disabled = true;
        archiveSelect.disabled = true;
        viewSelect.disabled = true;
        timeBtns.forEach(b => b.disabled = true); // Buttons sperren während geladen wird
        
        var msg = currentFile ? 'Lade Archiv ' + archiveSelect.options[archiveSelect.selectedIndex].text + '...' : 'Erstelle ' + currentHours + 'h Diagramm…';
        if (status) status.textContent = msg;
        
        var jsContainer = document.getElementById('liveChartContainer');
        if (jsContainer) {
            jsContainer.style.display = 'block';
            if (currentView === 'price') {
                if (typeof loadJsPriceChart === 'function') {
                    if (currentFile) loadJsPriceChart(24, currentFile);
                    else loadJsPriceChart(currentHours);
                }
            } else if (typeof loadJsLiveChart === 'function') {
                if (currentFile) loadJsLiveChart(24, currentFile);
                else loadJsLiveChart(currentHours);
            }
            if (status) status.textContent = 'Aktualisiert';
            resetBtns();
        }
    }

    function resetBtns() {
        btn.disabled = false;
        archiveSelect.disabled = false;
        viewSelect.disabled = false;
        timeBtns.forEach(b => b.disabled = false);
    }

    // Auto-load beim Öffnen der Seite
    document.addEventListener('DOMContentLoaded', function() {
        if (typeof CURRENT_VIEW !== 'undefined') CURRENT_VIEW = currentView;
        triggerUpdate();
    });
})();
</script>
