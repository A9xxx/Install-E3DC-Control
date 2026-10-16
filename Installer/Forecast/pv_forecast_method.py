"""Gemeinsamer fachlicher Methodenstand, ohne Erzeugerinitialisierung.

Bei möglichen Änderungen der Prognosewerte einen neuen Methodenstand setzen.
Historische Zulassungen sind ausschließlich LF-Quelltextrevisionen mit fest
gebundenem Wertevergleich; neue Revisionen benötigen einen neuen Wertebeleg.
Bestehende Producer-Contracts werden dabei niemals umgeschrieben.
"""

FORECAST_METHOD_REVISION = 'sha256:934e4b65c5a24c4e47b069782b03e4aa22740a0ccf80140e590e2d6536342bca'
EQUIVALENCE_PROOF_REVISION = 'sha256:9c97e2b938bc605f299e4093ed97183b70c65115b8e11351e3cca65588ccca3f'
# Belegt: c3059c2/fdeb8c0/5d69956/92c839d, 6bf7d65, 0cd4b3e.
# Gegenproben: test_pv287_historical_values und test_pv287_real_control_chain.
VERIFIED_SOURCE_REVISIONS = ('sha256:2100e8987bd1c3d1c181d23ecc23a135db0b99690c35367ee9b4523def13fe75', 'sha256:14c1eb5a11250b6fbe2895cbcb1c0c8ee95582f593127a3ecd5469b3ff5b06fa', 'sha256:52aa0d6d9a4d81d1497e57909fb6287eefe95b3daee953bf6d2330caa6b4bb84')


def evaluation_method_revisions(method_revision):
    """Begrenzte Auswertungszuordnung; unbekannte Methoden bleiben getrennt."""
    family = (FORECAST_METHOD_REVISION, *VERIFIED_SOURCE_REVISIONS)
    return family if method_revision in family else (method_revision,)


def validated_merge(value, topology_revision, method_revision):
    """Prüft den additiven Kontinuitätsbeleg einschließlich Topologiebindung."""
    raw = value if isinstance(value, dict) else {}
    revisions = raw.get("merged_method_revisions", [])
    family = evaluation_method_revisions(method_revision)
    return (raw.get("merged_into_current_metrics") is True
        and len(family) > 1 and isinstance(revisions, list) and bool(revisions)
        and all(isinstance(r, str) for r in revisions)
        and len(revisions) == len(set(revisions))
        and all(isinstance(r, str) and r in family and r != method_revision for r in revisions)
        and raw.get("merged_topology_revision") == topology_revision
        and raw.get("method_family_revision") == FORECAST_METHOD_REVISION
        and raw.get("equivalence_proof_revision") == EQUIVALENCE_PROOF_REVISION)
