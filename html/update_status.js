// Gemeinsame Anzeige und rein lesende Wiederaufnahme von Updateaufträgen.
function e3dcUpdateObservationKey(scope) {
    const directory = window.location.pathname.replace(/[^/]*$/, '');
    return 'e3dc-update-observation:' + directory + ':' + scope;
}

function e3dcUpdateRunId(value) {
    return typeof value === 'string'
        && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)
        ? value : null;
}

function e3dcReadUpdateObservation(scope, maxDurationMs) {
    try {
        const key = e3dcUpdateObservationKey(scope);
        const saved = JSON.parse(sessionStorage.getItem(key));
        if (!saved) return null;
        const duration = Math.min(maxDurationMs, saved.maxDurationMs);
        const pending = saved.binding;
        const pendingValid = !saved.runId && pending && typeof pending.baselineAvailable === 'boolean'
            && typeof pending.baselineWasRunning === 'boolean'
            && (pending.baselineRunId === null || e3dcUpdateRunId(pending.baselineRunId));
        if ((!e3dcUpdateRunId(saved.runId) && !pendingValid) || !Number.isFinite(saved.startedAt)
            || !Number.isFinite(duration) || duration <= 0
            || saved.startedAt > Date.now() || Date.now() - saved.startedAt >= duration) {
            sessionStorage.removeItem(key);
            return null;
        }
        return {
            binding: saved.runId ? {expectedRunId: saved.runId, boundRunId: saved.runId} : {
                baselineAvailable: pending.baselineAvailable,
                baselineWasRunning: pending.baselineWasRunning,
                baselineRunId: pending.baselineRunId,
                expectedRunId: null, boundRunId: null,
            },
            startedAt: saved.startedAt,
            maxDurationMs: duration,
        };
    } catch (_error) {
        return null;
    }
}

function e3dcValidateUpdatePoll(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)
        || typeof data.running !== 'boolean' || typeof data.log !== 'string'
        || data.snapshot_consistent === false
        || (data.exit_code != null && !Number.isInteger(data.exit_code))) {
        const error = new Error('Keine gültige, zusammenhängende Statusantwort');
        error.code = 'invalid_response';
        throw error;
    }
}

function e3dcUpdateObservation(scope, binding, startedAt, maxDurationMs) {
    const state = {
        binding, startedAt, errorSince: null, nextPollAt: 0,
        expired() { return Date.now() - startedAt >= maxDurationMs; },
        ready() { return Date.now() >= this.nextPollAt; },
        save() {
            const runId = e3dcUpdateRunId(binding.boundRunId || binding.expectedRunId);
            try {
                // Vor der Startantwort nur die vorhandene Zuordnung erhalten, keine Formdaten oder Tokens.
                const pending = runId ? undefined : {
                    baselineAvailable: binding.baselineAvailable === true,
                    baselineWasRunning: binding.baselineWasRunning === true,
                    baselineRunId: e3dcUpdateRunId(binding.baselineRunId),
                };
                sessionStorage.setItem(e3dcUpdateObservationKey(scope), JSON.stringify({runId, binding: pending, startedAt, maxDurationMs}));
            } catch (_error) { /* Die laufende Beobachtung benötigt keinen Browserspeicher. */ }
        },
        clear() {
            try {
                const key = e3dcUpdateObservationKey(scope);
                const saved = JSON.parse(sessionStorage.getItem(key));
                if (saved && saved.startedAt === startedAt
                    && saved.runId === e3dcUpdateRunId(binding.boundRunId || binding.expectedRunId)) {
                    sessionStorage.removeItem(key);
                }
            } catch (_error) { /* Browserspeicher kann gesperrt sein. */ }
        },
        accept() {
            this.errorSince = null;
            this.nextPollAt = 0;
            this.save();
        },
        fail(error) {
            if (this.errorSince === null) this.errorSince = Date.now();
            const long = Date.now() - this.errorSince >= 120000;
            const status = Number(error && error.status);
            const invalid = error && (error.code === 'invalid_response' || error.kind === 'invalid_response' || error.name === 'SyntaxError');
            const auth = status === 401 || status === 403;
            const temporary = !status || [408, 429, 500, 502, 503, 504].includes(status);
            const slow = long || auth || invalid || !temporary;
            this.nextPollAt = Date.now() + (slow ? 15000 : 1000);
            let title = long ? 'Verbindung seit über zwei Minuten unterbrochen' : 'Verbindung vorübergehend unterbrochen';
            let detail = 'Der aktuelle Auftragszustand ist nicht bestätigt.';
            if (auth) {
                title = 'Statuszugriff nicht erlaubt (HTTP ' + status + ')';
                detail = status === 401 ? 'Bitte melde Dich erneut an. Der Auftragszustand ist unbekannt.'
                    : 'Bitte prüfe Deine Anmeldung und Zugriffsberechtigung. Der Auftragszustand ist unbekannt.';
            } else if (invalid) {
                title = 'Statusantwort nicht verwertbar';
                detail = 'Es liegt keine gültige, zusammenhängende Statusantwort vor.';
            } else if (!temporary) {
                title = 'Statusabfrage fehlgeschlagen (HTTP ' + status + ')';
            }
            detail += slow
                ? ' Der Status wird alle 15 Sekunden automatisch erneut abgefragt.'
                : ' Der Status wird automatisch erneut abgefragt.';
            return {title, detail, long};
        },
    };
    state.save();
    return state;
}

function e3dcUpdatePhase(logText) {
    const log = typeof logText === 'string' ? logText : '';
    let phase = {title: 'Auftrag wird vorbereitet', detail: 'Warte auf einen bestätigten Arbeitsschritt.', step: 'Start', progress: 5, warning: false};
    if (log.includes('[1/4]')) phase = {title: 'Sicherung erstellen – Anlage läuft weiter', detail: 'Die Sicherung wird erstellt und geprüft.', step: 'Schritt 1 von 4', progress: 22, warning: false};
    if (log.includes('[2/4]')) phase = {title: 'Dateien und Rechte aktualisieren – kurze Anlagenunterbrechung', detail: 'Die Dienste werden für die abschließende Sicherung und den Dateiaustausch angehalten.', step: 'Schritt 2 von 4', progress: 48, warning: true};
    if (log.includes('[3/4]')) phase = {title: 'Dateien und Rechte aktualisieren – kurze Anlagenunterbrechung', detail: 'Produktdateien und Rechte werden aktualisiert.', step: 'Schritt 3 von 4', progress: 70, warning: true};
    if (log.includes('[4/4]')) phase = {title: 'Dienste starten und prüfen', detail: 'Der Wiederanlauf wird geprüft.', step: 'Schritt 4 von 4', progress: 88, warning: false};
    if (log.includes('[STATUS] Regelung und Weboberfläche laufen wieder.')) phase = {title: 'Anlage läuft wieder – Abschlussbereinigung', detail: 'Die Abschlussbereinigung und das Backup-Limit werden geprüft.', step: 'Abschlussprüfung', progress: 96, warning: false};
    return phase;
}
