<?php
// fahrzeug.php - Eigene Dashboard-Unterseite für Fahrzeug-Informationen
?>
<div class="card dashboard-card shadow-sm mb-4" style="border-radius: 16px;">
    <div class="card-body p-4">
        <div class="d-flex justify-content-between align-items-center border-bottom border-secondary-subtle pb-3 mb-4">
            <h5 class="fw-bold m-0" style="color: var(--text-body, inherit);">
                <i class="fas fa-car-side text-success me-2"></i>Fahrzeug Details
            </h5>
            <button class="btn btn-outline-success btn-sm rounded-pill fw-bold" onclick="forceSocUpdate()" id="fz-update-btn" style="display:none;">
                <i class="fas fa-sync-alt me-1"></i> Fahrzeug aufwecken
            </button>
        </div>

        <ul class="nav nav-pills mb-4" id="vehicleTabs" role="tablist">
            <!-- Tabs werden von JavaScript injiziert -->
        </ul>
        <div class="tab-content" id="vehicleTabsContent">
            <div class="text-center text-muted py-5" id="fz-loading">
                <i class="fas fa-spinner fa-spin fa-2x mb-3"></i><br>Lade Fahrzeugdaten...
            </div>
        </div>
        
        <div class="alert alert-secondary mt-4 border-secondary-subtle text-center small mb-0">
            <i class="fas fa-info-circle me-1"></i> Das System erkennt anhand der in der Konfiguration hinterlegten GPS-Koordinaten automatisch, ob das Fahrzeug sich in der "Homezone" (Zuhause) befindet.
        </div>
    </div>
</div>