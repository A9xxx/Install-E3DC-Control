"""Unveränderliche Viertelstundenkosten, additive Summen und einmalige Nachberechnung."""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import shlex
import sys
import subprocess
import shutil
import time
from zoneinfo import ZoneInfo

TABLE = 'quarter_hour_costs'
COLUMNS = ('ts_start', 'date', 'pv_kwh', 'grid_in_kwh', 'grid_out_kwh', 'bat_in_kwh',
           'bat_out_kwh', 'home_kwh', 'wb_kwh', 'wb2_kwh', 'wp_kwh', 'climate_kwh',
           'pv_to_bat_kwh', 'grid_to_bat_kwh', 'billing_price_ct', 'feedin_price_ct',
           'dv_market_price_ct', 'dv_net_sell_ct', 'grid_cost_eur', 'feedin_revenue_eur',
           'home_cost_eur', 'dv_fixed_fee_eur', 'net_cost_eur', 'battery_energy_kwh',
           'battery_mean_ct', 'complete', 'payload')


def config_for(root):
    with (Path(root) / 'data/e3dc_v4.json').open(encoding='utf-8') as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError('Konfiguration ungültig')
    return config


def enabled(config):
    return str(config.get('cost_accounting_quarterhour_enable', '0')) == '1'


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def ensure_schema(conn):
    # Ausschließlich additive Tabelle, keine Änderung an bestehenden Tabellen.
    columns = ['ts_start INTEGER PRIMARY KEY', 'date TEXT NOT NULL']
    columns += [name + ' REAL' for name in COLUMNS[2:-2]]
    columns += ['complete INTEGER NOT NULL', 'payload TEXT NOT NULL']
    conn.execute('CREATE TABLE IF NOT EXISTS quarter_hour_costs (' + ','.join(columns) + ')')


# Nur Felder, die Kostenrechnung und die gemeinsame Energiebilanz auswerten.
HISTORY_FIELDS = frozenset(('ts', 'soc', 'pv', 'pv_total_w', 'pv_external_w',
    'pv_external_topology_present', 'pv_external_charge_locked', 'dc0_w', 'dc1_w',
    'grid', 'bat', 'home', 'home_raw', 'wb', 'wb2', 'wp', 'hs', 'climate',
    'price_ct', 'cost_prices', 'rscp_sample_valid', 'is_external_wb', 'is_external_wb2',
    'wb_home_relation', 'wb2_home_relation', 'wb_native_type', 'wb2_native_type',
    'wb_source', 'wb2_source', 'wb_daily_source', 'wb2_daily_source'))


def read_history(paths, after=None, before=None, deadline=None):
    rows = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('Zeitgrenze beim Lesen der Minutenhistorie erreicht')
                # Eine beschädigte Zeile darf weder den Rest sperren noch als Messung zählen.
                if '\ufffd' in line:
                    continue
                try:
                    row = json.loads(line)
                    stamp = datetime.datetime.fromisoformat(row['ts'])
                    if stamp.tzinfo is None:
                        continue
                    at = stamp.timestamp()
                    if (after is not None and at < after) or (before is not None and at > before):
                        continue
                    # Keine Geräte-, Phasen- oder Diagnoseblöcke in der PHP-Eingabe.
                    rows[at] = {key: value for key, value in row.items()
                                if key in HISTORY_FIELDS or key.endswith('_valid')}
                except (ValueError, KeyError, TypeError):
                    continue
    return [rows[key] for key in sorted(rows)]


