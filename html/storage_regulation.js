/* Shared desktop/mobile status for the storage-only switch. */
function storageRegulationStatus(state, enabled, requestTs, nowS) {
    const observed = state && state.storage_regulation;
    if (!observed || state.state === 'stopped' || !Number.isFinite(Number(state.ts)) || nowS - Number(state.ts) > 15 || Number(state.ts) > nowS + 5) {
        return {text: 'Keine aktuelle Regler-Rückmeldung. Der gespeicherte Schalter allein bestätigt keine Limitfreigabe.', warning: false};
    }
    if (observed.requested_enabled === null) {
        return {text: 'Konfiguration nicht lesbar – Speicherbefehle sind gesperrt. Eine Limitfreigabe ist nicht bestätigt.', warning: true};
    }
    if (Number(observed.request_ts || 0) !== Number(requestTs) || observed.requested_enabled !== enabled) {
        return {text: 'Einstellung gespeichert – warte auf Übernahme durch den Speicherregler …', warning: false};
    }
    if (enabled) {
        return {text: 'Speicherregelung eingeschaltet. Die normalen Freigaben und Schutzgrenzen gelten.', warning: false};
    }
    if (observed.release_status === 'confirmed' && observed.release_confirmed === true) {
        return {text: 'Speicherregelung aus – Limitfreigabe bestätigt. Keine weiteren Speicherbefehle. Eine vorherige Leistungsvorgabe kann noch bis zum geräteseitigen Timeout nachwirken.', warning: false};
    }
    if (observed.release_status === 'pending') {
        return {text: 'Speicherregelung wird ausgeschaltet – Limitfreigabe noch nicht bestätigt.', warning: false};
    }
    if (observed.release_status === 'not_released_on_start') {
        return {text: 'Speicherregelung aus gestartet – keine Speicherbefehle. Eine frühere Limitfreigabe ist nicht bestätigt; vorhandene Gerätevorgaben bleiben unangetastet.', warning: false};
    }
    return {text: 'Speicherregelung aus – Limitfreigabe nicht bestätigt. Es werden keine weiteren Speicherbefehle gesendet. Bestehende Vorgaben am E3DC prüfen, bevor ein externer Regler übernimmt.', warning: true};
}

(function () {
    const start = () => {
        const control = document.getElementById('storageRegulationControl');
        const toggle = document.getElementById('storageRegulationEnabled');
        const status = document.getElementById('storageRegulationState');
        if (!control || !toggle || !status) return;
        const poll = async () => {
            if (!control.isConnected) return;
            if (!document.hidden && !toggle.disabled) {
                try {
                    const response = await fetch('ramdisk/storage_manager_state.json?_=' + Date.now(), {cache: 'no-store', signal: AbortSignal.timeout(4000)});
                    const state = response.ok ? await response.json() : null;
                    if (!toggle.disabled) {
                        const result = storageRegulationStatus(state, toggle.checked, Number(control.dataset.requestTs || 0), Date.now() / 1000);
                        status.textContent = result.text;
                        status.className = 'small mt-2 ' + (result.warning ? 'text-warning' : 'text-body-secondary');
                    }
                } catch (_) {
                    if (!toggle.disabled) {
                        status.textContent = 'Reglerstatus nicht erreichbar. Der gespeicherte Schalter allein bestätigt keine Limitfreigabe.';
                        status.className = 'small text-body-secondary mt-2';
                    }
                }
            }
            if (control.isConnected) setTimeout(poll, 3000);
        };
        poll();
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
})();
