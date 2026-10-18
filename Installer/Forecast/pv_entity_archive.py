"""Einzelprognosen und DC-Tracker ausschließlich für die lokale Diagnose.

Rohstufe: dieselben Stundenwerte wie der Anbieterpfad vor Gruppensummierung.
Beitrag: 250 * ((1-a)*p0*w0 + a*p1*w1) Wh; keine Bias-/Cap-Projektion.
Keine Gerätezugriffe, keine Rückgabe von Regelwerten, keine Flächenaufteilung.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone

SCHEMA = 'pv_entity_archive_v1'
MAX_ENTITIES = 16
MAX_SLOTS = 480
MAX_ISSUE_BYTES = 2 * 1024 * 1024
RETENTION_DAYS = 45
RESERVED_BYTES = 96 * 1024 * 1024
MAX_ENTITY_ROWS = 4 * MAX_SLOTS * RETENTION_DAYS
MAX_TOTAL_ROWS = MAX_ENTITY_ROWS * MAX_ENTITIES
MAX_OBSERVATION_ROWS = RETENTION_DAYS * 96 * 5
PUBLIC_ROWS = 32
PROVENANCE_FIELDS = ('evaluated_at','source_time_basis','ttl_s','freshness_reason','daylight','ghi','ghi_reason')
REASONS = ('ok', 'entity_source_missing', 'binding_changed', 'source_stale',
           'target_missing', 'global_horizontal_missing', 'sun_below_limit',
           'measurement_gaps', 'invalid_sample', 'no_distinct_samples',
           'reference_unverified', 'reference_missing', 'diagnostic_budget', 'collecting', 'location_missing_or_invalid', 'diagnostic_error')


def number(value, signed=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) and (signed or v >= 0) else None
    except (ValueError, TypeError):
        return None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def encode_diagnostic(value):
    """Dieselben kompakten UTF-8-Bytes für Größenprüfung und Dateiausgabe."""
    return canonical(value).encode("utf-8")


def revision(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def roof_entity(roof, model, index):
    group = roof.get('resource_key')
    geometry = {k: roof.get(k) for k in ('tilt', 'azimuth', 'kwp')}
    # Die Position benennt nur alte, nicht benannte Abrufe; sie weist keine Gruppe zu.
    surface = roof.get('surface_id') or 'legacy_request_' + str(index + 1)
    return {'descriptor': {'model': model, 'kind': 'surface', 'surface': surface,
            'group_bindings': [{'group': group, 'share': 1.0}] if group else None,
            'binding_reason': 'ok' if group else 'reference_missing', 'geometry': geometry,
            'raw_stage': 'provider_hourly_kw_before_group_sum',
            'contribution_formula': '250*((1-a)*p0*w0+a*p1*w1)',
            'unavailable_stages': 'individual_bias_and_caps_do_not_exist'},
            'series': {}, 'ghi': {}}


def site_entity(site):
    bindings = [{'group': a.get('group_id'), 'share': number(a.get('share'))}
                for a in site.get('allocations', []) if isinstance(a, dict)]
    valid = bool(bindings) and all(a['group'] and a['share'] is not None and a['share'] > 0 for a in bindings)
    return {'descriptor': {'model': 'm3', 'kind': 'provider_resource',
            'resource': site.get('resource_id'), 'group_bindings': bindings if valid else None,
            'binding_reason': 'ok' if valid else 'reference_missing',
            'surface': None, 'surface_reason': 'reference_missing',
            'raw_stage': 'provider_hourly_kw_before_group_sum',
            'contribution_formula': '250*((1-a)*p0*w0+a*p1*w1)',
            'unavailable_stages': 'individual_bias_and_caps_do_not_exist'}, 'series': {}, 'ghi': {}}


def capture_value(entity, target, value):
    if entity is not None:
        entity['source_at_utc_s'] = int(time.time())
        entity['series'][target] = entity['series'].get(target, 0.0) + value


def capture_series(entity, series):
    if entity is not None:
        entity['source_at_utc_s'] = int(time.time())
        entity['series'] = dict(series)


def capture_radiation(entity, target, value, rad_best, index, v_icon, v_ecmwf, v_best):
    if entity is None:
        return
    used = ([v_icon, v_ecmwf, v_best] if v_icon is not None and v_ecmwf is not None
            else [v_ecmwf, v_best] if v_ecmwf is not None else [v_best])
    valid = index < len(rad_best) and number(rad_best[index]) is not None and all(number(v) is not None for v in used)
    entity['source_at_utc_s'] = int(time.time())
    entity['series'][target] = value if valid else None


def diagnostic_path(forecast_path):
    return str(forecast_path).removesuffix('.json') + '_entities.json'


def pack_diagnostic(forecasts, slots):
    """Identitäten einmal binden, Einzelwerte außerhalb der Regeldatei halten."""
    descriptors = []; ids = {}; rows = []; solar = None; provenance = []; provenance_ids = {}
    for forecast in forecasts:
        stamp = forecast['start_timestamp']
        payload = slots.get(stamp) or {'status': 'diagnostic_error', 'entities': []}
        solar = solar or payload.get('solar_model')
        entries = []
        for e in payload.get('entities', []):
            key = revision(e['descriptor'])
            if key not in ids:
                ids[key] = len(descriptors); descriptors.append(e['descriptor'])
            metadata = compact_provenance(e['diagnostic_provenance']) if isinstance(e.get('diagnostic_provenance'),dict) else None
            metadata_id = None
            if metadata is not None:
                metadata_key = canonical(metadata)
                if metadata_key not in provenance_ids:
                    provenance_ids[metadata_key] = len(provenance); provenance.append(metadata)
                metadata_id = provenance_ids[metadata_key]
            entries.append([ids[key], e.get('source_at_utc_s'), e.get('fresh') is True,
                            e.get('reason'), e.get('values'), e.get('clarity_reason'), metadata_id])
        rows.append([stamp, payload.get('status'), entries])
    output = {'schema_version': SCHEMA, 'decision_use_allowed': False,
              'forecast_issue_id': forecasts[0]['forecast_issue_id'], 'descriptors': descriptors,
              'solar_model': solar, 'slots': rows, 'provenance': provenance}
    if len(encode_diagnostic(output)) > MAX_ISSUE_BYTES or len(descriptors) > MAX_ENTITIES or len(rows) > MAX_SLOTS:
        output.update(descriptors=[], solar_model=None, slots=[], provenance=[], status='diagnostic_budget')
    return output


def attach_diagnostic(forecasts, payload):
    """Nur der Evidenzleser ergänzt die passende Ausgabe, niemals ein Regelleser."""
    reason = 'entity_source_missing'
    if isinstance(payload, dict) and payload.get('schema_version') == SCHEMA and payload.get('decision_use_allowed') is False:
        if payload.get('forecast_issue_id') == forecasts[0].get('forecast_issue_id'):
            reason = payload.get('status', 'ok')
        else:
            reason = 'binding_changed'
    output = []
    rows = {r[0]: r for r in payload.get('slots', [])} if reason == 'ok' else {}
    descriptors = payload.get('descriptors', []) if reason == 'ok' else []
    for f in forecasts:
        row = rows.get(f['start_timestamp'])
        data = {'schema_version': SCHEMA, 'decision_use_allowed': False, 'status': reason,
                'solar_model': payload.get('solar_model') if reason == 'ok' else None, 'entities': []}
        if row is not None:
            data['status'] = row[1]
            for e in row[2]:
                metadata = e[6] if len(e) > 6 else None
                pool = payload.get('provenance', [])
                if isinstance(metadata,int) and not isinstance(metadata,bool):
                    metadata = pool[metadata] if isinstance(pool,list) and 0 <= metadata < len(pool) else None
                data['entities'].append({'descriptor': descriptors[e[0]], 'source_at_utc_s': e[1],
                    'fresh': e[2], 'reason': e[3], 'values': e[4], 'clarity_reason': e[5],
                    'diagnostic_provenance': expanded_provenance(metadata) if isinstance(metadata,(list,dict)) else None})
        elif reason == 'ok':
            data['status'] = 'target_missing'
        output.append({**f, 'pv_diagnostic_entities': data})
    return output


def capture_ghi(entity, hourly, index, target):
    """Nur vorhandene horizontale Globalstrahlung; geneigte Strahlung ist kein Ersatz."""
    if entity is None:
        return
    values = hourly.get('shortwave_radiation', hourly.get('shortwave_radiation_best_match', []))
    entity['ghi'][target] = number(values[index]) if isinstance(values, list) and index < len(values) else None


def source_binding(service, model):
    topology = service._pv_topology_contract_or_unbound()
    return {'topology': topology.get('revision'), 'roofs': getattr(service, 'roofs', []),
            'sites': [{k: s.get(k) for k in ('resource_id', 'allocations')}
                      for s in getattr(service, 'solcast_sites', [])] if model == 'm3' else [],
            'location': [getattr(service, 'lat', None), getattr(service, 'lon', None)],
            'location_valid': getattr(service, '_solar_location_valid', False)}


def cache_payload(service, model, entities):
    couplings = {r.get('resource_key'): r.get('coupling') for r in service._pv_topology_contract_or_unbound().get('resources', [])}
    for entity in entities:
        for binding in entity['descriptor'].get('group_bindings') or []:
            binding['measurement_boundary'] = couplings.get(binding['group'])
            binding['boundary_reason'] = 'ok' if binding['measurement_boundary'] else 'reference_missing'
    return json.loads(canonical({'schema_version': SCHEMA, 'binding': source_binding(service, model),
            'source_at_utc_s': int(time.time()), 'source_time_basis': 'response_capture',
            'entities': entities}))


def solar_model(target, lat, lon):
    """NOAA, Kasten/Young, Spencer, Ineichen/Perez; TL=3, Höhe=0, Albedo=.20."""
    lat, lon = number(lat, True), number(lon, True)
    if lat is None or lon is None or not -66 <= lat <= 66 or not -180 <= lon <= 180:
        return None
    dt = datetime.fromtimestamp(target, timezone.utc)
    day = dt.timetuple().tm_yday
    hour = dt.hour + dt.minute / 60 + dt.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    eq = 229.18 * (.000075 + .001868*math.cos(gamma) - .032077*math.sin(gamma)
                  - .014615*math.cos(2*gamma) - .040849*math.sin(2*gamma))
    dec = (.006918 - .399912*math.cos(gamma) + .070257*math.sin(gamma)
           - .006758*math.cos(2*gamma) + .000907*math.sin(2*gamma)
           - .002697*math.cos(3*gamma) + .00148*math.sin(3*gamma))
    ha = math.radians(((hour * 60 + eq + 4 * lon) % 1440) / 4 - 180)
    phi = math.radians(lat)
    cosz = max(-1., min(1., math.sin(phi)*math.sin(dec)+math.cos(phi)*math.cos(dec)*math.cos(ha)))
    elevation = math.degrees(math.asin(cosz))
    az = (math.degrees(math.atan2(math.sin(ha), math.cos(ha)*math.sin(phi)-math.tan(dec)*math.cos(phi)))+180)%360
    if elevation < 10:
        return {'elevation': elevation, 'azimuth': az, 'ghi': None, 'dni': None, 'dhi': None}
    tangent = math.tan(math.radians(elevation))
    apparent = elevation + (58.1/tangent - .07/tangent**3 + .000086/tangent**5)/3600
    zenith = 90 - apparent
    cosine = math.cos(math.radians(zenith))
    am = 1/(cosine + .50572*(96.07995-zenith)**-1.6364)
    b = 2*math.pi*(day-1)/365
    extra = 1366.1*(1.00011+.034221*math.cos(b)+.00128*math.sin(b)+.000719*math.cos(2*b)+.000077*math.sin(2*b))
    ghi = .868*extra*cosine*math.exp(-.0387*am*3)
    dni = min(.827*extra*math.exp(-.18*am), ghi*(1-(.1-.2*math.exp(-3))/.982)/cosine)
    return {'elevation': elevation, 'azimuth': az, 'ghi': ghi, 'dni': dni, 'dhi': ghi-dni*cosine}


def clear_power(model, roofs):
    if not model or model['ghi'] is None or not roofs:
        return None
    total = 0.
    for roof in roofs:
        tilt, azimuth, kwp = number(roof.get('tilt')), number(roof.get('azimuth'), True), number(roof.get('kwp'))
        if tilt is None or azimuth is None or kwp is None:
            return None
        beta = math.radians(tilt)
        z = math.radians(90 - model['elevation'])
        incidence = max(0., math.cos(z)*math.cos(beta) + math.sin(z)*math.sin(beta)*math.cos(math.radians(model['azimuth'] - (azimuth+180)%360)))
        poa = model['dni']*incidence + model['dhi']*(1+math.cos(beta))/2 + model['ghi']*.2*(1-math.cos(beta))/2
        total += kwp * poa * .85
    return total


def slot_payload(service, current, adjacent, a, start):
    """Additiv und fehlerneutral; keinerlei Rückfluss in die Prognoserechnung."""
    result = {'schema_version': SCHEMA, 'decision_use_allowed': False, 'entities': [],
              'status': 'ok', 'solar_model': None}
    sm = None
    try:
        if getattr(service, '_solar_location_valid', False):
            result['solar_model'] = {'location': [service.lat, service.lon],
                'method': 'NOAA_IneichenPerez_TL3_alt0_KastenYoung_Spencer_albedo020',
                'roofs': getattr(service, 'roofs', [])}
        sm = solar_model(start+450, service.lat, service.lon) if result['solar_model'] else None
        for model in ('m1', 'm2', 'm3'):
            cache = getattr(service, '_model_cache', {}).get(model, {})
            data = cache.get('diagnostic_entities')
            if isinstance(data, dict) and data.get('reason') == 'diagnostic_error':
                result['status'] = 'diagnostic_error'
                result['entities'].append({'descriptor': {'model': model, 'kind': 'surface' if model != 'm3' else 'provider_resource'},
                    'source_at_utc_s': None, 'source_time_basis': 'unknown', 'fresh': False,
                    'reason': 'diagnostic_error', 'values': [None]*8, 'clarity_reason': 'diagnostic_error'})
                continue
            missing = ('entity_source_missing' if not isinstance(data, dict) or data.get('schema_version') != SCHEMA
                       else 'binding_changed' if data.get('binding') != source_binding(service, model) else None)
            if missing:
                expected = ([site_entity(site) for site in getattr(service, 'solcast_sites', [])]
                            if model == 'm3' else [roof_entity(roof, model, i) for i, roof in enumerate(getattr(service, 'roofs', []))])
                if missing == 'binding_changed' or result['status'] == 'ok':
                    result['status'] = missing
                for entity in expected:
                    result['entities'].append({'descriptor':entity['descriptor'], 'source_at_utc_s':None,
                        'source_time_basis':'unknown', 'fresh':False, 'reason':missing,
                        'values':[None]*8, 'clarity_reason':'global_horizontal_missing'})
                continue
            ts = number(data.get('source_at_utc_s'))
            # Frische ist Diagnoseprovenienz; sie ändert weder TTL noch Abruf.
            ttl = getattr(service, 'solcast_ttl_s', 3600) if model == 'm3' else 3600
            fresh = ts is not None and 0 <= time.time()-ts < ttl
            for entity in data.get('entities', [])[:MAX_ENTITIES+1]:
                ts = number(entity.get('source_at_utc_s',data.get('source_at_utc_s')))
                fresh = ts is not None and 0 <= time.time()-ts < ttl
                series = entity.get('series', {})
                p0 = number(series.get(str(current['ts']), series.get(current['ts'])))
                p1 = number(series.get(str(adjacent['ts']), series.get(adjacent['ts'])))
                w0 = number(current.get('diagnostic_weights', {}).get(model))
                w1 = number(adjacent.get('diagnostic_weights', {}).get(model))
                raw = 250*((1-a)*p0+a*p1) if p0 is not None and p1 is not None else None
                used = current.get(model) is not None and adjacent.get(model) is not None
                contribution = 250*((1-a)*p0*w0+a*p1*w1) if raw is not None and w0 is not None and w1 is not None and used else None
                ghi = entity.get('ghi', {})
                g0 = number(ghi.get(str(current['ts']), ghi.get(current['ts'])))
                g1 = number(ghi.get(str(adjacent['ts']), ghi.get(adjacent['ts'])))
                g = (1-a)*g0+a*g1 if g0 is not None and g1 is not None else None
                clarity = g/sm['ghi'] if g is not None and sm and sm['ghi'] else None
                result['entities'].append({'descriptor': entity['descriptor'], 'source_at_utc_s': ts,
                    'source_time_basis': 'response_capture', 'fresh': fresh,
                    'reason': 'target_missing' if raw is None else 'ok' if fresh else 'source_stale',
                    'values': [raw, contribution, p0, p1, w0, w1, a, clarity],
                    'clarity_reason': 'ok' if clarity is not None and fresh else 'source_stale' if clarity is not None else 'global_horizontal_missing' if g is None else 'location_missing_or_invalid' if sm is None else 'sun_below_limit'})
        if not result['entities'] and result['status']=='ok':
            result['status'] = 'entity_source_missing'
    except Exception:
        result['entities'] = []
        result['status'] = 'diagnostic_error'
    # Additive Belege für neue Ausgaben; Altbestände werden nicht rekonstruiert.
    evaluated = int(time.time())
    for entry in result['entities']:
        source = number(entry.get('source_at_utc_s'))
        ttl = getattr(service, 'solcast_ttl_s', 3600) if entry['descriptor']['model'] == 'm3' else 3600
        age = evaluated - source if source is not None else None
        freshness = ('source_missing' if entry['reason'] in ('entity_source_missing', 'binding_changed', 'diagnostic_error')
                     else 'time_missing' if source is None else 'time_future' if age < 0
                     else 'ttl_exceeded' if age >= ttl else 'fresh')
        entry['diagnostic_provenance'] = {
            'evaluated_at': evaluated, 'source_time_basis': entry.get('source_time_basis', 'unknown'),
            'ttl_s': ttl, 'freshness_reason': freshness,
            'daylight': sm['elevation'] >= 10 if sm else None,
            'ghi': None, 'ghi_reason': 'source_missing' if freshness == 'source_missing' else 'not_provided'}
        if entry['descriptor']['model'] == 'm2' and freshness != 'source_missing':
            data = getattr(service, '_model_cache', {}).get('m2', {}).get('diagnostic_entities', {})
            entity = next((e for e in data.get('entities', []) if e['descriptor'] == entry['descriptor']), {})
            ghi = entity.get('ghi', {})
            g0 = number(ghi.get(str(current['ts']), ghi.get(current['ts'])))
            g1 = number(ghi.get(str(adjacent['ts']), ghi.get(adjacent['ts'])))
            entry['diagnostic_provenance']['ghi'] = (1-a)*g0+a*g1 if g0 is not None and g1 is not None else None
            entry['diagnostic_provenance']['ghi_reason'] = 'ok' if g0 is not None and g1 is not None else 'support_missing'
    return result


TABLES = ('pv_entity_parameters', 'pv_entity_issues', 'pv_entity_bindings', 'pv_entity_values', 'pv_tracker_observations', 'pv_entity_diagnostics')


def initialize_tables(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS pv_entity_parameters (
        revision TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pv_entity_issues (
        id INTEGER PRIMARY KEY, issue_id TEXT UNIQUE NOT NULL REFERENCES forecast_issues(issue_id) ON DELETE CASCADE,
        status TEXT NOT NULL, solar_revision TEXT REFERENCES pv_entity_parameters(revision));
    CREATE TABLE IF NOT EXISTS pv_entity_bindings (
        id INTEGER PRIMARY KEY, revision TEXT UNIQUE NOT NULL, descriptor_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pv_entity_values (
        issue INTEGER NOT NULL REFERENCES pv_entity_issues(id) ON DELETE CASCADE,
        entity INTEGER NOT NULL REFERENCES pv_entity_bindings(id), target INTEGER NOT NULL,
        source_at INTEGER, fresh INTEGER NOT NULL, reason TEXT NOT NULL,
        raw_wh REAL, contribution_wh REAL, p0 REAL, p1 REAL, w0 REAL, w1 REAL, a REAL,
        clarity REAL, clarity_reason TEXT NOT NULL,
        PRIMARY KEY(issue, entity, target)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS pv_entity_diagnostics (
        issue INTEGER NOT NULL, entity INTEGER NOT NULL, target INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY(issue, entity, target),
        FOREIGN KEY(issue, entity, target) REFERENCES pv_entity_values(issue, entity, target) ON DELETE CASCADE
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS pv_tracker_observations (
        topology TEXT NOT NULL, target INTEGER NOT NULL, tracker TEXT NOT NULL,
        observed_at INTEGER NOT NULL, payload_json TEXT NOT NULL, model_revision TEXT REFERENCES pv_entity_parameters(revision),
        PRIMARY KEY(topology, target, tracker)) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS pv_entity_target ON pv_entity_values(target);
    CREATE INDEX IF NOT EXISTS pv_entity_by_entity ON pv_entity_values(entity);
    CREATE INDEX IF NOT EXISTS pv_tracker_target ON pv_tracker_observations(target);
    CREATE TABLE IF NOT EXISTS pv_entity_budget_state (id INTEGER PRIMARY KEY CHECK(id=1), dropped_values INTEGER NOT NULL, dropped_observations INTEGER NOT NULL);
    INSERT OR IGNORE INTO pv_entity_budget_state VALUES(1,0,0);
    ''')
    for name in TABLES:
        c.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_no_update BEFORE UPDATE ON {name} BEGIN SELECT RAISE(ABORT, 'diagnostic rows are immutable'); END")


def tables_ready(c):
    return set(TABLES + ('pv_entity_budget_state',) + tuple(t+'_no_update' for t in TABLES) + ('pv_entity_target','pv_entity_by_entity','pv_tracker_target')).issubset({r[0] for r in c.execute('SELECT name FROM sqlite_master')})


def first_entities(entries):
    seen=set()
    for slot in entries:
        for entity in slot['pv_diagnostic_entities'].get('entities', []):
            key=revision(entity['descriptor'])
            if key not in seen:
                seen.add(key)
                yield entity


def budget_bytes(c):
    """Tatsächlich belegte SQLite-Seiten einschließlich aller eigenen Indizes."""
    return c.execute("SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name IN "
        "(SELECT name FROM sqlite_master WHERE tbl_name LIKE 'pv_entity_%' OR tbl_name='pv_tracker_observations')").fetchone()[0]


def budget_limit(c):
    # Bestehende Tabellen erhalten mindestens den übrigen 160-MiB-Anteil.
    from .pv_forecast_evidence import MAX_DATABASE_BYTES
    page = c.execute('PRAGMA page_size').fetchone()[0]
    occupied = (c.execute('PRAGMA page_count').fetchone()[0] - c.execute('PRAGMA freelist_count').fetchone()[0]) * page
    return max(0, min(RESERVED_BYTES, MAX_DATABASE_BYTES - (occupied - budget_bytes(c)) - 2 * page))


def note_loss(c, values=0, observations=0):
    c.execute('UPDATE pv_entity_budget_state SET dropped_values=dropped_values+?, '
              'dropped_observations=dropped_observations+? WHERE id=1', (values, observations))


def trim_budget(c, extra_bytes=0, incoming_values=0, incoming_observations=0, incoming_entities=None):
    """Nur älteste Zusatzreihen entfernen, Bestands- und Gruppenreihen erhalten."""
    incoming_entities = incoming_entities or {}
    while True:
        value_count = c.execute('SELECT COUNT(*) FROM pv_entity_values').fetchone()[0]
        obs_count = c.execute('SELECT COUNT(*) FROM pv_tracker_observations').fetchone()[0]
        excessive_entity = any(c.execute('SELECT COUNT(*) FROM pv_entity_values WHERE entity='
            '(SELECT id FROM pv_entity_bindings WHERE revision=?)', (key,)).fetchone()[0] + count > MAX_ENTITY_ROWS
            for key, count in incoming_entities.items())
        if (budget_bytes(c) + extra_bytes <= budget_limit(c) and value_count + incoming_values <= MAX_TOTAL_ROWS
                and obs_count + incoming_observations <= MAX_OBSERVATION_ROWS and not excessive_entity):
            return True
        issue = c.execute('SELECT e.id,i.issued_at_utc_s FROM pv_entity_issues e JOIN forecast_issues i ON i.issue_id=e.issue_id '
            'WHERE EXISTS(SELECT 1 FROM pv_entity_values v WHERE v.issue=e.id) ORDER BY i.issued_at_utc_s,e.id LIMIT 1').fetchone()
        observation = c.execute('SELECT MIN(target) FROM pv_tracker_observations').fetchone()[0]
        if issue is None and observation is None:
            return False
        if issue is not None and (observation is None or issue[1] <= observation):
            dropped = c.execute('SELECT COUNT(*) FROM pv_entity_values WHERE issue=?', (issue[0],)).fetchone()[0]
            c.execute('DELETE FROM pv_entity_issues WHERE id=?', (issue[0],))
            note_loss(c, values=dropped)
        else:
            dropped = c.execute('DELETE FROM pv_tracker_observations WHERE target=?', (observation,)).rowcount
            note_loss(c, observations=dropped)
        clean_bindings(c)


def clean_bindings(c):
    c.execute('DELETE FROM pv_entity_bindings WHERE id NOT IN (SELECT entity FROM pv_entity_values)')
    c.execute('DELETE FROM pv_entity_parameters WHERE revision NOT IN '
        '(SELECT solar_revision FROM pv_entity_issues WHERE solar_revision IS NOT NULL UNION '
        'SELECT model_revision FROM pv_tracker_observations WHERE model_revision IS NOT NULL)')


def archive(c, issue_id, slots):
    entries = [s for s in slots if isinstance(s.get('pv_diagnostic_entities'), dict)]
    if not entries:
        return
    valid = all(s['pv_diagnostic_entities'].get('schema_version') == SCHEMA and s['pv_diagnostic_entities'].get('decision_use_allowed') is False for s in entries)
    descriptors = {revision(e['descriptor']) for s in entries for e in s['pv_diagnostic_entities'].get('entities', [])}
    count = sum(len(s['pv_diagnostic_entities'].get('entities', [])) for s in entries)
    budget = (not valid or len(descriptors)>MAX_ENTITIES or len(entries)>MAX_SLOTS or count>MAX_TOTAL_ROWS
              or count*192 + sum(len(canonical(e['descriptor']).encode()) for e in first_entities(entries))>MAX_ISSUE_BYTES)
    incoming={}
    for slot in entries:
        for entity in slot['pv_diagnostic_entities'].get('entities', []):
            key=revision(entity['descriptor'])
            incoming[key]=incoming.get(key,0)+1
    budget = budget or any(n > MAX_ENTITY_ROWS for n in incoming.values())
    if not budget:
        budget = not trim_budget(c, count * 192 + 32768, count, incoming_entities=incoming)
    first = entries[0]['pv_diagnostic_entities']
    solar=first.get('solar_model')
    solar_revision=revision(solar) if isinstance(solar,dict) else None
    if solar_revision:
        c.execute('INSERT OR IGNORE INTO pv_entity_parameters VALUES(?,?)',(solar_revision,canonical(solar)))
    c.execute('INSERT OR IGNORE INTO pv_entity_issues(issue_id,status,solar_revision) VALUES(?,?,?)',
              (issue_id, 'diagnostic_budget' if budget else first.get('status', 'collecting'), solar_revision))
    issue = c.execute('SELECT id FROM pv_entity_issues WHERE issue_id=?',(issue_id,)).fetchone()[0]
    if budget:
        note_loss(c, values=count)
        return
    bindings={}
    for e in first_entities(entries):
        key=revision(e['descriptor'])
        c.execute('INSERT OR IGNORE INTO pv_entity_bindings(revision,descriptor_json) VALUES(?,?)',
                  (key,canonical(e['descriptor'])))
        bindings[key]=c.execute('SELECT id FROM pv_entity_bindings WHERE revision=?',(key,)).fetchone()[0]
    records=[]; provenance=[]
    existing={(r[0],r[1]) for r in c.execute('SELECT entity,target FROM pv_entity_values WHERE issue=?',(issue,))}
    for s in entries:
        start = number(s.get('start_timestamp'))
        if start is None or start%900000:
            continue
        for e in s['pv_diagnostic_entities'].get('entities', []):
            v=e.get('values', [])
            if not isinstance(v,list) or len(v)!=8:
                continue
            records.append((issue,bindings[revision(e['descriptor'])],int(start/1000),
                 number(e.get('source_at_utc_s')),int(e.get('fresh') is True),
                 e.get('reason') if e.get('reason') in REASONS else 'invalid_sample',*[number(x) for x in v],
                 e.get('clarity_reason') if e.get('clarity_reason') in REASONS else 'invalid_sample'))
            if (records[-1][1],records[-1][2]) not in existing and isinstance(e.get('diagnostic_provenance'),dict):
                provenance.append((*records[-1][:3],canonical(compact_provenance(e['diagnostic_provenance']))))
    c.executemany('INSERT OR IGNORE INTO pv_entity_values VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',records)
    c.executemany('INSERT OR IGNORE INTO pv_entity_diagnostics VALUES(?,?,?,?)',provenance)
    trim_budget(c)


def retention(c, cutoff):
    c.execute('DELETE FROM pv_entity_issues WHERE issue_id IN '
              '(SELECT issue_id FROM forecast_issues WHERE issued_at_utc_s<?)', (cutoff,))
    c.execute('DELETE FROM pv_tracker_observations WHERE target+900<?', (cutoff,))
    clean_bindings(c)
    trim_budget(c)


class MpptAccumulator:
    """Verschiedene frische Quellen, Trapezregel, maximal 45 s, keine Extrapolation.

    Gemeinsame Blocklesung beweist keine Trackerqualität oder MPPT→Fläche.
    Gruppen-Klarheit verwendet ausschließlich das Modell und die andere Gruppe.
    """
    def __init__(self):
        self.previous = {}
        self.highwater = {}
        self.slots = {}
        self.started = None

    def observe(self, live, topology, now_s, config=None):
        if self.started is None:
            self.started=now_s
        if not isinstance(live,dict) or not isinstance(topology,str):
            self.previous.clear()
            return
        config=config or {}
        block=live.get('ext_inverter') or {}
        ts=number(block.get('sample_ts'))
        age=now_s-ts if ts is not None else None
        limit=number(block.get('max_age_s'))
        block_valid=(block.get('enabled') is True and block.get('valid') is True and age is not None
                     and limit is not None and 0<=age<=min(limit,45))
        samples={}
        for index in (1,2,3):
            matches=[x for x in block.get('mppt',[]) if isinstance(x,dict) and x.get('index')==index]
            item=matches[0] if len(matches)==1 else {}
            v,i,w=number(item.get('v')),number(item.get('i')),number(item.get('w'))
            valid=block_valid and v is not None and i is not None and w is not None and abs(v*i-w)<=max(2,w*.01)
            samples['mppt_'+str(index)]=(ts,w,v,i,valid,age)
        # Vorhandene neutrale Bilanz am RSCP-Messpunkt, ohne weitere Abfrage.
        rt=number(live.get('_ts')); ext=number(live.get('Ext_PV_Power')); total=number(live.get('PV_Power'))
        rs_valid=(rt is not None and 0<=now_s-rt<=45 and live.get('RSCP_Sample_Valid') is True
            and live.get('Ext_PV_Power_Valid') is True and live.get('Ext_PV_Power_Source')=='e3dc_add_power'
            and number(live.get('Ext_PV_Power_Age_S')) is not None and live['Ext_PV_Power_Age_S']<=45 and ext is not None)
        samples['reference_ac']=(rt,ext,None,None,rs_valid,now_s-rt if rt else None)
        samples['reference_dc']=(rt,total-ext if total is not None and ext is not None and total>=ext else None,
                                 None,None,rs_valid and total is not None and total>=ext,now_s-rt if rt else None)
        for tracker,(stamp,watts,volt,amp,valid,age) in samples.items():
            start=int(now_s//900)*900
            state=self.slots.setdefault((topology,start,tracker),self.empty(start,config))
            state['age']=age
            if not valid or watts is None:
                self.previous.pop(tracker,None)
                state['invalid']+=1
                continue
            high=self.highwater.get(tracker)
            if high is not None and stamp<=high:
                if stamp<high:
                    self.previous.pop(tracker,None)
                    state['regressions']+=1
                continue
            self.highwater[tracker]=stamp
            before=self.previous.get(tracker)
            self.previous[tracker]=(stamp,watts,volt,amp,topology)
            state['samples']+=1
            state['last_source']=stamp
            if not before or before[4]!=topology or stamp-before[0]>45:
                state['gaps']+=1
                continue
            gap=stamp-before[0]; cursor=before[0]
            while cursor<stamp:
                slot=int(cursor//900)*900; stop=min(stamp,slot+900); duration=stop-cursor
                st=self.slots.setdefault((topology,slot,tracker),self.empty(slot,config))
                st['last_source']=stamp
                st['covered']+=duration
                st['segments']+=1
                st['max_gap']=max(st['max_gap'],gap)
                for name,x0,x1 in [('energy',before[1],watts),('voltage',before[2],volt),('current',before[3],amp)]:
                    if x0 is None or x1 is None:
                        continue
                    y0=x0+(x1-x0)*(cursor-before[0])/gap; y1=x0+(x1-x0)*(stop-before[0])/gap
                    st[name]+=(y0+y1)*.5*duration/(3600 if name=='energy' else 1)
                    if name!='energy':
                        st[name+'_min']=min(st[name+'_min'],y0,y1); st[name+'_max']=max(st[name+'_max'],y0,y1)
                cursor=stop

    def empty(self,start,config):
        return {'energy':0.,'covered':0.,'segments':0,'max_gap':0.,'samples':0,'gaps':0,'invalid':0,
                'regressions':0,'age':None,'last_source':None,'restart': self.started>start,
                'voltage':0.,'voltage_min':math.inf,'voltage_max':0.,
                'current':0.,'current_min':math.inf,'current_max':0.,
                'config':{k:config.get(k) for k in ('hoehe','laenge','pv_forecast_topology_config')}}

    def closed(self,now_s):
        rows=[]
        for (topology,start,tracker),s in list(self.slots.items()):
            if start+945>now_s:
                continue
            complete=abs(s['covered']-900)<1e-6
            r={'schema_version':SCHEMA,'topology':topology,'target':start,'tracker':tracker,
                'observed_at':int(now_s),'decision_use_allowed':False,
                'energy_wh':round(s['energy'],6) if complete else None,
                'partial_energy_wh':round(s['energy'],6) if s['segments'] else None,
                'covered_seconds':round(s['covered'],6),'uncovered_seconds':round(900-s['covered'],6),
                'valid':complete,'reason':'ok' if complete else 'invalid_sample' if s['invalid'] else 'no_distinct_samples' if not s['segments'] else 'measurement_gaps',
                'samples':s['samples'],'gaps':s['gaps'],'invalid_samples':s['invalid'],
                'restart':s['restart'],'clock_regressions':s['regressions'],
                'max_gap_s':s['max_gap'],'source_age_s':number(s['age']),
                'last_source_at':s['last_source'],
                'age_at_close_s':now_s-s['last_source'] if s['last_source'] is not None else None,
                'tracker_quality':'unknown','surface_binding':None,'surface_binding_reason':'reference_missing'}
            for name in ('voltage','current'):
                r[name+'_mean']=s[name]/s['covered'] if s['covered'] and tracker.startswith('mppt_') else None
                r[name+'_min']=number(s[name+'_min']); r[name+'_max']=s[name+'_max'] if r[name+'_min'] is not None else None
            if tracker.startswith('reference_'):
                r.update(observed_clarity(s['config'],start,tracker,s['energy'] if complete else None))
                r['model_parameters']={'config':s['config'],'method':'NOAA_IneichenPerez_TL3_alt0_KastenYoung_Spencer_albedo020'}
            rows.append(r); del self.slots[(topology,start,tracker)]
        return rows


def observed_clarity(config,start,tracker,energy):
    # Ohne belegte Gruppen-Geometrie keine Kopplungs-/Flächenzuordnung erfinden.
    try:
        from ..pv_forecast_topology import configured_generator_groups
    except ImportError:
        from pv_forecast_topology import configured_generator_groups
    groups=configured_generator_groups(config)
    source_coupling='E3DC_DC' if tracker=='reference_dc' else 'EXTERNAL_AC'
    source=[g for g in groups if g.get('coupling')==source_coupling and not g.get('invalid')]
    others=[g.get('group_id') for g in groups if g.get('coupling')!=source_coupling and not g.get('invalid')]
    model=solar_model(start+450,config.get('hoehe'),config.get('laenge'))
    roofs=source[0].get('surfaces',[]) if len(source)==1 else []
    power=clear_power(model,roofs)
    clarity=energy/(power*.25) if energy is not None and power is not None and power>=1000 else None
    return {'observed_clarity':clarity,'clarity_valid':False,
            'clarity_reason':'reference_unverified' if clarity is not None else 'reference_missing',
            'clarity_source_group':source[0].get('group_id') if len(source)==1 else None,
            'classified_groups':others,'clarity_source':'other_group_clear_model',
            'clear_model_power_w':power,'reference_independently_proven':False}


def store_observations(c,rows):
    available = min(576, max(0, MAX_OBSERVATION_ROWS))
    accepted = rows[:available]
    if accepted and not trim_budget(c, len(accepted)*1024 + 8192, incoming_observations=len(accepted)):
        accepted = []
    note_loss(c, observations=len(rows)-len(accepted))
    inserted=0
    for r in accepted:
        if r.get('schema_version')!=SCHEMA or r.get('decision_use_allowed') is not False:
            continue
        if r.get('tracker') not in ('mppt_1','mppt_2','mppt_3','reference_dc','reference_ac'):
            continue
        parameters=r.get('model_parameters')
        model_revision=revision(parameters) if isinstance(parameters,dict) else None
        if model_revision:
            c.execute('INSERT OR IGNORE INTO pv_entity_parameters VALUES(?,?)',(model_revision,canonical(parameters)))
        payload={'schema_version':'pv_tracker_values_v1',
            'values':[number(r.get(key)) for key in OBS_NUMBERS],
            'reason':REASONS.index(r.get('reason')) if r.get('reason') in REASONS else REASONS.index('invalid_sample'),
            'flags':int(r.get('valid') is True) + 2*int(r.get('restart') is True),
            'clarity_reason':REASONS.index(r.get('clarity_reason')) if r.get('clarity_reason') in REASONS else REASONS.index('reference_missing'),
            'reference':[r.get('clarity_source_group'),r.get('classified_groups',[])]}
        inserted += max(0,c.execute('INSERT OR IGNORE INTO pv_tracker_observations VALUES(?,?,?,?,?,?)',
                  (r['topology'],r['target'],r['tracker'],r['observed_at'],canonical(payload),model_revision)).rowcount)
    trim_budget(c)
    return {'inserted':inserted,'budget_dropped':len(rows)-len(accepted)}


def expanded_observation(row):
    """Lesbare lokale Einzelreihe aus dem festen, kompakten Archivformat."""
    payload=json.loads(row['payload_json'])
    if payload.get('schema_version')!='pv_tracker_values_v1':
        return payload
    values=payload.get('values',[])
    if not isinstance(values,list) or len(values)!=len(OBS_NUMBERS):
        return {}
    result=dict(zip(OBS_NUMBERS,values))
    flags=payload.get('flags',0)
    ref=payload.get('reference',[None,[]])
    result.update({'schema_version':SCHEMA,'decision_use_allowed':False,
        'topology':row['topology'],'tracker':row['tracker'],'valid':bool(flags&1),'restart':bool(flags&2),
        'reason':REASONS[payload['reason']], 'clarity_reason':REASONS[payload['clarity_reason']],
        'clarity_source_group':ref[0], 'classified_groups':ref[1],
        'tracker_quality':'unknown','surface_binding':None,'surface_binding_reason':'reference_missing',
        'reference_independently_proven':False,'clarity_valid':False,'clarity_source':'other_group_clear_model'})
    return result


def public_excerpt(c,topology, observation_window=None):
    # Nur belegte Methodenfamilien derselben Topologie verbinden.
    try:
        from .pv_forecast_method import evaluation_method_revisions
    except ImportError:
        from pv_forecast_method import evaluation_method_revisions
    issue=c.execute('SELECT e.id,e.status,i.issued_at_utc_s, '
        "CASE WHEN k.method_revision_status='complete' AND k.topology_revision_status='complete' THEN k.method_revision END FROM pv_entity_issues e "
        'JOIN forecast_issues i ON i.issue_id=e.issue_id LEFT JOIN forecast_issue_contracts k ON k.issue_id=i.issue_id '
        'WHERE i.topology_revision=? ORDER BY i.issued_at_utc_s DESC,e.id DESC LIMIT 1',(topology,)).fetchone()
    result={'schema_version':SCHEMA,'decision_use_allowed':False,'status':issue[1] if issue else 'collecting',
            'issued_at_utc_s':issue[2] if issue else None,'entities':[],'observations':[],'excerpt_limit':PUBLIC_ROWS}
    loss = c.execute('SELECT dropped_values,dropped_observations FROM pv_entity_budget_state WHERE id=1').fetchone()
    result['budget'] = {'reserved_bytes': RESERVED_BYTES, 'used_bytes': budget_bytes(c),
        'retention_days': RETENTION_DAYS, 'dropped_values': loss[0], 'dropped_observations': loss[1]}
    if any(loss): result['status'] = 'diagnostic_budget'
    groups={}; surfaces={}; resources={}
    def neutral(mapping,key,prefix):
        if key is None: return None
        if key not in mapping: mapping[key]=prefix+str(len(mapping)+1)
        return mapping[key]
    if issue:
        family=evaluation_method_revisions(issue[3]) if issue[3] else ()
        marks=','.join('?' for _ in family) or 'NULL'
        identity="json_extract(b.descriptor_json,'$.model'),json_extract(b.descriptor_json,'$.kind'),COALESCE(json_extract(b.descriptor_json,'$.surface'),json_extract(b.descriptor_json,'$.resource'),b.revision)"
        rows=c.execute('SELECT * FROM (SELECT v.*,b.descriptor_json,i.issued_at_utc_s,d.payload_json AS diagnostic_json, '
            f'MIN(v.entity) OVER (PARTITION BY {identity}) AS stable_entity, '
            f'ROW_NUMBER() OVER (PARTITION BY {identity} ORDER BY '
            "CASE WHEN v.fresh=1 AND v.raw_wh IS NOT NULL AND (COALESCE(json_extract(d.payload_json,'$[4]'),json_extract(d.payload_json,'$.daylight'))=1 OR (d.payload_json IS NULL AND v.clarity_reason='ok' AND v.clarity IS NOT NULL)) THEN 0 ELSE 1 END, "
            'i.issued_at_utc_s DESC,e.id DESC,v.target DESC) AS selection_rank '
            'FROM pv_entity_values v JOIN pv_entity_bindings b ON b.id=v.entity '
            'LEFT JOIN pv_entity_diagnostics d ON d.issue=v.issue AND d.entity=v.entity AND d.target=v.target '
            'JOIN pv_entity_issues e ON e.id=v.issue JOIN forecast_issues i ON i.issue_id=e.issue_id '
            'LEFT JOIN forecast_issue_contracts k ON k.issue_id=i.issue_id '
            f'WHERE i.topology_revision=? AND (e.id=? OR (k.method_revision IN ({marks}) '
            "AND k.method_revision_status='complete' AND k.topology_revision_status='complete'))) "
            'WHERE selection_rank=1 ORDER BY entity LIMIT ?',(topology,issue[0],*family,PUBLIC_ROWS)).fetchall()
        for row in rows:
            meta=expanded_provenance(json.loads(row['diagnostic_json'])) if row['diagnostic_json'] else {}
            # Ein gespeicherter gültiger Klarheitswert belegt bereits Tageslicht;
            # daraus werden weder Quellenzeiten noch horizontales GHI erfunden.
            daylight_value=bool(row['fresh'] and row['raw_wh'] is not None and (meta.get('daylight') is True
                or not meta and row['clarity_reason']=='ok' and row['clarity'] is not None))
            d=json.loads(row['descriptor_json']); group_bindings=d.get('group_bindings') or []
            meta=expanded_provenance(json.loads(row['diagnostic_json'])) if row['diagnostic_json'] else {}
            result['entities'].append({'entity':'entity_'+str(row['stable_entity']), 'model':d.get('model'),
                'kind':d.get('kind'),'surface':neutral(surfaces,d.get('surface'),'surface_'),
                'resource':neutral(resources,d.get('resource'),'resource_'),
                'groups':[{'group':neutral(groups,g.get('group'),'group_'),'share':g.get('share'),'measurement_boundary':g.get('measurement_boundary')} for g in group_bindings],
                'target':row['target'],'source_at':row['source_at'],'fresh':bool(row['fresh']),
                'daylight':meta.get('daylight'),'issued_at':row['issued_at_utc_s'],'historical':row['issue']!=issue[0],
                'selection_reason':'daylight_value' if daylight_value else 'no_valid_daylight_value' if meta else 'provenance_unknown',
                'reason':row['reason'],'raw_wh':row['raw_wh'],'contribution_wh':row['contribution_wh'],
                'w0':row['w0'],'w1':row['w1'],'clarity':row['clarity'],'clarity_reason':row['clarity_reason'],
                **{k:meta.get(k) for k in PROVENANCE_NUMBERS},
                **{k:meta.get(k,'unknown') for k in PROVENANCE_TOKENS if k != 'selection_reason'}})
    if observation_window is None:
        observation_window=c.execute('SELECT MAX(target) FROM pv_tracker_observations WHERE topology=?',(topology,)).fetchone()[0]
    result['observation_window_start']=number(observation_window)
    result['observation_window_end']=observation_window+900 if number(observation_window) is not None else None
    for row in c.execute('SELECT * FROM pv_tracker_observations WHERE topology=? AND target=? ORDER BY tracker',(topology,observation_window)):
        r=expanded_observation(row)
        r['clarity_source_group']=neutral(groups,r.get('clarity_source_group'),'group_')
        r['classified_groups']=[neutral(groups,g,'group_') for g in r.get('classified_groups',[])]
        result['observations'].append(r)
    return sanitize_public(result)


PROVENANCE_NUMBERS=('evaluated_at','ttl_s','ghi')
# Gespeicherte Codes nur anhängen; bestehende Reihenfolgen unverändert lassen.
PROVENANCE_TOKENS={
    'source_time_basis':('response_capture','unknown'),
    'freshness_reason':('fresh','source_missing','time_missing','time_future','ttl_exceeded','unknown'),
    'ghi_reason':('ok','source_missing','not_provided','support_missing','unknown'),
    'selection_reason':('daylight_value','no_valid_daylight_value','provenance_unknown')}


def compact_provenance(raw):
    """Kurze Positionswerte statt wiederholter Feldnamen und Grundtexte."""
    values=[raw.get(k) for k in PROVENANCE_FIELDS]
    for index in (1,3,6):
        allowed=PROVENANCE_TOKENS[PROVENANCE_FIELDS[index]]
        values[index]=allowed.index(values[index]) if values[index] in allowed else allowed.index('unknown')
    return values


def expanded_provenance(raw):
    # Auch die lesbare Form bleibt lesbar; Altzeilen ohne Beleg bleiben leer.
    if isinstance(raw,dict): return raw
    if not isinstance(raw,list) or len(raw)!=len(PROVENANCE_FIELDS): return {}
    values=list(raw)
    for index in (1,3,6):
        allowed=PROVENANCE_TOKENS[PROVENANCE_FIELDS[index]]; value=values[index]
        values[index]=(allowed[value] if isinstance(value,int) and not isinstance(value,bool) and 0<=value<len(allowed)
                       else value if isinstance(value,str) and value in allowed else 'unknown')
    return dict(zip(PROVENANCE_FIELDS,values))


ENTITY_NUMBERS=('issued_at',*PROVENANCE_NUMBERS,'target','source_at','raw_wh','contribution_wh','w0','w1','clarity')
OBS_NUMBERS=('target','observed_at','energy_wh','partial_energy_wh','covered_seconds','uncovered_seconds',
             'samples','gaps','invalid_samples','clock_regressions','max_gap_s','source_age_s','last_source_at','age_at_close_s',
             'voltage_mean','voltage_min','voltage_max','current_mean','current_min','current_max',
             'observed_clarity','clear_model_power_w')


def token(value,prefix):
    return value if isinstance(value,str) and value.startswith(prefix) and value[len(prefix):].isdigit() else None


def entity_list(value):
    return value if isinstance(value,list) else []


def sanitize_public(raw):
    if not isinstance(raw,dict) or raw.get('schema_version')!=SCHEMA or raw.get('decision_use_allowed') is not False:
        return None
    out={'schema_version':SCHEMA,'decision_use_allowed':False,
         'status':raw.get('status') if raw.get('status') in REASONS else 'collecting',
         'issued_at_utc_s':number(raw.get('issued_at_utc_s')),'excerpt_limit':PUBLIC_ROWS,'entities':[],'observations':[]}
    for k in ('observation_window_start','observation_window_end'): out[k]=number(raw.get(k))
    budget = raw.get('budget')
    if isinstance(budget, dict):
        out['budget'] = {k: number(budget.get(k)) for k in ('reserved_bytes','used_bytes','retention_days','dropped_values','dropped_observations')}
    for r in entity_list(raw.get('entities'))[:PUBLIC_ROWS]:
        if not isinstance(r,dict) or r.get('model') not in ('m1','m2','m3') or r.get('kind') not in ('surface','provider_resource'):
            continue
        item={k:number(r.get(k)) for k in ENTITY_NUMBERS}
        item.update({'entity':token(r.get('entity'),'entity_'),'model':r['model'],'kind':r['kind'],
            'surface':token(r.get('surface'),'surface_'),'resource':token(r.get('resource'),'resource_'),
            'fresh':r.get('fresh') is True,'reason':r.get('reason') if r.get('reason') in REASONS else 'invalid_sample',
            'clarity_reason':r.get('clarity_reason') if r.get('clarity_reason') in REASONS else 'invalid_sample',
            'groups':[{'group':token(g.get('group'),'group_'),'share':number(g.get('share')),'measurement_boundary':g.get('measurement_boundary') if g.get('measurement_boundary') in ('E3DC_DC','EXTERNAL_AC') else None} for g in entity_list(r.get('groups'))[:MAX_ENTITIES] if isinstance(g,dict)]})
        item['daylight']=r.get('daylight') if isinstance(r.get('daylight'),bool) else None
        item['historical']=r.get('historical') is True
        for k,allowed in PROVENANCE_TOKENS.items(): item[k]=r.get(k) if r.get(k) in allowed else 'unknown'
        out['entities'].append(item)
    for r in entity_list(raw.get('observations'))[:PUBLIC_ROWS]:
        if not isinstance(r,dict) or r.get('tracker') not in ('mppt_1','mppt_2','mppt_3','reference_dc','reference_ac'):
            continue
        item={k:number(r.get(k)) for k in OBS_NUMBERS}
        item.update({'tracker':r['tracker'],'valid':r.get('valid') is True,'restart':r.get('restart') is True,
            'reason':r.get('reason') if r.get('reason') in REASONS else 'invalid_sample',
            'tracker_quality':'unknown','surface_binding':None,'surface_binding_reason':'reference_missing',
            'clarity_valid':False,'clarity_reason':r.get('clarity_reason') if r.get('clarity_reason') in REASONS else 'reference_missing',
            'clarity_source_group':token(r.get('clarity_source_group'),'group_'),
            'classified_groups':[token(g,'group_') for g in entity_list(r.get('classified_groups'))[:MAX_ENTITIES]],
            'clarity_source':'other_group_clear_model','reference_independently_proven':False})
        if not item['valid']: item['energy_wh']=None
        out['observations'].append(item)
    return out