def calculate(conn, root, rows, starts, config, *, bootstrap=True, timeout=120, low_priority=False, initial_state=None):
    if not starts:
        return []
    existing = {}
    state = None
    if exists(conn):
        previous = conn.execute('SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start<? ORDER BY ts_start DESC LIMIT 1', (starts[0],)).fetchone()
        if previous:
            state = dict(json.loads(previous[1])['state'], last_ts=previous[0]+900)
        existing = {str(ts): json.loads(payload) for ts, payload in conn.execute(
            'SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start BETWEEN ? AND ?', (starts[0], starts[-1]))}
    if all(str(start) in existing for start in starts):
        return []
    if initial_state is not None:
        state = initial_state
    bootstrap_rows = []
    def needs_replay(value):
        return value is None or value.get('energy_kwh') is None or (
            value.get('energy_kwh', 0) > 1e-9 and value.get('mean_ct') is None)
    bootstrap_at, probe = None, state
    for start in starts:
        if str(start) in existing:
            probe = existing[str(start)].get('state')
        elif needs_replay(probe):
            bootstrap_at = start
            break
    replay_state = None
    if bootstrap and bootstrap_at is not None:
        # Der Anschlusswert wird tageweise verkettet. Keine 30 Tage im PHP-Array.
        zone = ZoneInfo('Europe/Berlin')
        day = datetime.datetime.fromtimestamp(bootstrap_at, zone).date()
        for offset in range(30, -1, -1):
            date = day - datetime.timedelta(days=offset)
            at = int(datetime.datetime.combine(date, datetime.time(), zone).timestamp())
            until = min(bootstrap_at, int(datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time(), zone).timestamp()))
            if until <= at:
                continue
            paths = [Path(root) / 'data/history_backups' / ('history_' + value.isoformat() + '.txt')
                     for value in (date - datetime.timedelta(days=1), date, date + datetime.timedelta(days=1))]
            samples = read_history(paths + [Path(root) / 'ramdisk/live_history.txt'], after=at-120, before=until + (120 if until < bootstrap_at else 0))
            if not samples:
                continue
            replay = subprocess.run(['php', '-d', 'memory_limit=128M', str(Path(root) / 'cost_accounting_cli.php')],
                input=json.dumps({'action': 'replay', 'rows': samples, 'before': until, 'config': config, 'state': replay_state}, allow_nan=False),
                text=True, capture_output=True, check=True, timeout=timeout)
            replay_state = json.loads(replay.stdout)
        if replay_state and bootstrap_at == starts[0]:
            state = replay_state
    result = subprocess.run(['php', '-d', 'memory_limit=128M', str(Path(root) / 'cost_accounting_cli.php')],
        input=json.dumps({'rows': rows, 'starts': starts, 'config': config, 'existing': existing, 'state': state,
                          'bootstrap_rows': bootstrap_rows, 'replay_state': replay_state}, allow_nan=False),
        text=True, capture_output=True, check=True, timeout=timeout,
        preexec_fn=_low_priority if low_priority and os.name == 'posix' else None)
    slots = json.loads(result.stdout)
    if not isinstance(slots, list) or any(slot.get('ts_start') not in starts for slot in slots):
        raise ValueError('Ungültige Antwort der Kostenberechnung')
    return slots


def insert(conn, slots):
    _ensure_aggregates(conn)
    for slot in slots:
        values = [slot.get(key) for key in COLUMNS[:-1]] + [json.dumps(slot, ensure_ascii=False, allow_nan=False, separators=(',', ':'))]
        conn.execute('INSERT INTO quarter_hour_costs (' + ','.join(COLUMNS) + ') VALUES (' + ','.join('?' for _ in COLUMNS) + ')', values)
        _extend_aggregates(conn, [slot])


def archive_costs(conn, root, now=None):
    root = Path(root)
    config = config_for(root)
    if not enabled(config):
        return 0
    now = int(time.time() if now is None else now)
    rows = read_history([root / 'ramdisk/live_history.txt'])
    # Erst die aufgezeichnete Aktivierung zählt; Altverläufe ergänzt die einmalige Hintergrundprüfung.
    stamps = [int(datetime.datetime.fromisoformat(row['ts']).timestamp()) for row in rows if 'cost_prices' in row]
    if not stamps:
        if exists(conn):
            try:
                conn.execute('BEGIN IMMEDIATE')
                _ensure_aggregates(conn, verify_count=True)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return 0
    start = max(min(stamps) // 900 * 900, (now - 48 * 3600) // 900 * 900)
    starts = list(range(start, now // 900 * 900, 900))
    def sealed_slots():
        if not exists(conn):
            return []
        if not starts:
            return []
        return conn.execute(
            'SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start BETWEEN ? AND ? '
            'OR ts_start=(SELECT MAX(ts_start) FROM quarter_hour_costs WHERE ts_start<?) ORDER BY ts_start',
            (start, starts[-1], start)).fetchall()

    # Lesen und PHP-Berechnung erfolgen vor der kurzen Schreibtransaktion.
    before = sealed_slots()
    slots = calculate(conn, root, rows, starts, config)
    if not slots:
        if exists(conn):
            try:
                conn.execute('BEGIN IMMEDIATE')
                _ensure_aggregates(conn, verify_count=True)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return 0
    try:
        conn.execute('BEGIN IMMEDIATE')
        # Auch der Akku-Anschlusswert muss noch zur Berechnung passen. Bei
        # paralleler Festschreibung rechnet erst der nächste Stundenlauf neu.
        if sealed_slots() != before:
            raise RuntimeError('Kostenbestand inzwischen geändert; erneute Berechnung im nächsten Archivlauf')
        ensure_schema(conn)
        _ensure_aggregates(conn, verify_count=True)
        insert(conn, slots)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(slots)


def _verified_cost_backup(archiver, *, deadline=None):
    """Nur Kosten, abgeleitete Summen und Nachberechnungszustand konsistent sichern."""
    deadline = min(time.monotonic() + 30, deadline if deadline is not None else float('inf'))
    directory = Path(archiver.DB_DIR) / 'statistics_backups'
    directory.mkdir(mode=0o770, exist_ok=True)
    backup = None
    reserve = 16 * 1024 * 1024
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('Zeitgrenze der Kostensicherung erreicht')
        if shutil.disk_usage(directory).free < reserve:
            raise OSError('Zu wenig freier Platz für die Kostensicherung')
    def progress():
        if time.monotonic() >= deadline:
            return 1
        return 0
    def digest(path):
        result = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                check()
                result.update(block)
        return result.hexdigest()
    try:
        check()
        backup = Path(__import__('tempfile').mkdtemp(prefix='kosten-', dir=directory))
        os.chmod(backup, 0o770)
        snapshot = backup / 'e3dc_stats.db'
        source = archiver._readonly_database(archiver.DB_PATH)
        destination = sqlite3.connect(snapshot)
        tables = []
        try:
            source.set_progress_handler(progress, 1000)
            destination.set_progress_handler(progress, 1000)
            source.execute('BEGIN')
            for table in (TABLE, AGGREGATES, 'cost_backfill_state'):
                schema = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if schema is None:
                    continue
                tables.append(table)
                destination.execute(schema[0])
                cursor = source.execute('SELECT * FROM ' + table)
                while True:
                    check()
                    rows = cursor.fetchmany(128)
                    if not rows:
                        break
                    size = sum(len(value.encode('utf-8')) for row in rows for value in row if isinstance(value, str))
                    if shutil.disk_usage(directory).free < reserve + size * 2:
                        raise OSError('Zu wenig freier Platz für die Kostensicherung')
                    destination.executemany('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
            destination.commit()
            if destination.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Kostensicherung ist nicht konsistent')
            check()
        finally:
            source.close()
            destination.close()
        os.chmod(snapshot, 0o660)
        with snapshot.open('rb') as stream:
            os.fsync(stream.fileno())
        binding = {'format': 'cost_tables_backup_v1', 'sha256': digest(snapshot),
                   'datenbank': str(Path(archiver.DB_PATH).resolve()), 'tables': tables}
        _write_json(backup / 'backup.json', binding)
        _write_json(backup / 'costs.json', {'format': 'quarter_hour_costs_backup_v2'})
        if digest(snapshot) != binding['sha256']:
            raise ValueError('Prüfsumme der Kostensicherung stimmt nicht')
        check()
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return backup
    except Exception:
        if backup is not None:
            shutil.rmtree(backup)
        raise


def _expire_cost_backup(root, state, now):
    """Den Rückweg 30 Tage nach Abschluss entfernen; andere Sicherungen erhalten."""
    if state.get('status') != 'completed' or now - state.get('completed_at', now) < 30 * 86400 or not state.get('backup'):
        return
    backup = Path(state['backup'])
    directory = (root / 'data/statistics_backups').resolve()
    if backup.resolve().parent != directory or backup.is_symlink() or not (backup / 'costs.json').is_file():
        return
    shutil.rmtree(backup)


def maintain(archiver, *, apply=False, today=None):
    root = Path(archiver.DB_DIR).parent
    config = config_for(root)
    if not enabled(config):
        raise ValueError('Experimentelle Kostenansicht ist ausgeschaltet')
    today = today or datetime.datetime.now(ZoneInfo('Europe/Berlin')).date()
    days = [today - datetime.timedelta(days=offset) for offset in range(30, 0, -1)]
    paths = [root / 'data/history_backups' / ('history_' + day.isoformat() + '.txt') for day in days]
    zone = ZoneInfo('Europe/Berlin')
    connection = (sqlite3.connect(Path(archiver.DB_PATH).resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
                  if apply else archiver._readonly_database(archiver.DB_PATH))
    report = {'modus': 'anwenden' if apply else 'trockenlauf', 'geplant': 0, 'geändert': 0, 'backup': None, 'rückweg': None,
              'fehlende_tage': [day.isoformat() for day, path in zip(days, paths) if not path.is_file()]}
    first = int(datetime.datetime.combine(days[0], datetime.time(), zone).timestamp())
    support_end = int(datetime.datetime.combine(today, datetime.time(), zone).timestamp())
    def inventory():
        return connection.execute('SELECT ts_start,payload FROM quarter_hour_costs WHERE ts_start>=? AND ts_start<? '
            'OR ts_start=(SELECT MAX(ts_start) FROM quarter_hour_costs WHERE ts_start<?) ORDER BY ts_start',
            (first, support_end, first)).fetchall() if exists(connection) else []
    try:
        before = inventory()
        # Auch der Supportlauf lädt und berechnet nur einen Tag zur selben Zeit.
        slots = []
        carry = None
        for day in days:
            deadline = time.monotonic() + 60
            start = int(datetime.datetime.combine(day, datetime.time(), zone).timestamp())
            next_day = day + datetime.timedelta(days=1)
            end = int(datetime.datetime.combine(next_day, datetime.time(), zone).timestamp())
            day_paths = [root / 'data/history_backups' / ('history_' + date.isoformat() + '.txt')
                         for date in (day - datetime.timedelta(days=1), day, next_day)]
            rows = read_history(day_paths + [root / 'ramdisk/live_history.txt'],
                                after=start-120, before=end+120, deadline=deadline)
            calculated = calculate(connection, root, rows, list(range(start, end, 900)), config,
                                   bootstrap=False, initial_state=carry, low_priority=True,
                                   timeout=max(0.01, deadline-time.monotonic())) if rows else []
            usable = any(slot.get('energy_valid') for slot in calculated)
            calculated = [slot for slot in calculated if slot.get('coverage_s', 0) > 0] if usable else []
            if time.monotonic() >= deadline:
                raise TimeoutError('Zeitgrenze der Support-Nachberechnung erreicht')
            slots.extend(calculated)
            last = max(calculated, key=lambda slot: slot['ts_start']) if calculated else None
            if exists(connection):
                saved = connection.execute('SELECT payload FROM quarter_hour_costs WHERE ts_start>=? AND ts_start<? ORDER BY ts_start DESC LIMIT 1', (start, end)).fetchone()
                if saved:
                    saved = json.loads(saved[0])
                    if last is None or saved['ts_start'] > last['ts_start']:
                        last = saved
            if last is not None:
                carry = dict(last['state'], last_ts=last['ts_start']+900)
        report['geplant'] = len(slots)
        report['vollständig'] = sum(slot['complete'] for slot in slots)
        if apply and slots:
            # Bestehende Backup-API einschließlich WAL, Integrität, Hash und Zeitgrenze.
            backup = archiver._verified_statistics_backup([], cost_only=True)
            description = {'format': 'quarter_hour_costs_backup_v1', 'slots': slots}
            manifest = backup / 'costs.json'
            with manifest.open('w', encoding='utf-8') as stream:
                json.dump(description, stream, ensure_ascii=False, allow_nan=False)
                stream.flush(); os.fsync(stream.fileno())
            connection.execute('BEGIN IMMEDIATE')
            current = inventory()
            if current != before:
                raise RuntimeError('Kostenbestand inzwischen geändert; Supportlauf erneut ausführen')
            ensure_schema(connection)
            insert(connection, slots)
            connection.commit()
            report.update({'geändert': len(slots), 'backup': str(backup),
                           'rückweg': shlex.join([sys.executable, '-B', str(Path(archiver.__file__).resolve()),
                               '--kosten-wiederherstellen', str(backup), '--web-root', str(root)])})
        else:
            connection.rollback()
    finally:
        connection.close()
    return report


def restore(archiver, backup):
    backup = Path(backup)
    binding = json.loads((backup / 'backup.json').read_text(encoding='utf-8'))
    snapshot = backup / 'e3dc_stats.db'
    if binding.get('sha256') != archiver._sha256_file(snapshot) or binding.get('datenbank') != str(Path(archiver.DB_PATH).resolve()):
        raise ValueError('Backup gehört nicht zur Datenbank oder Prüfsumme ungültig')
    description = json.loads((backup / 'costs.json').read_text(encoding='utf-8'))
    if description.get('format') not in ('quarter_hour_costs_backup_v1', 'quarter_hour_costs_backup_v2'):
        raise ValueError('Unbekanntes Kostenbackup')
    conn = sqlite3.connect(Path(archiver.DB_PATH).resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
    source = archiver._readonly_database(snapshot)
    try:
        if source.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Backupintegrität ungültig')
        pending = []
        def restored_slots():
            if description['format'] == 'quarter_hour_costs_backup_v1':
                yield from description['slots']
            else:
                for path in sorted(backup.glob('costs-????-??-??.json')):
                    yield from json.loads(path.read_text(encoding='utf-8'))['slots']
        for slot in restored_slots():
            stamp = slot['ts_start']
            if exists(source) and source.execute('SELECT 1 FROM quarter_hour_costs WHERE ts_start=?', (stamp,)).fetchone():
                raise ValueError('Rückweg würde einen vorhandenen Ausgangswert löschen')
            current = conn.execute('SELECT payload FROM quarter_hour_costs WHERE ts_start=?', (stamp,)).fetchone() if exists(conn) else None
            if current is None:
                continue
            if json.loads(current[0]) != slot:
                raise ValueError('Kostenwert inzwischen verändert')
            pending.append((stamp, current[0]))
        conn.execute('BEGIN IMMEDIATE')
        for stamp, payload in pending:
            current = conn.execute('SELECT payload FROM quarter_hour_costs WHERE ts_start=?', (stamp,)).fetchone()
            if current != (payload,):
                raise ValueError('Kostenbestand inzwischen verändert')
        for stamp, _payload in pending:
            conn.execute('DELETE FROM quarter_hour_costs WHERE ts_start=?', (stamp,))
        if pending:
            _ensure_aggregates(conn)
            _rebuild_aggregates(conn)
        conn.commit()
        return {'wiederhergestellt': len(pending)}
    finally:
        source.close(); conn.close()

AGGREGATES = 'cost_aggregates'
AMOUNT_FIELDS = ('grid_cost_eur', 'feedin_revenue_eur', 'net_cost_eur', 'home_cost_eur', 'dv_fixed_fee_eur')


def merge_aggregate(total, slot):
    """Additive Kostenwerte; fehlende Beträge bleiben fehlend wie im Webpfad."""
    n = slot.get('quarters', 1)
    amounts = {}
    for field in AMOUNT_FIELDS:
        value = slot.get(field)
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        part = slot.get('amounts', {}).get(field, {
            'sum_eur': value if valid else None, 'valid_quarters': n if valid else 0,
            'missing_quarters': 0 if valid else n})
        previous = (total or {}).get('amounts', {}).get(field)
        if previous is None:
            amounts[field] = dict(part)
        else:
            count = previous['valid_quarters'] + part['valid_quarters']
            amounts[field] = {'sum_eur': ((previous['sum_eur'] or 0) + (part['sum_eur'] or 0)) if count else None,
                             'valid_quarters': count,
                             'missing_quarters': previous['missing_quarters'] + part['missing_quarters']}
    first, last = slot.get('first_ts', slot['ts_start']), slot.get('last_ts', slot['ts_start'])
    if total is None:
        result = dict(slot)
        result.pop('prices', None)
        result.update(quarters=n, first_ts=first, last_ts=last, sealed=slot.get('sealed', True),
                      amounts=amounts, amount_schema=2)
        return result
    for key, value in slot.items():
        if key.endswith('_eur') or (key.endswith('_kwh') and key != 'battery_energy_kwh'):
            total[key] = None if total.get(key) is None or value is None else total[key] + value
    total['amounts'] = amounts
    total['amount_schema'] = 2
    total['sealed'] = bool(total.get('sealed', True)) and bool(slot.get('sealed', True))
    total['quarters'] += n
    total['first_ts'] = min(total['first_ts'], first)
    if last >= total['last_ts']:
        for key in ('state', 'battery_energy_kwh', 'battery_mean_ct', 'battery_estimated_share'):
            total[key] = slot.get(key)
    total['last_ts'] = max(total['last_ts'], last)
    for key in ('complete', 'energy_valid'):
        total[key] = bool(total.get(key)) and bool(slot.get(key))
    total['battery_estimated'] = bool(total.get('battery_estimated')) or bool(slot.get('battery_estimated'))
    for key in ('reasons', 'home_includes'):
        total[key] = sorted(set(total.get(key, ())) | set(slot.get(key, ())))
    for key in ('billing_price_ct', 'feedin_price_ct', 'dv_market_price_ct', 'dv_net_sell_ct'):
        total[key] = None
    return total


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _save_aggregate(conn, key, payload):
    conn.execute('INSERT INTO cost_aggregates(key,payload) VALUES (?,?) '
                 'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload', (key, _json(payload)))


def _rebuild_aggregates(conn):
    """Altschema einmal streamend übernehmen, danach nur neue Viertelstunden addieren."""
    conn.execute('DELETE FROM cost_aggregates')
    total = day_sum = None
    day = None
    for payload, in conn.execute('SELECT payload FROM quarter_hour_costs ORDER BY ts_start'):
        slot = json.loads(payload)
        if slot['date'] != day:
            if day_sum is not None:
                _save_aggregate(conn, 'day:' + day, day_sum)
            day, day_sum = slot['date'], None
        day_sum = merge_aggregate(day_sum, slot)
        total = merge_aggregate(total, slot)
    if day_sum is not None:
        _save_aggregate(conn, 'day:' + day, day_sum)
    if total is not None:
        _save_aggregate(conn, 'total', total)


def _ensure_aggregates(conn, *, verify_count=False):
    ready = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_aggregates'").fetchone()
    conn.execute('CREATE TABLE IF NOT EXISTS cost_row_count(id INTEGER PRIMARY KEY CHECK(id=1),n INTEGER NOT NULL)')
    conn.execute('CREATE TRIGGER IF NOT EXISTS cost_count_insert AFTER INSERT ON quarter_hour_costs '
                 'BEGIN UPDATE cost_row_count SET n=n+1 WHERE id=1; END')
    conn.execute('CREATE TRIGGER IF NOT EXISTS cost_count_delete AFTER DELETE ON quarter_hour_costs '
                 'BEGIN UPDATE cost_row_count SET n=n-1 WHERE id=1; END')
    if verify_count or conn.execute('SELECT n FROM cost_row_count WHERE id=1').fetchone() is None:
        conn.execute('INSERT INTO cost_row_count(id,n) SELECT 1,COUNT(*) FROM quarter_hour_costs WHERE 1 '
                     'ON CONFLICT(id) DO UPDATE SET n=excluded.n')
    if ready is None:
        conn.execute('CREATE TABLE cost_aggregates(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    cached = conn.execute("SELECT payload FROM cost_aggregates WHERE key='total'").fetchone()
    total = json.loads(cached[0]) if cached else None
    count = conn.execute('SELECT n FROM cost_row_count WHERE id=1').fetchone()[0]
    first = conn.execute('SELECT MIN(ts_start) FROM quarter_hour_costs').fetchone()[0]
    last = conn.execute('SELECT MAX(ts_start) FROM quarter_hour_costs').fetchone()[0]
    if ready is None or (total is None and count) or (total is not None and (
            total.get('amount_schema') != 2 or total.get('quarters') != count or
            total.get('first_ts') != first or total.get('last_ts') != last)):
        _rebuild_aggregates(conn)

def _extend_aggregates(conn, slots):
    for slot in slots:
        for key in ('day:' + slot['date'], 'total'):
            previous = conn.execute('SELECT payload FROM cost_aggregates WHERE key=?', (key,)).fetchone()
            total = json.loads(previous[0]) if previous else None
            _save_aggregate(conn, key, merge_aggregate(total, slot))


def _low_priority():
    """Nur der Rechenprozess erhält niedrige Priorität; der Archivprozess bleibt unverändert."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    ionice = shutil.which('ionice')
    if ionice:
        try:
            subprocess.run([ionice, '-c', '3', '-p', str(os.getpid())],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass


def _save_backfill(conn, state):
    conn.execute('INSERT INTO cost_backfill_state(id,payload) VALUES (1,?) '
                 'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (_json(state),))


def _backfill_state(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cost_backfill_state'").fetchone():
        row = conn.execute('SELECT payload FROM cost_backfill_state WHERE id=1').fetchone()
        if row:
            return json.loads(row[0])
    return None


def _write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(_json(value) + '\n')
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transition_gap(previous, existing):
    if previous['ts_start'] + 900 != existing['ts_start']:
        return True
    # Der gespeicherte Anschlusswert beschreibt das Ende der ersten bestehenden Viertelstunde.
    a = previous.get('state', {})
    b = existing.get('state', {})
    values = (a.get('energy_kwh'), b.get('energy_kwh'), existing.get('bat_in_kwh'), existing.get('bat_out_kwh'))
    if any(value is None for value in values):
        return True
    energy, final, charged, discharged = values
    if abs(energy + charged - discharged - final) > 0.01:
        return True
    if charged <= 1e-9:
        return a.get('mean_ct') != b.get('mean_ct')
    buy, sell = existing.get('billing_price_ct'), existing.get('feedin_price_ct')
    pv, grid = existing.get('pv_to_bat_kwh'), existing.get('grid_to_bat_kwh')
    if any(value is None for value in (buy, sell, pv, grid, a.get('mean_ct'), b.get('mean_ct'))):
        return True
    mixed = (energy * a['mean_ct'] + pv * sell + grid * buy) / (energy + charged) if energy + charged > 1e-9 else None
    return mixed is None or abs(mixed - b['mean_ct']) > 0.01


def automatic_backfill(archiver, conn, now=None):
    """Einmalig, maximal ein Kalendertag und 60 Sekunden pro regulärem Archivlauf."""
    root = Path(archiver.DB_DIR).parent
    config = config_for(root)
    if not enabled(config):
        return None
    state = _backfill_state(conn)
    failure_path = root / 'data/cost_backfill_failure.json'
    if state is None and failure_path.is_file():
        state = json.loads(failure_path.read_text(encoding='utf-8'))
        if state.get('status') == 'failed':
            return state
    if state is not None and state.get('status') in ('completed', 'failed'):
        _expire_cost_backup(root, state, int(time.time() if now is None else now))
        return state
    deadline = time.monotonic() + 60
    now = int(time.time() if now is None else now)
    zone = ZoneInfo('Europe/Berlin')
    try:
        expected_state = _backfill_state(conn)
        if state is None or not state.get('next_date'):
            failures = (state or {}).get('failures', 0)
            today = datetime.datetime.fromtimestamp(now, zone).date()
            oldest = today - datetime.timedelta(days=30)
            available = []
            for path in (root / 'data/history_backups').glob('history_????-??-??.txt'):
                try:
                    day = datetime.date.fromisoformat(path.stem[8:])
                    if oldest <= day < today:
                        available.append(day)
                except ValueError:
                    continue
            first = min(available) if available else oldest
            state = {'format': 'cost_backfill_v1', 'status': 'running', 'backup': (state or {}).get('backup'),
                     'next_date': first.isoformat(), 'end_date': today.isoformat(), 'processed': 0,
                     'total': (today - first).days, 'added_days': 0, 'missing_days': 0,
                     'inserted_quarters': 0, 'failures': failures, 'unrecoverable_days': [], 'battery_transition_gap': False}
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('CREATE TABLE IF NOT EXISTS cost_backfill_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)')
            _save_backfill(conn, state)
            conn.commit()
            expected_state = dict(state)
        day = datetime.date.fromisoformat(state['next_date'])
        if day >= datetime.date.fromisoformat(state['end_date']):
            return state
        start = int(datetime.datetime.combine(day, datetime.time(), zone).timestamp())
        next_day = day + datetime.timedelta(days=1)
        end = int(datetime.datetime.combine(next_day, datetime.time(), zone).timestamp())
        # Nur dieser Tag und die Randproben der Nachbarn, niemals der ganze Rückblick.
        paths = [root / 'data/history_backups' / ('history_' + date.isoformat() + '.txt')
                 for date in (day - datetime.timedelta(days=1), day, next_day)]
        rows = read_history(paths + [root / 'ramdisk/live_history.txt'], after=start-120, before=end+120, deadline=deadline)
        starts = list(range(start, end, 900))
        if time.monotonic() >= deadline:
            raise TimeoutError('Zeitgrenze der Nachberechnung erreicht')
        slots = calculate(conn, root, rows, starts, config, bootstrap=False,
                          timeout=max(0.01, deadline-time.monotonic()), low_priority=True) if rows else []
        usable = any(slot.get('energy_valid') for slot in slots)
        slots = [slot for slot in slots if slot.get('coverage_s', 0) > 0] if usable else []
        # Der Rückweg wird vor dem Schreiben dauerhaft hinterlegt.
        if slots:
            if not state.get('backup'):
                backup = archiver._verified_statistics_backup([], cost_only=True, deadline=deadline)
                state['backup'] = str(backup)
                # Der Rückweg bleibt auch nach einem Abbruch vor dem ersten INSERT bekannt.
                conn.execute('BEGIN IMMEDIATE')
                if _backfill_state(conn) != expected_state:
                    raise RuntimeError('Nachberechnung inzwischen fortgesetzt')
                _save_backfill(conn, state)
                conn.commit()
                expected_state = dict(state)
            _write_json(Path(state['backup']) / ('costs-' + day.isoformat() + '.json'), {'slots': slots})
        if time.monotonic() >= deadline:
            raise TimeoutError('Zeitgrenze der Nachberechnung erreicht')
        conn.execute('BEGIN IMMEDIATE')
        # Checkpoint und Ergänzungen sind gemeinsam atomar, auch nach SIGKILL.
        current = _backfill_state(conn)
        if current != expected_state:
            raise RuntimeError('Nachberechnung inzwischen fortgesetzt')
        ensure_schema(conn)
        _ensure_aggregates(conn)
        for slot in slots:
            if time.monotonic() >= deadline:
                raise TimeoutError('Zeitgrenze der Nachberechnung erreicht')
            insert(conn, [slot])
        if slots:
            for slot in slots:
                following = conn.execute('SELECT payload FROM quarter_hour_costs WHERE ts_start>? ORDER BY ts_start LIMIT 1', (slot['ts_start'],)).fetchone()
                if following:
                    saved = json.loads(following[0])
                    if saved['ts_start'] not in {item['ts_start'] for item in slots} and _transition_gap(slot, saved):
                        state['battery_transition_gap'] = True
        state['failures'] = 0
        if failure_path.exists():
            failure_path.unlink()
        state['processed'] += 1
        state['added_days'] += int(bool(slots))
        missing = not rows or (not usable and not conn.execute(
            'SELECT 1 FROM quarter_hour_costs WHERE ts_start>=? AND ts_start<? LIMIT 1', (start, end)).fetchone())
        if missing:
            state['missing_days'] += 1
            state.setdefault('unrecoverable_days', []).append({'date': day.isoformat(), 'reason': 'nicht nachberechenbar: keine verwertbaren Minutenwerte'})
        state['inserted_quarters'] += len(slots)
        state['next_date'] = next_day.isoformat()
        if state['next_date'] >= state['end_date']:
            state.update(status='completed', completed_at=now)
        _save_backfill(conn, state)
        if time.monotonic() >= deadline:
            raise TimeoutError('Zeitgrenze der Nachberechnung erreicht')
        conn.commit()
    except Exception as exc:
        conn.rollback()
        # Nie einen zurückgerollten Tagesfortschritt aus dem Python-Objekt übernehmen.
        persisted = _backfill_state(conn)
        state = persisted or {'format': 'cost_backfill_v1', 'status': 'running',
                              'failures': (state or {}).get('failures', 0), 'processed': 0, 'total': 30}
        state['failures'] += 1
        state['last_error'] = type(exc).__name__
        state['reason'] = ('Die Sicherung der Statistikdatenbank ist fehlgeschlagen.' if not state.get('backup')
                           else 'Die Berechnung konnte nach drei Versuchen nicht abgeschlossen werden.')
        if state['failures'] >= 3:
            state['status'] = 'failed'
        if persisted is None:
            _write_json(failure_path, state)
        else:
            _save_backfill(conn, state)
            conn.commit()
        print('  [WARN] Nachberechnung verschoben: ' + type(exc).__name__)
    return state
