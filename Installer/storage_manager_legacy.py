"""
E3DC Storage Manager - Eba-M Klon
1:1 Uebersetzung RscpExampleMain.cpp Kern-Algorithmus.
Variablen behalten Eba-Namen. Kommentare zeigen Zeilen im Original.
"""
import os, sys, json, time, math, signal, logging, datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from rscp_client import RscpConnection, RscpTag, RscpType
from Wallbox.modes import MODE_OFF, MODE_PRICE, MODE_TARGET, normalize_wb_mode, storage_floor_mode
from consumer_priority import (
    allocate_consumer_budget,
    priority_order_from_config,
    priority_order_key,
    priority_runon_s_from_config,
)
try:
    from storage_parallel_regulator import emit_parallel_decision
except Exception:
    emit_parallel_decision = None

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - StorageManager - %(levelname)s - %(message)s',
    datefmt='%d.%m %H:%M:%S')
log = logging.getLogger('StorageManager')

RAMDISK  = '/var/www/html/ramdisk'
DATA_DIR = '/var/www/html/data'
V4_CFG   = os.path.join(DATA_DIR,  'e3dc_v4.json')
LIVE_F   = os.path.join(RAMDISK,   'live_data_py.json')
PLAN_F   = os.path.join(RAMDISK,   'storage_plan.json')
STATE_F  = os.path.join(RAMDISK,   'storage_manager_state.json')
WB_F     = os.path.join(RAMDISK,   'wb_pv_budget.json')
LIVE_HISTORY_F = os.path.join(RAMDISK, 'live_history.txt')
WB_NATIVE_F = os.path.join(RAMDISK, 'wallbox_native.json')
WB_INTENT_F = os.path.join(RAMDISK, 'wallbox_storage_intent.json')
OPENWB_F = os.path.join(RAMDISK, 'openwb_data.json')
PRICE_BOOST_F = os.path.join(RAMDISK, 'price_boost_plan.json')
PREDUMP_PLAN_F = os.path.join(RAMDISK, 'predump_consumer_plan.json')

CYCLE_S   = 5
MODE_AUTO = 0; MODE_IDLE = 1; MODE_DISCH = 2; MODE_CHRG = 3; MODE_GRID = 4

_stop = False
def _sig(s,_):
    global _stop; _stop=True; log.info('Signal %d - beende.' % s)
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT,  _sig)

_last_status_log = {}
_last_status_key = None
def log_status_throttled(key, message, interval_s=900):
    """Loggt Dauerzustaende pro Key beim ersten Auftreten und danach gedrosselt."""
    global _last_status_key
    now = time.time()
    last = _last_status_log.get(key)
    if not last or now - float(last.get('ts', 0)) >= interval_s:
        log.info(message)
        _last_status_log[key] = {'ts': now, 'message': message}
    _last_status_key = key

def gf(d,k,v=0.0):
    try:
        x=d.get(k)
        if x is None or str(x).strip() in ('','None','null'): return float(v)
        return float(str(x).strip().replace(',','.'))
    except: return float(v)

def cfg_bool(d, k, default=False):
    try:
        x = d.get(k, default)
        return str(x).strip().lower() in ('1', 'true', 'yes', 'on')
    except Exception:
        return bool(default)

def wb_target_curve_soc(cfg, intent, intent_fresh, car_active, mode, default_target_soc):
    """Speicherziel fuer Modus Ziel wbminSoC: Kurve endet bei wbminSoC."""
    try:
        if not intent_fresh or not car_active:
            return None
        if normalize_wb_mode(mode) != MODE_TARGET:
            return None
        raw = intent.get('effective_wb_floor_soc', intent.get('wbminsoc', cfg.get('wbminsoc', default_target_soc)))
        target = gf({'v': raw}, 'v', default_target_soc)
        return max(5.0, min(100.0, float(target)))
    except Exception:
        return None

def read_eco_score():
    """Liest aktuellen Eco-Score aus eco_score.json. Return: 0-100, default 50."""
    try:
        eco_f = os.path.join(RAMDISK, 'eco_score.json')
        if not os.path.exists(eco_f): return 50.0
        slots = json.load(open(eco_f, 'r', encoding='utf-8'))
        now_ms = time.time() * 1000
        for slot in slots:
            if slot.get('start_timestamp', 0) <= now_ms < slot.get('end_timestamp', 0):
                return float(slot.get('optimization_score', 50.0))
    except: pass
    return 50.0

def load_cfg():
    try:
        with open(V4_CFG,'r',encoding='utf-8') as f:
            raw = json.load(f)
        cfg = {}
        for k, v in raw.items():
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    cfg[str(sub_k).lower()] = sub_v
            else:
                cfg[str(k).lower()] = v
        return cfg
    except: return {}

def read_live():
    try:
        with open(LIVE_F,'r',encoding='utf-8') as f: return json.load(f)
    except: return {}

def read_json_file(path, max_age_s=30):
    try:
        if time.time() - os.path.getmtime(path) > max_age_s:
            return {}
        with open(path,'r',encoding='utf-8') as f: return json.load(f)
    except: return {}

def price_boost_window_active(plan):
    try:
        if not (plan.get('enabled') and plan.get('active')):
            return False
        win = plan.get('active_window') or {}
        now_ms = int(time.time() * 1000)
        start_ms = int(win.get('start_timestamp', 0) or 0)
        end_ms = int(win.get('end_timestamp', 0) or 0)
        return start_ms <= now_ms < end_ms
    except Exception:
        return False

def price_boost_allows_battery():
    try:
        d = read_json_file(PRICE_BOOST_F, max_age_s=1800)
        return bool(price_boost_window_active(d) and (d.get('allow') or {}).get('battery'))
    except Exception:
        return False

def price_boost_is_active():
    try:
        d = read_json_file(PRICE_BOOST_F, max_age_s=1800)
        return price_boost_window_active(d)
    except Exception:
        return False

def cheap_grid_charge_active(cheap_grid_charge):
    try:
        if not cheap_grid_charge.get('active'):
            return False
        win = cheap_grid_charge.get('active_window') or {}
        now_ms = int(time.time() * 1000)
        start_ms = int(win.get('start_timestamp', 0) or 0)
        end_ms = int(win.get('end_timestamp', cheap_grid_charge.get('window_end', 0)) or 0)
        return start_ms <= now_ms < end_ms
    except Exception:
        return False

def write_state(d):
    """Schreibt Manager-State und fuegt automatisch Ladekurve-Meilensteine hinzu."""
    d['ts']=int(time.time()); d['service']='storage_manager'
    # Ladekurve (3 Meilensteine) immer frisch aus Plan berechnen
    d['ladekurve'] = _build_ladekurve()
    try:
        tmp=STATE_F+'.tmp'
        with open(tmp,'w',encoding='utf-8') as f: json.dump(d,f,indent=2)
        os.replace(tmp,STATE_F)
    except: pass
    try:
        if emit_parallel_decision:
            emit_parallel_decision(d)
    except Exception:
        pass

def write_wb_budget(d):
    """Schreibt das frische Budget-Signal fuer Wallbox/WP atomar."""
    try:
        d['ts'] = int(time.time())
        if 'budget_w' in d:
            budget_w = max(0, int(float(d.get('budget_w', 0) or 0)))
            d['budget_w'] = budget_w
            d.setdefault('budget_amp_1ph', max(6, min(32, int(budget_w / 230))) if budget_w >= 6 * 230 else 0)
            d.setdefault('budget_amp_3ph', max(6, min(32, int(budget_w / 690))) if budget_w >= 6 * 690 else 0)
        tmp = WB_F + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        os.replace(tmp, WB_F)
    except Exception:
        pass

def write_predump_consumer_plan(active, allow=None, budget_w=0, discharge_w=0, reason='', target_soc=None, no_grid=True):
    """Gibt Pre-Dump-Leistung gezielt fuer Hausverbraucher frei.

    Der Speicher bleibt die einzige Stelle, die EMS-Befehle sendet. Andere
    Manager duerfen nur dieses frische Plan-Signal lesen und ihre Geraete
    innerhalb des Budgets nutzen.
    """
    try:
        now = int(time.time())
        payload = {
            'enabled': bool(active),
            'active': bool(active),
            'state': 'pre_discharge' if active else 'idle',
            'ts': now,
            'expires_ts': now + max(20, int(CYCLE_S * 4)),
            'allow': allow or {'wallbox': False, 'heatpump': False, 'heater': False},
            'budget_w': max(0, int(float(budget_w or 0))),
            'discharge_w': max(0, int(float(discharge_w or 0))),
            'no_grid': bool(no_grid),
            'reason': str(reason or '')[:160],
        }
        if target_soc is not None:
            payload['target_soc'] = round(float(target_soc), 1)
            payload['floor_soc'] = round(float(target_soc), 1)
        tmp = PREDUMP_PLAN_F + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, PREDUMP_PLAN_F)
    except Exception:
        pass

def _build_ladekurve():
    """Baut Ladekurven-Meilensteine aus storage_plan.json fuer das Frontend.
    Erwartet vom PHP/JS:
      ladestart: {t:'HH:MM', soc:40.0}  <- storage_morning_soc + Ladestart-Zeit
      peak:      {t:'HH:MM', soc:95.0, pv_kw:8.5, past:False} <- PV-Peak
      freilauf:  {t:'HH:MM', soc:92.0}  <- letzter Punkt des Tages
    """
    try:
        with open(PLAN_F,'r',encoding='utf-8') as f:
            plan = json.load(f)
    except:
        return None

    now_ms  = time.time() * 1000
    today0  = datetime.datetime.now().replace(hour=0,minute=0,second=0,microsecond=0)
    today0_ms = today0.timestamp() * 1000
    day_ms = 86400000
    today1_ms = today0_ms + day_ms

    # --- Anzeige-Tag bestimmen ------------------------------------------------
    # Nach Sonnenuntergang darf die Kachel nicht den letzten Daemmerungs-Slot als
    # "PV-Peak" verkaufen. Wir waehlen deshalb den naechsten Tag mit echter
    # PV-Prognose, sobald fuer heute keine relevante PV-Leistung mehr kommt.
    tl_all = plan.get('timeline') or plan.get('target_timeline') or []
    days = []
    for offset in (0, 1):
        start = today0_ms + offset * day_ms
        end = start + day_ms
        slots = [s for s in tl_all if start <= float(s.get('ts', 0)) < end]
        if not slots:
            continue
        future_slots = [s for s in slots if float(s.get('ts', 0)) >= now_ms - 15 * 60000]
        max_pv = max((float(s.get('pv_w', 0) or 0) for s in slots), default=0.0)
        max_future_pv = max((float(s.get('pv_w', 0) or 0) for s in future_slots), default=0.0)
        last_pv_ts = max((float(s.get('ts', 0)) for s in slots if float(s.get('pv_w', 0) or 0) > 500), default=0.0)
        days.append({
            'start': start,
            'end': end,
            'offset': offset,
            'slots': slots,
            'max_pv': max_pv,
            'max_future_pv': max_future_pv,
            'last_pv_ts': last_pv_ts,
        })

    display = next((d for d in days if d['offset'] == 0), None)
    today_done = bool(display) and display['max_future_pv'] < 500 and now_ms > (display['last_pv_ts'] + 30 * 60000)
    if today_done:
        tomorrow = next((d for d in days if d['offset'] == 1 and d['max_pv'] > 500), None)
        if tomorrow:
            display = tomorrow
    if not display and days:
        display = days[0]
    display_start_ms = display['start'] if display else today0_ms
    display_end_ms = display['end'] if display else today1_ms
    display_offset = int(display['offset']) if display else 0
    display_label = 'Morgen' if display_offset == 1 else 'Heute'

    def _actual_pv_peak_from_history(day_start_ms, day_end_ms):
        """Findet den gemessenen Tagespeak aus live_history.txt.

        Die Forecast-Datei enthaelt fuer den laufenden Tag oft nur noch den
        Rest des Tages. Ohne diesen Ist-Peak wuerde die Kachel den Peak
        alle 15 Minuten auf den naechsten Forecast-Slot verschieben.
        """
        best = None
        try:
            if not os.path.exists(LIVE_HISTORY_F):
                return None
            with open(LIVE_HISTORY_F, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    try:
                        h = json.loads(line)
                    except Exception:
                        continue
                    raw_ts = h.get('ts')
                    ts_ms = None
                    if isinstance(raw_ts, (int, float)):
                        ts_ms = float(raw_ts)
                        if ts_ms < 100000000000:
                            ts_ms *= 1000
                    elif isinstance(raw_ts, str) and raw_ts:
                        try:
                            ts_ms = datetime.datetime.fromisoformat(raw_ts.replace('Z', '+00:00')).timestamp() * 1000
                        except Exception:
                            ts_ms = None
                    if ts_ms is None or not (day_start_ms <= ts_ms < day_end_ms):
                        continue
                    pv = float(h.get('pv', 0) or 0)
                    if pv <= 0:
                        continue
                    if best is None or pv > best['pv_w']:
                        best = {
                            'ts': ts_ms,
                            'pv_w': pv,
                            'soc': float(h.get('soc', 0) or 0),
                        }
        except Exception:
            return None
        return best

    # --- Ladestart: aus ladestart_ts + ladestart_soc ---
    lk_ladestart = None
    try:
        _ls  = plan.get('ladestart_ts')   # ms
        _soc = plan.get('ladestart_soc')
        if _ls and _soc is not None:
            _lts = float(_ls)
            while _lts < display_start_ms:
                _lts += day_ms
            if display_offset > 0:
                # Fuer Folgetage hat die echte Sollkurve Vorrang vor der
                # freien Physik-Simulation. Sonst zeigt die Kachel am
                # Morgenanker einen Roh-SoC, obwohl der Storage Manager nach
                # target_timeline regelt.
                _ladestart_source = plan.get('target_timeline') or tl_all
                _forecast_slot = next(
                    (s for s in sorted(_ladestart_source, key=lambda x: float(x.get('ts', 0)))
                     if _lts <= float(s.get('ts', 0)) < display_end_ms
                     and float(s.get('soc', -1)) >= 0),
                    None
                )
                if _forecast_slot is not None:
                    _soc = float(_forecast_slot.get('soc', _soc))
            _dt = datetime.datetime.fromtimestamp(_lts / 1000)
            lk_ladestart = {
                't': _dt.strftime('%H:%M'),
                'soc': round(float(_soc), 1),
                'past': _lts < now_ms,
                'forecast': bool(display_offset > 0)
            }
    except: pass

    # --- target_timeline oder timeline (Fallback) ---
    # WICHTIG: target_timeline hat nur {ts, soc} - KEIN pv_w!
    # Fuer PV-Peak und Freilauf nutzen wir die volle timeline (3-Tage-Simulation)
    # die auch pv_w, home_w, surplus_w enthaelt.
    tl_target = plan.get('target_timeline') or []  # Nur fuer SOC-Punkte
    tl = plan.get('timeline') or []                # Fuer PV-Daten (Peak, Freilauf)
    if not tl:
        tl = tl_target  # Absoluter Fallback
    tl_day = [s for s in tl if display_start_ms <= float(s.get('ts',0)) < display_end_ms]
    tl_target_day = [s for s in tl_target if display_start_ms <= float(s.get('ts',0)) < display_end_ms]
    if lk_ladestart:
        lk_ladestart['forecast'] = bool(display_offset > 0 and not tl_target_day)

    # PV-Peak: Slot mit maximalem pv_w im angezeigten Tag.
    lk_peak = None
    if tl_day:
        peak_slot = max(tl_day, key=lambda s: float(s.get('pv_w', 0)))
        if float(peak_slot.get('pv_w', 0)) > 500:
            _ts = float(peak_slot['ts'])
            _pdt = datetime.datetime.fromtimestamp(_ts / 1000)

            # SOC aus der geregelten target_timeline holen (sicheres Ziel)
            _target_soc = next((float(s.get('soc', 0)) for s in tl_target_day if abs(float(s.get('ts', 0)) - _ts) < 1000), None)
            if _target_soc is None:
                _target_soc = float(peak_slot.get('soc', 0))

            lk_peak = {
                't':      _pdt.strftime('%H:%M'),
                'soc':    round(_target_soc, 1),
                'pv_kw':  round(float(peak_slot.get('pv_w', 0)) / 1000, 1),
                'past':   _ts < now_ms
            }
            if display_offset == 0:
                actual_peak = _actual_pv_peak_from_history(display_start_ms, min(display_end_ms, now_ms + 60000))
                if actual_peak and actual_peak['pv_w'] > 500:
                    # Wenn die Restprognose nur noch knapp vor uns liegt oder der
                    # Ist-Peak schon fast gleich gross ist, zeige den stabilen
                    # Tagespeak statt eines wandernden Forecast-Rests.
                    forecast_pv = float(peak_slot.get('pv_w', 0))
                    if actual_peak['pv_w'] >= forecast_pv * 0.80 or _ts <= now_ms + 60 * 60000:
                        _adt = datetime.datetime.fromtimestamp(actual_peak['ts'] / 1000)
                        lk_peak = {
                            't':     _adt.strftime('%H:%M'),
                            'soc':   round(actual_peak.get('soc', 0.0), 1),
                            'pv_kw': round(actual_peak['pv_w'] / 1000, 1),
                            'past':  True,
                            'source': 'live_history'
                        }

    # Freilauf = tLadezeitende (Eba): geplanter Zeitpunkt, ab dem
    # der E3DC wieder in AUTO losgelassen wird.
    # Das ist das ZIEL (target_soc), kein Istwert aus der Simulation.
    # Wenn der Simulator ein explizites Ladeende liefert, hat dieses Vorrang.
    lk_freilauf = None
    target_soc_full = float(plan.get('ladeende_soc',
                            plan.get('effective_target_soc',
                            plan.get('target_soc', 95.0))) or 95.0)
    if display_offset > 0 and not tl_target_day and tl_day:
        # Morgen-Vorschau ohne eingefrorene Sollkurve: hier gibt es noch kein
        # echtes Freilauf-Ziel. Zeige deshalb den prognostizierten erreichbaren
        # SoC statt das Wunsch-Tagesziel als vermeintlichen Sollwert.
        target_soc_full = max(
            float(s.get('soc', 0) or 0)
            for s in tl_day
        )
    LADEZEITENDE_OFFSET_MS = 90 * 60 * 1000
    try:
        _le_ts = plan.get('ladeende_ts') or (plan.get('target_curve_meta') or {}).get('curve_end_ts')
        if _le_ts:
            _le_ts = float(_le_ts)
            if display_start_ms <= _le_ts < display_end_ms:
                _fdt = datetime.datetime.fromtimestamp(_le_ts / 1000)
                lk_freilauf = {
                    't':      _fdt.strftime('%H:%M'),
                    'soc':    round(target_soc_full, 1),
                    'past':   _le_ts < now_ms
                }
    except: pass
    if tl_day:
        # 1. Finde LETZTEN Slot mit PV-Ueberschuss (Abend-Crossover: PV faellt unter Home)
        # surplus_w = pv_w - home_w - wp_w (bereits im Plan berechnet)
        # Schwelle: pv > 500W (Tageslicht) UND Ueberschuss > 200W
        # LETZTER solcher Slot = wenn PV abends unter Hausverbrauch faellt
        # Freilauf = dieser Zeitpunkt - 1.5h (z.B. 19:30 - 1.5h = 18:00)
        pv_ge_home_ts = None
        for _s in sorted(tl_day, key=lambda s: float(s.get('ts', 0)), reverse=False):
            _pv  = float(_s.get('pv_w', 0))
            _sur = float(_s.get('surplus_w', _pv - float(_s.get('home_w',0)) - float(_s.get('wp_w',0))))
            if _pv > 500 and _sur > 200:
                pv_ge_home_ts = float(_s['ts'])  # Wird immer weiter verschoben -> bleibt am letzten
        # Jetzt ist pv_ge_home_ts = letzter Surplus-Slot (abends)

        if pv_ge_home_ts and lk_freilauf is None:
            # 2. Freilauf = 1.5h VOR diesem Zeitpunkt
            freilauf_ts_ms = pv_ge_home_ts - LADEZEITENDE_OFFSET_MS
            _fdt = datetime.datetime.fromtimestamp(freilauf_ts_ms / 1000)
            lk_freilauf = {
                't':      _fdt.strftime('%H:%M'),
                'soc':    round(target_soc_full, 1),   # ZIEL, nicht Istwert
                'past':   freilauf_ts_ms / 1000 < now_ms / 1000
            }
        elif lk_freilauf is None:
            # Fallback: kein PV>Home bekannt -> Ende des PV-Fensters (letzter Slot mit pv>100W)
            pv_slots = sorted([s for s in tl_day if float(s.get('pv_w',0)) > 100],
                              key=lambda s: float(s.get('ts',0)))
            if pv_slots:
                _fs = pv_slots[-1]
                _fdt = datetime.datetime.fromtimestamp(float(_fs['ts']) / 1000)
                lk_freilauf = {
                    't':    _fdt.strftime('%H:%M'),
                    'soc':  round(target_soc_full, 1),
                    'past': float(_fs['ts']) < now_ms
                }

    if not lk_ladestart and not lk_peak and not lk_freilauf:
        return None
    return {
        'day_label': display_label,
        'day_offset': display_offset,
        'day_start_ts': int(display_start_ms),
        'date': datetime.datetime.fromtimestamp(display_start_ms / 1000).strftime('%Y-%m-%d'),
        'has_target_curve': bool(tl_target_day),
        'ladestart': lk_ladestart,
        'peak': lk_peak,
        'freilauf': lk_freilauf
    }

def solar_times(lat,lon,date):
    doy=date.timetuple().tm_yday; B=2*math.pi*(doy-81)/364
    decl=math.radians(23.45*math.sin(B))
    eot=9.87*math.sin(2*B)-7.53*math.cos(B)-1.5*math.sin(B)
    lat_r=math.radians(lat)
    cos_ha=max(-0.9999,min(0.9999,-math.tan(lat_r)*math.tan(decl)))
    ha=math.degrees(math.acos(cos_ha))
    utc=datetime.datetime.now(datetime.timezone.utc).astimezone().utcoffset().seconds/3600
    noon=12-(eot/60)-(lon-15*utc)/15
    return noon-ha/15, noon+ha/15

def get_ladeende(cfg,now_h,sunrise_h,sunset_h):
    """tLadezeitende und fLadeende aus Plan oder Fallback."""
    target=gf(cfg,'storage_target_soc',95)
    end_h=max(sunrise_h+1,sunset_h-1.5)
    try:
        plan=json.load(open(PLAN_F,'r',encoding='utf-8'))
        # Das Config-Ziel bleibt die Basis. Nur ein explizites Plan-Ziel
        # (z.B. Schlechtwetterreserve) darf darueber liegen. Werte wie
        # effective_target_soc/max_reachable_soc sind Diagnose, kein neues
        # iFc-Regelziel.
        target=float(plan.get('planning_target_soc', plan.get('target_soc', target)) or target)
        now_date=datetime.datetime.now().date()
        ladeende_ts=plan.get('ladeende_ts') or (plan.get('target_curve_meta') or {}).get('curve_end_ts')
        if ladeende_ts:
            d=datetime.datetime.fromtimestamp(float(ladeende_ts)/1000)
            if d.date()==now_date:
                end_h=d.hour+d.minute/60+d.second/3600
                return end_h, target

        anchors=plan.get('curve_anchors',[])
        if anchors:
            pts=[]
            for s in anchors:
                d=datetime.datetime.fromtimestamp(float(s['ts'])/1000)
                if d.date()==now_date:
                    pts.append((d.hour+d.minute/60,float(s.get('soc',target))))
            pts.sort()
            if pts:
                end_h=pts[-1][0]
                target=pts[-1][1]
                return end_h, target

        tl=plan.get('target_timeline',[])
        if tl:
            pts=[]
            for s in tl:
                d=datetime.datetime.fromtimestamp(float(s['ts'])/1000)
                if d.date()==now_date:
                    pts.append((d.hour+d.minute/60,float(s.get('soc',target))))
            pts.sort()
            if pts: end_h=pts[-1][0]; target=pts[-1][1]
    except: pass
    return end_h, target

def get_tl_target(plan, t0, lookahead_h=2.0):
    """Liest Soll-SOC (jetzt + in lookahead_h Stunden) aus target_timeline.
    Gibt (soc_now, soc_target, ts_target) zurueck.
    soc_now    = interpolierter Soll-SoC fuer den aktuellen Zeitpunkt
    soc_target = Soll-SoC in lookahead_h Stunden (Eba iFc-Zwischenziel)
    ts_target  = Timestamp [s] des Zielpunkts
    Gibt (None, None, None) wenn keine target_timeline vorhanden oder leer.
    """
    tl = plan.get('target_timeline', [])
    if not tl:
        return None, None, None
    try:
        now_ms    = t0 * 1000.0
        target_ms = now_ms + lookahead_h * 3600.0 * 1000.0
        first_ts = float(tl[0].get('ts', 0))
        if first_ts and now_ms < first_ts:
            first_soc = float(tl[0].get('soc', 0))
            return first_soc, first_soc, first_ts / 1000.0
        soc_now    = None
        soc_target = None
        ts_target  = None
        prev = None
        for slot in tl:
            ts  = float(slot.get('ts', 0))
            soc = float(slot.get('soc', 0))
            if soc_now is None and ts >= now_ms:
                soc_now = soc
            if soc_target is None and ts >= target_ms:
                soc_target = soc
                ts_target  = ts / 1000.0
                break
            prev = slot
        # Fallback: Lookahead hinter letztem Kurven-Punkt -> letzter bekannter Wert
        if soc_target is None and prev:
            soc_target = float(prev.get('soc', 0))
            ts_target  = float(prev.get('ts', 0)) / 1000.0
        if soc_now is None and prev:
            soc_now = float(prev.get('soc', 0))
        return soc_now, soc_target, ts_target
    except:
        return None, None, None

# Eba Mode-Mapping (aus createRequestEMSData)
def eba_mode(iE3DC_Req_Load, maximumLadeleistung):
    val=int(iE3DC_Req_Load)
    # Eba/C++ createRequestEMSData(): Req_Load==0 wird als Mode 1 (IDLE) gesendet.
    if val==0:                            return MODE_IDLE, 0
    elif val>maximumLadeleistung:         return MODE_GRID, min(val-maximumLadeleistung, maximumLadeleistung)
    elif val==maximumLadeleistung:        return MODE_AUTO, maximumLadeleistung
    elif val>0:                           return MODE_CHRG, val
    else:                                 return MODE_DISCH, min(-val, maximumLadeleistung)

class BattCtrl:
    def __init__(self,host,port,user,pw,rscp_pw):
        self.host=host; self.port=port; self.user=user; self.pw=pw; self.rscp_pw=rscp_pw
        self._c=None; self._lm=-1; self._lv=-1; self._lcap=-1; self._ldcap=-1
    def _conn(self):
        try:
            self._c=RscpConnection(self.host,self.port,self.rscp_pw)
            self._c.connect(); self._c.authenticate(self.user,self.pw)
            log.info('RSCP: %s:%d' % (self.host,self.port)); return True
        except Exception as e:
            log.error('RSCP conn: %s'%e); self._c=None; return False
    def send(self,mode,val,force=False):
        if not self._c and not self._conn(): return

        # C++-konform: EMS_REQ_SET_POWER mit MODE_IDLE und VALUE=0 ist der
        # harte "Speicher wird nicht geladen"-Befehl. IDLE ist kein Freilauf,
        # sondern muss gehalten werden; nur AUTO wird bewusst nicht ge-heartbeatet.
        # Die 0W=unlimitiert-Quirk gilt fuer SET_MAX_CHARGE_POWER, nicht fuer
        # EMS_REQ_SET_POWER.
        if mode == MODE_IDLE:
            force = True

        # AUTO ist nur die Freigabe zur internen E3DC-Regelung. Wenn der E3DC
        # bereits freigegeben ist, senden wir keinen Heartbeat und greifen nicht
        # weiter in den Netzpunkt ein.
        if mode == MODE_AUTO:
            # Ein zuvor gesetztes MAX_DISCHARGE_POWER kann im E3DC ueber einen
            # Python-Neustart hinweg haengen bleiben. Dann steht AUTO zwar im
            # Status, der E3DC kann das Haus aber nur noch mit wenigen Watt aus
            # dem Akku versorgen. AUTO-Freigabe muss deshalb auch die Entlade-
            # sperre loesen; das ist fachlich nur das Ruecksetzen unserer
            # eigenen RSCP-Begrenzung, keine zusaetzliche Entladeanforderung.
            _auto_discharge_cap = max(10000, int(val))
            if self._ldcap < 0 or self._ldcap < (_auto_discharge_cap - 50):
                self.set_max_discharge_power(_auto_discharge_cap)
            if self._lm == MODE_AUTO and abs(val-self._lv) < 50:
                return
        if mode == MODE_AUTO and self._lm == MODE_AUTO and not force:
            return

        if not force and mode==self._lm and abs(val-self._lv)<50: return
        try:
            self._c.request([{'tag':RscpTag.EMS_REQ_SET_POWER,'type':RscpType.Container,'value':[
                {'tag':RscpTag.EMS_REQ_SET_POWER_MODE,'type':RscpType.UChar8,'value':mode},
                {'tag':RscpTag.EMS_REQ_SET_POWER_VALUE,'type':RscpType.Int32,'value':val}]}])
            nm={0:'AUTO',1:'IDLE',2:'DISCH',3:'CHRG',4:'GRID'}
            log.info('RSCP SET: %s(%d) %dW'%(nm.get(mode,'?'),mode,val))
            self._lm=mode; self._lv=val
        except Exception as e:
            log.error('RSCP send: %s'%e)
            try: self._c.close()
            except: pass
            self._c=None
    def set_max_charge_power(self, val, force=False):
        if not self._c and not self._conn(): return
        # E3DC QUIRK: SET_MAX_CHARGE_POWER=0 bedeutet 'unlimitiert'.
        # Wenn wir das Laden blockieren wollen (z.B. bei CHRG 1W), muessen wir 50W senden.
        # Wenn die C++-Logik 0W sendet, meint sie "Limit aufheben"!
        val = int(val)
        if not force and abs(val - self._lcap) < 50:
            return
        actual_w = max(50, val) if val < 200 and val > 0 else val
        try:
            self._c.request([{'tag':RscpTag.EMS_REQ_SET_MAX_CHARGE_POWER,'type':RscpType.Int32,'value':actual_w}])
            log.info('RSCP MAX_CHARGE_POWER: %dW (Echt: %dW)' % (val, actual_w))
            self._lcap = val
        except Exception as e:
            log.error('RSCP set_max_charge: %s'%e)
            try: self._c.close()
            except: pass
            self._c=None
    def set_max_discharge_power(self, val):
        if not self._c and not self._conn(): return
        val = int(val)
        if self._ldcap >= 0 and abs(val - self._ldcap) < 50:
            return
        try:
            self._c.request([{'tag':RscpTag.EMS_REQ_SET_MAX_DISCHARGE_POWER,'type':RscpType.Int32,'value':val}])
            log.info('RSCP MAX_DISCHARGE_POWER: %dW' % val)
            self._ldcap = val
        except Exception as e:
            log.error('RSCP set_max_discharge: %s'%e)
            try: self._c.close()
            except: pass
            self._c=None
    def release(self, hold_idle=False):
        """Shutdown: defensiv IDLE halten, wenn der letzte Zustand begrenzt war."""
        if not self._c: self._conn()
        if hold_idle:
            self.send(MODE_IDLE, 0, force=True)
        else:
            self.set_max_discharge_power(10000)
            self.send(MODE_AUTO, 10000, force=True)  # 10000W = unlimitiert freigeben
    def close(self):
        if self._c:
            try: self._c.close()
            except: pass
        self._c=None

def main():
    log.info('=== E3DC Storage Manager gestartet ===')
    cfg=load_cfg()
    host=str(cfg.get('server_ip','') or '').strip()
    port=int(gf(cfg,'server_port',5033))
    user=str(cfg.get('e3dc_user','') or '').strip()
    pw=str(cfg.get('e3dc_password','') or '').strip()
    aes=cfg.get('aes_password')
    rscp_pw=str(aes).strip() if aes and str(aes).strip() else pw
    while (not host or not user or not pw or not rscp_pw) and not _stop:
        log.error('RSCP-Konfiguration unvollständig. Warte auf gespeicherte Konfiguration.')
        time.sleep(30)
        cfg=load_cfg()
        host=str(cfg.get('server_ip','') or '').strip()
        port=int(gf(cfg,'server_port',5033))
        user=str(cfg.get('e3dc_user','') or '').strip()
        pw=str(cfg.get('e3dc_password','') or '').strip()
        aes=cfg.get('aes_password')
        rscp_pw=str(aes).strip() if aes and str(aes).strip() else pw
    if not host or not user or not pw or not rscp_pw:
        log.error('RSCP-Konfiguration unvollständig. Abbruch.')
        return

    ctrl=BattCtrl(host,port,user,pw,rscp_pw)
    cfg_ts=0; hb=0

    # ----------------------------------------------------------------
    # Eba Zustandsvariablen (persistent ueber Zyklen)
    # ----------------------------------------------------------------
    iFc         = 0          # Anforderungs-Ladeleistung [W]
    iMinLade    = 0          # Lineare Mindest-Ladeleistung [W]
    iMinLade2   = 0          # Mindest-Ladeleistung Periode 2
    iBattLoad   = 0          # Aktuell gesetzter Ladewert [W]
    iE3DC_Req_Load     = 0  # Anforderung an E3DC [W]
    iE3DC_Req_Load_alt = 0  # Vorheriger Wert (fuer Grid-Waechter)
    iDiffLadeleistung  = 0  # Korrektur fuer Regelverzoegerung [W]
    iMaxBattLade       = 0  # Gemessene Max-Ladeleistung [W]
    iLMStatus   = 5          # Eba State: 1=aktiv, >1=countdown, <0=neg-countdown
    fAvBatterie = 0.0        # Gleitender Mittelwert Bat-Leistung (30 Zyklen)
    fAvBatterie900=0.0       # Gleitender Mittelwert (900 Zyklen)
    iAvBatt_Count=0
    iAvBatt_Count900=0
    fBatt_SOC_alt=-1.0
    tLadezeit_alt=0
    tLadezeitende_alt=0
    bCheckConfig=True
    awattar_mode_prev = 1   # Vorheriger awattar_mode fuer Freigabe-Erkennung
    price_boost_idle_until = 0.0
    _abregel_charge_last = 0
    wb_fine_next_step_count = 0
    wb_fine_trim_hold_w = 0
    wb_fine_last_amp = 0
    price_hold_discharge_w = 0
    price_hold_auto_until = 0.0
    power_filter_ready = False
    fPower_Grid_ema = 0.0
    iPowerHome_ema = 0.0
    consumer_priority_last_key = None
    consumer_priority_changed_at = -999999.0
    consumer_previous_active = {"heatpump": False, "wallbox": False, "heater": False}

    # --- Pre-Discharge (Eba Unload) State Machine ---
    # Zustand: None=warten, 'active'=entladen, 'done'=beendet (Reset taglich)
    pd_state      = None    # Pre-Discharge Zustand
    pd_target_soc = None    # Eingefrorenes Ziel-SoC [%]
    pd_ladestart  = None    # Eingefrorener Ladestart-Timestamp [s]
    pd_frozen_day = None    # Datum des letzten Einfrierens
    pd_start_soc  = None    # SoC beim Start der Entladerampe [%]
    pd_start_ts   = None    # Startzeit der Entladerampe [s]
    pd_grid_guard_hold_w = 0
    pd_grid_guard_hold_until = 0.0
    pd_consumer_wait_since = 0.0
    tl_idle_grid_guard_hold_w = 0
    tl_idle_grid_guard_hold_until = 0.0
    tl_idle_charge_hold_until = 0.0
    _abregel_hold_until = 0.0
    _abregel_auto_since = 0.0

    # --- Trajectory-Clamping Grid-Waechter Hysterese ---
    # Zaehlt konsekutive Zyklen mit Grid-Import > tl_grid_limit_w.
    # Bremse wird NUR freigegeben wenn Netzbezug dauerhaft anliegt (kein 1s-Transient).
    _tl_grid_consec    = 0
    _TL_GRID_CONSEC_LIMIT = 3
    _abregel_was_aktiv = False  # Hysterese: bleibt True bis Bedingung sicher weg
    _tl_softcap_gate = False
    _tl_softcap_label = 'Kurve'
    _tl_softcap_soc = None
    _tl_softcap_ts = None
    _tl_autodump_gate = False
    _ifc_grid_block = False
    _ifc_grid_block_until = 0.0
    _ifc_cap_ramp = 0
    _ifc_auto_quiet_until = 0.0
    _ifc_auto_quiet_last_log = 0.0
    _ifc_auto_quiet_reason = ''
    _idle_charge_violation_count = 0
    _tl_forced_by_noon_logged = False

    # --- Letzten TL-Zustand aus State-Datei als Startup-Fallback laden ---
    # Wenn beim Start die Plan-Datei kurzzeitig nicht lesbar ist (Race Condition
    # mit storage_simulator), nutzen wir die zuletzt bekannten TL-Werte.
    # Diese werden im ersten erfolgreichen Plan-Lese-Zyklus ueberschrieben.
    tl_soc_now    = None
    tl_soc_target = None
    tl_ts_target  = None
    tl_active     = False
    try:
        _saved_state = json.load(open(STATE_F, 'r', encoding='utf-8'))
        _sn  = _saved_state.get('tl_soc_now')
        _st  = _saved_state.get('tl_soc_target')
        _tst = _saved_state.get('tl_ts_target')
        if _sn is not None and _st is not None:
            tl_soc_now   = float(_sn)
            tl_soc_target= float(_st)
            tl_ts_target = float(_tst) if _tst is not None else None
            tl_active    = True
            log.info('[INIT] TL Fallback aus State: soc_now=%.1f%% soc_target=%.1f%%' % (
                tl_soc_now, tl_soc_target))
    except Exception:
        pass  # Kein State vorhanden: normaler Erststart

    _last_wb9_state = None
    _wb9_discharge_gate = False

    # C++-naher Start: kein pauschales IDLE senden. Ein Start-IDLE kann bei
    # manchen E3DC nachts die Hausversorgung aus dem Akku unterbrechen oder ein
    # altes Entlade-Limit am Leben halten. Die erste Regelrunde entscheidet
    # sofort anhand von Live-Daten, ob IDLE, AUTO, CHRG oder DISCH passend ist.
    log.info('[INIT] Kein pauschales IDLE - erste Regelrunde entscheidet')

    while not _stop:
        t0=time.time()
        dt=datetime.datetime.fromtimestamp(t0)
        now_h=dt.hour+dt.minute/60.0
        # t in Sekunden seit Mitternacht (wie Eba)
        t=int(now_h*3600)

        if t0-cfg_ts>60: cfg=load_cfg(); cfg_ts=t0

        live=read_live()
        if not live:
            write_state({'state':'no_data','mode':-1})
            time.sleep(CYCLE_S); continue

        # ---- Live-Daten (Eba-Konvention: bat>0=laden, grid>0=Netzbezug) ----
        fBatt_SOC  = float(live.get('SOC',0) or 0)
        iPower_PV  = int(live.get('PV_Power',0) or 0)      # Eba: iPower_PV
        fPower_Grid= float(live.get('Grid_Power',0) or 0)  # Eba: fPower_Grid >0=Bezug <0=Einspeisung
        iPowerHome = int(live.get('Home_Power',0) or 0)    # Eba: iPowerHome
        iPower_Bat = int(live.get('Battery_Power',0) or 0) # Eba: iPower_Bat >0=laden <0=entladen
        fPower_WB  = float(live.get('Wallbox_Power',0) or 0)
        live_wb_phase_sum = 0.0
        live_wb_charging = False
        wb_real_power_active = False
        try:
            live_wb_phase_sum = (
                abs(float(live.get('wb_p1', 0) or 0)) +
                abs(float(live.get('wb_p2', 0) or 0)) +
                abs(float(live.get('wb_p3', 0) or 0))
            )
            live_wb_charging = bool(live.get('wb_charging', False))
            wb_real_power_active = bool(live_wb_phase_sum > 500)
            # E3DC/Multi Connect can report stale/phantom WB totals while the
            # phase meters and working bit are already quiet. C++ effectively
            # keyed Mode 9/10 support off real WB power/status, not intention.
            if abs(fPower_WB) > 500 and live_wb_phase_sum < 500:
                fPower_WB = 0.0
        except Exception:
            pass

        # Bei Fremd-Wallboxen (z.B. openWB via HTTP) steht die WB-Leistung nicht
        # immer in live_data_py.json. Dann die frischen Wallbox-Ramdiskwerte
        # nutzen, damit Mode 9/10 die Batterie bis wbminsoc wirklich fuer das
        # Auto freigibt. Wichtig: Dies muss VOR der Hausverbrauchs-EMA passieren,
        # sonst haengt die traege WB-Leistung als wandernde Hauslast in der
        # Regelbasis.
        wb_status_charging = False
        wb_intent = {}
        wb_intent_fresh = False
        _wb_native = {}
        try:
            _wb_native = read_json_file(WB_NATIVE_F)
            _openwb    = read_json_file(OPENWB_F)
            wb_intent  = read_json_file(WB_INTENT_F, max_age_s=20)
            wb_intent_fresh = bool(wb_intent)
            _wb_active_threshold_w = 500.0
            for _wb_src, _p_key, _c_key in (
                    (_wb_native, 'total_power_w', 'charging_active'),
                    (_openwb, 'power_w', 'charge_state')):
                _p = float(_wb_src.get(_p_key, 0) or 0)
                _src_real_charging = bool(_wb_src.get(_c_key, False)) and abs(_p) > _wb_active_threshold_w
                if _src_real_charging and abs(_p) > abs(fPower_WB):
                    fPower_WB = _p
                    wb_real_power_active = True
                # "charge_state"/"charging_active" kann bei Fremdsystemen auch
                # PV-Wartezustand bedeuten. Fuer den wbminSoC-Hold zaehlt nur
                # echte Wallbox-Leistung, sonst wuerden Hauslasten faelschlich
                # durch EMS_IDLE vom Speicher getrennt.
                if _src_real_charging:
                    wb_status_charging = True
                    wb_real_power_active = True
        except Exception:
            wb_status_charging = False
            wb_intent = {}
            wb_intent_fresh = False

        try:
            _live_wb_w = float(live.get('Wallbox_Power', 0) or 0)
            if abs(fPower_WB) > 500 and abs(_live_wb_w) < 500:
                # Bei Fremd-WB/openWB steckt die Ladeleistung haeufig schon im
                # E3DC-Hausverbrauch. Sobald wir die WB-Leistung separat aus
                # openwb_data/wallbox_native kennen, ziehen wir sie vor der EMA
                # aus Home ab, damit Storage- und Wallbox-Budget sie nicht
                # doppelt und nicht zeitverzoegert zaehlen.
                _home_without_wb = float(iPowerHome) - abs(fPower_WB)
                if _home_without_wb >= -300.0:
                    iPowerHome = max(0, int(_home_without_wb))
        except Exception:
            pass

        # Kurz taktende Verbraucher wie Induktionsfelder springen oft im
        # 2-5s-Rhythmus um ~1 kW. Die Eba-Logik soll am Netzpunkt ruhig
        # bleiben: Schutzpfade sehen weiter den Rohwert, iFc/WB-Budget den
        # geglaetteten Mittelwert.
        if not power_filter_ready:
            fPower_Grid_ema = fPower_Grid
            iPowerHome_ema = float(iPowerHome)
            power_filter_ready = True
        else:
            _grid_alpha = 0.25
            fPower_Grid_ema = fPower_Grid_ema * (1.0 - _grid_alpha) + fPower_Grid * _grid_alpha
            iPowerHome_ema = iPowerHome_ema * (1.0 - _grid_alpha) + float(iPowerHome) * _grid_alpha
        fPower_Grid_ctrl = fPower_Grid_ema
        iPowerHome_budget = int(round(iPowerHome_ema))
        # heizstab_power ist lowercase in live_data_py.json
        wp_w       = float(live.get('WP_Power', live.get('heizstab_power',
                          live.get('Heizstab_Power', 0))) or 0)

        try:
            wb_intent_request = str(wb_intent.get('battery_request', 'none') or 'none')
            wb_intent_reason = str(wb_intent.get('reason', '') or '')
            wb_intent_mode = int(float(wb_intent.get('wb_mode_active', 0) or 0))
            wb_intent_car_active = bool(wb_intent.get('active') or wb_intent.get('car_active'))
            wb_intent_power_w = abs(float(wb_intent.get('wb_power_w', 0) or 0))
            wb_intent_set_amp = int(float(wb_intent.get('cap_amp', wb_intent.get('set_amp', 0)) or 0))
            wb_intent_phases = int(float(
                wb_intent.get('detected_phases',
                _wb_native.get('detected_phases', 1)) or 1
            ))
            if wb_intent_phases not in (1, 2, 3):
                wb_intent_phases = 1
            # C++ hatte keinen eigenen "Start beabsichtigt"-Speicherpfad:
            # Wallbox-Start und Speicherentladung waren ueber reale WB-Leistung,
            # Netzpunkt und iAvalPower gekoppelt. Im getrennten Python-System
            # darf ein Startwunsch deshalb keine Storage-Freigabe ausloesen.
            # Das Feld bleibt nur als Diagnose sichtbar.
            wb_intent_starting = False
            wb_intent_charging = (
                bool(wb_intent.get('charging_active'))
                or wb_intent_power_w > 500
            )
            if wb_intent_fresh and not wb_intent_charging and wb_intent_power_w <= 500 and live_wb_phase_sum < 500:
                # Frischer Wallbox-Manager sagt: Fahrzeug ggf. verbunden, aber
                # keine echte Ladeleistung. Dann duerfen alte E3DC-/WB-Summen
                # nicht laenger als Last fuer wbminSoC-Hold oder Speicher-DISCH
                # zaehlen. Genau diese stale Werte erzeugten Phantomladung nach
                # dem Stop an der wbminSoC-Grenze.
                fPower_WB = 0.0
                wb_status_charging = False
                wb_real_power_active = False
        except Exception:
            wb_intent_request = 'none'
            wb_intent_reason = ''
            wb_intent_mode = 0
            wb_intent_car_active = False
            wb_intent_charging = False
            wb_intent_power_w = 0.0
            wb_intent_set_amp = 0
            wb_intent_starting = False
            wb_intent_phases = 1

        # Harte Messwert-Grenze fuer Speicher-Eingriffe:
        # Sollstrom, cap_amp, Startwunsch oder ein reines Ladebit duerfen nie
        # als echte WB-Last gelten. Fuer DISCH/IDLE-Entscheidungen zaehlen nur
        # gemessene Phasenleistung oder gemeldete echte WB-Leistung.
        wb_measured_for_storage = bool(
            live_wb_phase_sum > 500
            or (wb_intent_fresh and wb_intent_charging and wb_intent_power_w > 500)
            or (wb_status_charging and abs(float(fPower_WB or 0.0)) > 500)
            or (not wb_intent_fresh and abs(float(fPower_WB or 0.0)) > 500)
        )

        # ---- Konfiguration (Eba e3dc_config.*) ----
        maximumLadeleistung  = int(gf(cfg,'maximumladeleistung',11000))
        minimumLadeleistung  = int(gf(cfg,'minimumladeleistung',300))
        speichergroesse      = gf(cfg,'speichergroesse',10.0)     # kWh
        untererLadekorridor  = int(gf(cfg,'untererladekorridor',200))
        powerfaktor          = gf(cfg,'powerfaktor',1.5)
        # einspeiselimit_w direkt in W aus Config; fallback: einspeiselimit (kW) * 1000
        einspeiselimit_w     = gf(cfg,'einspeiselimit_w',
                                  gf(cfg,'einspeiselimit',0)*1000)
        ladeschwelle         = gf(cfg,'ladeschwelle',10.0)
        storage_morning_soc  = gf(cfg,'storage_morning_soc',20.0)
        storage_morning_hour = gf(cfg,'storage_morning_hour',9.0)
        # ep_reserve_pct aus live_data (E3DC meldet aktuellen Wert); fallback Config/Default
        ep_reserve           = float(live.get('ep_reserve_pct',0) or
                                     gf(cfg,'ep_reserve_pct',8.0))
        lat                  = gf(cfg,'latitude',gf(cfg,'lat',48.3))   # Default: Bayern
        lon                  = gf(cfg,'longitude',gf(cfg,'lon',11.9))  # Default: Bayern

        try: sunrise_h,sunset_h=solar_times(lat,lon,dt.date())
        except: sunrise_h,sunset_h=6.0,20.0

        # Keep-Alive: force alle 10s
        hb+=1; force=(hb>=2)
        if force: hb=0

        # Notstrom/Inselbetrieb: keine Optimierungsregel darf gegen den E3DC
        # arbeiten. Speicher freigeben, Verbraucher-Budget sperren, dann den
        # naechsten Zyklus abwarten.
        try:
            notstrom_status = int(live.get('Notstrom_Status',
                                  live.get('ems_emergency_power_status', 0)) or 0)
        except Exception:
            notstrom_status = 0
        if notstrom_status in (1, 4):
            ctrl.send(MODE_AUTO, maximumLadeleistung, force=False)
            reason_notstrom = '[%s] NOTSTROM-AUTO Status=%d - E3DC autonom, externe Budgets gesperrt' % (
                dt.strftime('%H:%M'), notstrom_status)
            write_state({'state':'emergency_power','reason':reason_notstrom,
                         'mode':MODE_AUTO,'val':maximumLadeleistung,
                         'soc':fBatt_SOC,'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                         'notstrom_status':notstrom_status})
            try:
                wb_budget = {
                    'budget_w': 0, 'budget_amp_1ph': 0, 'budget_amp_3ph': 0,
                    'iAVal_w': 0, 'iFc_w': 0, 'iMinLade_w': 0,
                    'state': 'emergency_power',
                    'storage_state': 'emergency_power',
                    'reason': reason_notstrom[:100],
                    'ts': t0,
                    'energy_score': {
                        'pv_surplus_w': 0,
                        'free_for_limbs_w': 0,
                        'bat_charge_request_w': 0,
                        'prio_factor': 0.0,
                        'prio_reason': 'emergency_power',
                    }
                }
                tmp = WB_F + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(wb_budget, f)
                os.replace(tmp, WB_F)
            except Exception:
                pass
            log.warning(reason_notstrom)
            time.sleep(CYCLE_S); continue

        # --- Manueller Batterie-Override (hoechste Prioritaet) ---
        # Geschrieben von manual_bat_cmd.php via UI-Buttons "Laden" / "Entladen" / "Automatik"
        _MANUAL_OVERRIDE_FILE = os.path.join(RAMDISK, 'manual_bat_override.json')
        _manual_override = None
        try:
            if os.path.exists(_MANUAL_OVERRIDE_FILE):
                _mov = json.load(open(_MANUAL_OVERRIDE_FILE, encoding='utf-8'))
                _mov_mode = str(_mov.get('mode', 'auto')).lower()
                _mov_target = int(_mov.get('target_soc', 80))

                if _mov_mode == 'charge':
                    if fBatt_SOC < _mov_target:
                        _manual_override = (MODE_CHRG, maximumLadeleistung, 'Manuell LADEN bis %d%%' % _mov_target)
                    else:
                        try: os.unlink(_MANUAL_OVERRIDE_FILE)
                        except: pass
                        log.info('Manual Override LADEN: Ziel %d%% erreicht — Override geloescht.' % _mov_target)
                elif _mov_mode == 'discharge':
                    if fBatt_SOC > _mov_target:
                        dis_w = int(gf(cfg, 'maximaleentladeleistung', 11000))
                        _manual_override = (MODE_DISCH, dis_w, 'Manuell ENTLADEN bis %d%%' % _mov_target)
                    else:
                        try: os.unlink(_MANUAL_OVERRIDE_FILE)
                        except: pass
                        log.info('Manual Override ENTLADEN: Ziel %d%% erreicht — Override geloescht.' % _mov_target)
        except Exception as _me:
            log.warning('Manual Override Lesen fehlgeschlagen: %s' % _me)

        if _manual_override:
            mode, val, reason = _manual_override
            # Beim Laden MAX_CHARGE_POWER freigeben
            if mode == MODE_CHRG:
                ctrl.set_max_charge_power(val)
            ctrl.send(mode, val, force=force)

            write_state({'state': 'manual_override', 'reason': reason,
                         'mode': mode, 'val': val, 'soc': fBatt_SOC,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid)})

            # Budget für Wallbox/WP weitergeben
            if mode == MODE_DISCH:
                _pv_now = float(live.get('PV_Power', 0) or 0)
                _home_now = float(live.get('Consumption_W', 0) or 0)
                _wp_now = float(live.get('WP_Power', live.get('heizstab_power', live.get('Heizstab_Power', 0))) or 0)
                _free_w = max(0, val + _pv_now - _home_now - _wp_now)
                _bdata = {
                    'budget_w':       _free_w,
                    'budget_amp_1ph': (max(6, min(32, int(_free_w / 230))) if _free_w >= 6*230 else 0),
                    'budget_amp_3ph': (max(6, min(32, int(_free_w / 690))) if _free_w >= 6*690 else 0),
                    'iAVal_w':        _free_w,
                    'iFc_w':          0.0,
                    'iMinLade_w':     0.0,
                    'state':          'manual_override',
                    'storage_state':  'manual_override',
                    'reason':         reason[:100] if reason else 'Manual Discharge',
                    'ts':             t0,
                    'energy_score': {
                        'state': 'manual_override', 'free_for_limbs_w': _free_w,
                        'pv_surplus_w': max(0, _pv_now - _home_now - _wp_now),
                        'bat_charge_request_w': 0, 'prio_factor': 1.0, 'prio_reason': 'manual'
                    }
                }
            else:
                _bdata = {
                    'budget_w':       0.0,
                    'budget_amp_1ph': 0,
                    'budget_amp_3ph': 0,
                    'iAVal_w':        0.0,
                    'iFc_w':          0.0,
                    'iMinLade_w':     0.0,
                    'state':          'manual_override',
                    'storage_state':  'manual_override',
                    'reason':         reason[:100] if reason else 'Manual Charge',
                    'ts':             t0,
                    'energy_score': {
                        'state': 'manual_override', 'free_for_limbs_w': 0,
                        'pv_surplus_w': 0, 'bat_charge_request_w': val,
                        'prio_factor': 0.0, 'prio_reason': 'manual'
                    }
                }

            try:
                tmp = WB_F + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(_bdata, f)
                os.replace(tmp, WB_F)
            except: pass

            time.sleep(CYCLE_S); continue


        # tLadezeitende und fLadeende aus Plan
        end_h,fLadeende=get_ladeende(cfg,now_h,sunrise_h,sunset_h)
        wb_curve_target_soc = wb_target_curve_soc(
            cfg, wb_intent, wb_intent_fresh, wb_intent_car_active,
            wb_intent_mode, fLadeende
        )
        if wb_curve_target_soc is not None and wb_curve_target_soc < float(fLadeende) - 0.1:
            _old_ladeende_soc = float(fLadeende)
            fLadeende = wb_curve_target_soc
            log_status_throttled(
                'wb_target_curve_soc',
                '[%s] Wallbox-Zielkurve: Speicherziel %.1f%% -> wbminSoC %.1f%% '
                '(gleicher Startpfad wie PV-Kurve, hoehere Auto-Prioritaet)' % (
                    dt.strftime('%H:%M'), _old_ladeende_soc, wb_curve_target_soc
                )
            )
        day_end_h = end_h
        day_target_soc = float(fLadeende)
        tLadezeitende=int(end_h*3600)  # Sekunden seit Mitternacht
        tl_pv_day_active = (iPower_PV > 500 and t < tLadezeitende)

        # ----------------------------------------------------------------
        # Eba CheckaWATTar() Ergebnis aus storage_plan.json lesen
        # (berechnet vom storage_simulator alle 15min)
        # awattar_mode: 0=Entladen stoppen, 1=Normal, 2=Netzladen
        # ----------------------------------------------------------------
        awattar_mode   = 1  # Default: Normal
        awattar_reason = ''
        cheap_grid_charge = {}
        forecast_pv_now_w = 0.0
        pv_collapse_active = False
        pv_collapse_ratio = 1.0
        try:
            _plan = json.load(open(PLAN_F,'r',encoding='utf-8'))
            awattar_mode   = int(_plan.get('awattar_mode', 1))
            awattar_reason = str(_plan.get('awattar_reason', ''))
            cheap_grid_charge = _plan.get('cheap_grid_charge', {}) or {}
            _plan_ts = _plan.get('ts')
            if _plan_ts:
                _plan_age = time.time() - datetime.datetime.fromisoformat(str(_plan_ts)).timestamp()
                if _plan_age > 1800:
                    log.warning('[%s] Speicherplan %.0fs alt -> Preis-/Netzladen deaktiviert, Normalbetrieb' % (
                        dt.strftime('%H:%M'), _plan_age))
                    awattar_mode = 1
                    cheap_grid_charge = {}
            try:
                _now_ms = t0 * 1000.0
                _slots = [
                    s for s in (_plan.get('timeline') or [])
                    if abs(float(s.get('ts', 0) or 0) - _now_ms) <= 45 * 60000
                ]
                if _slots:
                    _slot = min(_slots, key=lambda s: abs(float(s.get('ts', 0) or 0) - _now_ms))
                    forecast_pv_now_w = max(0.0, float(_slot.get('pv_w', 0) or 0))
                    _collapse_min_w = max(1000.0, float(gf(cfg, 'tl_pv_collapse_forecast_min_w', 1500.0)))
                    _collapse_ratio_limit = min(0.5, max(0.02, float(gf(cfg, 'tl_pv_collapse_ratio', 0.10))))
                    if forecast_pv_now_w >= _collapse_min_w:
                        pv_collapse_ratio = max(0.0, float(iPower_PV)) / max(1.0, forecast_pv_now_w)
                        pv_collapse_active = bool(
                            pv_collapse_ratio <= _collapse_ratio_limit
                            and t < tLadezeitende
                            and fBatt_SOC > ep_reserve + 0.5
                        )
            except Exception:
                forecast_pv_now_w = 0.0
                pv_collapse_active = False
                pv_collapse_ratio = 1.0
        except: pass

        if awattar_mode == 2 and cheap_grid_charge.get('active') and not cheap_grid_charge_active(cheap_grid_charge):
            log.info('[%s] Preis-Boost Fenster beendet -> zurueck zur Ladekurve' % dt.strftime('%H:%M'))
            awattar_mode = 1
            cheap_grid_charge = {}

        # Freigabe: wenn awattar_mode von 0/2 auf 1 wechselt -> sofort AUTO senden
        # (E3DC haelt letzten Befehl; ohne Freigabe bleibt Batterie gesperrt/Netz)
        if awattar_mode == 1 and awattar_mode_prev in (0, 2):
            ctrl.send(MODE_AUTO, maximumLadeleistung, force=True)
            log.info('[%s] Awattar Freigabe (mode %d->1) - Batterie wieder AUTO' % (
                dt.strftime('%H:%M'), awattar_mode_prev))
        awattar_mode_prev = awattar_mode

        # Mode 2 = Netzladen (Eba: idauer > 0)
        if awattar_mode == 2:
            if cheap_grid_charge.get('active'):
                grid_target_soc = float(cheap_grid_charge.get('target_soc', fLadeende) or fLadeende)
                grid_charge_w = int(cheap_grid_charge.get('charge_w', maximumLadeleistung) or maximumLadeleistung)
                grid_hyst = float(cheap_grid_charge.get('hysteresis_pct', 0.5) or 0.5)
                grid_state = 'cheap_grid_charge'
            else:
                grid_target_soc = fLadeende  # Ziel-SoC aus Plan
                grid_charge_w = int(maximumLadeleistung)
                grid_hyst = 0.5
                grid_state = 'grid_charge'

            grid_charge_w = max(300, min(int(maximumLadeleistung), grid_charge_w))
            if fBatt_SOC < grid_target_soc - grid_hyst:
                ctrl.send(MODE_GRID, grid_charge_w, force=force)
                reason_aw = '[%s] NETZLADEN SOC=%.1f%% Ziel=%.1f%% Hyst=%.1f%% %dW | %s' % (
                    dt.strftime('%H:%M'), fBatt_SOC, grid_target_soc, grid_hyst,
                    grid_charge_w, awattar_reason[:80])
                log.info(reason_aw)
                write_state({'state':grid_state,'reason':reason_aw,'mode':MODE_GRID,
                             'val':grid_charge_w,'soc':fBatt_SOC,
                             'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                             'cheap_grid_charge':cheap_grid_charge})
                write_wb_budget({
                    'budget_w': 0,
                    'iAVal_w': 0,
                    'iFc_w': 0.0,
                    'iMinLade_w': float(grid_charge_w),
                    'state': grid_state,
                    'storage_state': grid_state,
                    'reason': reason_aw[:100],
                    'cheap_grid_charge': cheap_grid_charge,
                    'energy_score': {
                        'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                        'free_for_limbs_w': 0,
                        'bat_charge_request_w': int(grid_charge_w),
                        'prio_factor': 0.0,
                        'prio_reason': grid_state,
                    }
                })
                time.sleep(CYCLE_S); continue
            else:
                log.info('[%s] Netzladen: Ziel %.1f%% erreicht (%.1f%%, Hyst %.1f%%) -> Normal' % (
                    dt.strftime('%H:%M'), grid_target_soc, fBatt_SOC, grid_hyst))
                awattar_mode = 1  # Ziel erreicht: zurueck auf Normal

        _price_boost_any = price_boost_is_active()
        if _price_boost_any:
            awattar_mode = 1

        # Mode 0 = Entladen stoppen (Eba: return 0 aus CheckaWATTar)
        if awattar_mode == 0:
            # Ausnahme: Ziel-/Preislimit-Modus (PV+Hausspeicher) + WB laedt aktiv
            # -> Batterie fuer Auto freigeben (E3DC sperrt Netz via WBchar6 Mode=1 selbst)
            if wb_intent_fresh and wb_intent_car_active:
                wb_mode_now = normalize_wb_mode(wb_intent_mode)
            else:
                wb_mode_now = max(
                    normalize_wb_mode(cfg.get('wb1_mode', 0)),
                    normalize_wb_mode(cfg.get('wb2_mode', 0)),
                )
            wb_charging  = bool(wb_measured_for_storage)
            if storage_floor_mode(wb_mode_now) and wb_charging:
                log.info('[%s] ENTLADEN-STOPP uebersprungen: Mode%d+WB=%.0fW (Bat fuer Auto frei)' % (
                    dt.strftime('%H:%M'), wb_mode_now, fPower_WB))
                # Kein continue -> weiter in Eba-Normalbetrieb (MODE_AUTO)
            else:
                ctrl.send(MODE_IDLE, 0, force=force)
                reason_aw = '[%s] ENTLADEN-STOPP SOC=%.1f%% | %s' % (
                    dt.strftime('%H:%M'), fBatt_SOC, awattar_reason[:60])
                log.info(reason_aw)
                write_state({'state':'discharge_stop','reason':reason_aw,'mode':MODE_IDLE,
                             'val':0,'soc':fBatt_SOC,'pv_w':iPower_PV,'grid_w':int(fPower_Grid)})
                write_wb_budget({
                    'budget_w': 0,
                    'iAVal_w': 0,
                    'iFc_w': 0.0,
                    'iMinLade_w': 0.0,
                    'state': 'discharge_stop',
                    'storage_state': 'discharge_stop',
                    'reason': reason_aw[:100],
                    'energy_score': {
                        'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                        'free_for_limbs_w': 0,
                        'bat_charge_request_w': 0,
                        'prio_factor': 0.0,
                        'prio_reason': 'discharge_stop',
                    }
                })
                time.sleep(CYCLE_S); continue

        # ----------------------------------------------------------------
        # Morgen-Autonomie: kleine Speicher nicht an der EP-Reserve festnageln.
        # Wenn der Morgen-Puffer ueber der E3DC-Reserve liegt, darf der E3DC
        # nach Sonnenaufgang autonom bis zu diesem Puffer laden.
        # ----------------------------------------------------------------
        morning_target = max(float(storage_morning_soc or 0), float(ladeschwelle or 0))
        morning_hyst = 0.5
        # Nur bis zum konfigurierten Morgenanker autonom aufbauen. Danach muss
        # die normale C++-nahe Ladekurve uebernehmen, sonst bleibt der Speicher
        # bei knapp verfehltem Morgenpuffer im AUTO-Hold haengen und iFc greift
        # trotz PV-Ueberschuss nicht mehr.
        morning_anchor_h = max(float(sunrise_h - 0.25), float(storage_morning_hour))
        morning_window = (now_h >= (sunrise_h - 0.25) and now_h < min(morning_anchor_h, end_h))
        if (morning_window
            and morning_target > ep_reserve + morning_hyst
            and fBatt_SOC < morning_target - morning_hyst):
            ctrl.send(MODE_AUTO, maximumLadeleistung, force=False)
            reason_morning = ('[%s] ERHOLUNG-AUTO/MORGEN-AUTO SOC=%.1f%% < Morgenpuffer=%.1f%%@%.2fh '
                              '(EP=%.1f%%, E3DC autonom, WB-Budget 0W)') % (
                                  dt.strftime('%H:%M'), fBatt_SOC,
                                  morning_target, storage_morning_hour, ep_reserve)
            write_state({'state':'morning_autonomy','reason':reason_morning,
                         'mode':MODE_AUTO,'val':maximumLadeleistung,
                         'soc':fBatt_SOC,'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                         'morning_target_soc':morning_target})
            try:
                wb_budget = {
                    'budget_w': 0, 'budget_amp_1ph': 0, 'budget_amp_3ph': 0,
                    'iAVal_w': 0, 'iFc_w': 0, 'iMinLade_w': 0,
                    'state': 'morning_autonomy',
                    'storage_state': 'morning_autonomy',
                    'reason': reason_morning[:100],
                    'ts': t0,
                    'energy_score': {
                        'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                        'free_for_limbs_w': 0,
                        'bat_charge_request_w': 0,
                        'prio_factor': 0.0,
                        'prio_reason': 'morning_autonomy',
                    }
                }
                with open(WB_F,'w',encoding='utf-8') as f:
                    json.dump(wb_budget, f)
            except: pass
            log.info(reason_morning)
            time.sleep(CYCLE_S); continue

        # ----------------------------------------------------------------
        # EP-Reserve (Eba hatte keinen harten Stop): keine aktive Entladung
        # durch uns. C++-nah bleibt hier AUTO der richtige Befehl: Der E3DC
        # darf Hausverbrauch und PV-Ladung autonom regeln. Wir senden deshalb
        # nur EMS_REQ_SET_POWER AUTO(0) mit VALUE=maximumLadeleistung, aber
        # kein separates SET_MAX_CHARGE_POWER. Kleine CHRG-Werte aus dem
        # momentanen Netzueberschuss fuehren bei niedrigem SOC zu Pendeln,
        # weil die eigene Ladung den Netzueberschuss im naechsten Zyklus wieder
        # verschwinden laesst.
        # ----------------------------------------------------------------
        if fBatt_SOC<=ep_reserve+0.5:
            ctrl.send(MODE_AUTO, maximumLadeleistung, force=False)
            ep_mode = MODE_AUTO
            ep_val = maximumLadeleistung
            ep_state = 'ep_reserve_auto'
            ep_charge_req = maximumLadeleistung
            reason_ep = ('[%s] EP-RESERVE/AUTOMATIK SOC=%.1f%% <= EP=%.1f%% '
                         '-> E3DC autonom freigegeben/ueberwacht (%dW)') % (
                             dt.strftime('%H:%M'), fBatt_SOC, ep_reserve,
                             maximumLadeleistung)
            iE3DC_Req_Load_alt=0
            write_state({'state':ep_state,'reason':reason_ep,
                         'soc':fBatt_SOC,'mode':ep_mode,'val':int(ep_val),
                         'pv_w':iPower_PV,'grid_w':int(fPower_Grid)})
            write_wb_budget({
                'budget_w': 0,
                'iAVal_w': 0,
                'iFc_w': 0.0,
                'iMinLade_w': float(ep_charge_req),
                'state': ep_state,
                'storage_state': ep_state,
                'reason': reason_ep[:100],
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': 0,
                    'bat_charge_request_w': int(ep_charge_req),
                    'prio_factor': 0.0,
                    'prio_reason': ep_state,
                }
            })
            log.info(reason_ep)
            time.sleep(CYCLE_S); continue

        # ----------------------------------------------------------------
        # Pre-Discharge (Eba: e3dc_config.unload) - Nacht-Entladen vor PV
        # Sicherheitsprinzip:
        #   1. ladestart_ts: EINGEFROREN (Zeitpunkt bleibt stabil)
        #   2. ladestart_soc: IMMER FRISCH (Wetterprognose-Updates wirken!)
        #   3. Harter Abbruch bei Ladestart - IMMER, egal ob Ziel erreicht
        #   4. Startfenster: nur wenn hours_remaining <= pd_max_h (kein 10h-Dump)
        #   5. Wallbox-Pause: wenn Auto laedt, Battery entlaedt natural -> kein Zwang
        # ----------------------------------------------------------------
        today_d = dt.date()
        # Tag-Reset: Zustand zuruecksetzen fuer neuen Tag
        if pd_frozen_day != today_d:
            pd_state = None; pd_target_soc = None
            pd_ladestart = None; pd_start_soc = None; pd_start_ts = None
            pd_consumer_wait_since = 0.0
            pd_frozen_day = today_d
            log.info('[%s] Pre-Discharge State Reset (neuer Tag)' % dt.strftime('%H:%M'))

        predump_enabled = cfg_bool(cfg, 'predump_enable', True)
        if not predump_enabled:
            if pd_state == 'active':
                log.info('[%s] PRE-DISCH Abbruch: Pre-Dump ist in der Config deaktiviert' % dt.strftime('%H:%M'))
                ctrl.send(MODE_AUTO, maximumLadeleistung, force=True)
            pd_state = None
            pd_consumer_wait_since = 0.0
            write_predump_consumer_plan(False, reason='Pre-Dump deaktiviert')

        if predump_enabled and pd_state != 'done':
            # --- Immer frisch lesen: ladestart_ts (einfrieren) + ladestart_soc (NICHT einfrieren) ---
            pd_plan_unreachable = False
            try:
                _pdplan_raw = json.load(open(PLAN_F,'r',encoding='utf-8'))
                _ls  = _pdplan_raw.get('ladestart_ts')   # ms (Zeitpunkt)
                _soc = _pdplan_raw.get('ladestart_soc')  # % (Ziel-SoC - FRISCH)
                pd_plan_unreachable = (
                    _pdplan_raw.get('can_reach_target') is False
                    or bool((_pdplan_raw.get('target_curve_meta') or {}).get('target_capped_unreachable'))
                )
                # ladestart_soc: immer frisch uebernehmen (Wetter/Prognose-Updates!)
                if _soc is not None:
                    pd_target_soc = float(_soc)
                # ladestart_ts: nur einfrieren wenn noch nicht gesetzt
                if pd_ladestart is None and _ls:
                    pd_ladestart = float(_ls) / 1000.0  # ms -> s
                    if pd_ladestart < t0:
                        pd_ladestart += 86400.0  # auf naechsten Tag verschieben
                        log.info('[%s] Pre-Discharge: Ladestart auf %s verschoben' % (
                                 dt.strftime('%H:%M'),
                                 datetime.datetime.fromtimestamp(pd_ladestart).strftime('%d.%m %H:%M')))
                    log.info('[%s] Pre-Discharge Ladestart eingefroren: %s (Ziel aktuell: %.1f%%)' % (
                             dt.strftime('%H:%M'),
                             datetime.datetime.fromtimestamp(pd_ladestart).strftime('%H:%M'),
                             pd_target_soc if pd_target_soc is not None else -1))

                # Fallback: kein ladestart_ts im Plan, aber SOC noch ueber Ziel
                # und Uhrzeit im Morgen-Fenster (03:00-10:00) -> Wiederaufnahme
                # Ladestart = naechste volle Stunde (min. 30 Minuten Puffer)
                if pd_ladestart is None and pd_target_soc is not None:
                    _hour = dt.hour
                    if 3 <= _hour < 10 and fBatt_SOC > pd_target_soc + 2.0:
                        _fallback_ls = t0 + 3600.0  # 1h Puffer ab jetzt
                        pd_ladestart = _fallback_ls
                        log.info('[%s] Pre-Discharge: Kein ladestart_ts im Plan - Fallback Ladestart %s '
                                 '(SOC=%.1f%% > Ziel=%.1f%%)' % (
                                 dt.strftime('%H:%M'),
                                 datetime.datetime.fromtimestamp(pd_ladestart).strftime('%H:%M'),
                                 fBatt_SOC, pd_target_soc))

            except: pass

            # Prognose-Korrektur: Pre-Dump ist nur sinnvoll, solange das
            # Tagesziel mit aktueller Prognose erreichbar bleibt. Wenn die
            # Wetterlage kippt, darf der E3DC den Speicher wieder autonom
            # halten/laden statt weiter aktiv Platz zu schaffen.
            if pd_plan_unreachable:
                if pd_state == 'active':
                    _pd_abort_reason = (
                        '[%s] PRE-DISCH Abbruch: Tagesziel laut neuer Prognose '
                        'nicht mehr erreichbar -> E3DC Automatikmodus'
                    ) % dt.strftime('%H:%M')
                    log.info(_pd_abort_reason)
                    ctrl.send(MODE_AUTO, maximumLadeleistung, force=True)
                    write_predump_consumer_plan(False, reason='Prognose verschlechtert')
                    write_state({'state': 'auto', 'phase': 'Pre-Dump abgebrochen',
                                 'reason': _pd_abort_reason,
                                 'mode': MODE_AUTO, 'val': int(maximumLadeleistung),
                                 'soc': fBatt_SOC, 'pv_w': iPower_PV,
                                 'grid_w': int(fPower_Grid)})
                    pd_state = 'done'
                    time.sleep(CYCLE_S); continue
                # Noch nicht gestartet: nicht aktiv in den Speicher eingreifen.
                # Kein 'done', damit eine spaetere bessere Prognose am selben
                # Morgen den Pre-Dump wieder erlauben kann.
                write_predump_consumer_plan(False, reason='Prognose nicht erreichbar')

            # Max Startfenster aus Config (default 5h) - verhindert 10h-Vorausentladen.
            # Alte Anlagen haben hier teils 0 stehen, weil das Feld frueher wie
            # ein Boersenstrom-Feintuning wirkte. 0 darf den Ladekurven-Pre-Dump
            # nicht still komplett deaktivieren, sondern bedeutet Auto/Standard.
            pd_max_h_raw = gf(cfg, 'pd_max_hours', 5.0)
            pd_max_h = pd_max_h_raw if pd_max_h_raw > 0 else 5.0
            hours_remaining_chk = (pd_ladestart - t0) / 3600.0 if pd_ladestart else 999
            if pd_max_h_raw <= 0 and pd_ladestart:
                log_status_throttled(
                    'predump_auto_start_window',
                    '[%s] PRE-DISCH Startfenster Auto: pd_max_hours=%.1f -> %.1fh '
                    '(verhindert verstecktes Deaktivieren der Ladekurve)' % (
                        dt.strftime('%H:%M'), pd_max_h_raw, pd_max_h),
                    3600,
                )
            pd_morning_hyst = max(morning_hyst, gf(cfg, 'pd_morning_hyst_pct', 1.0))
            pd_morning_guard = False
            pd_morning_guard_soc = morning_target + pd_morning_hyst
            try:
                _morning_midnight = datetime.datetime(dt.year, dt.month, dt.day).timestamp()
                _morning_ts = _morning_midnight + float(storage_morning_hour) * 3600.0
                _pd_guard_until = max(_morning_ts, float(pd_ladestart or 0)) + 15 * 60
                pd_morning_guard = (
                    morning_target > ep_reserve + pd_morning_hyst
                    and pd_target_soc is not None
                    and pd_target_soc < (morning_target - 0.1)
                    and t0 <= _pd_guard_until
                )
            except Exception:
                pd_morning_guard = False
            pd_allow_cfg = {
                'wallbox': cfg_bool(cfg, 'predump_wallbox_enable', False),
                'heatpump': cfg_bool(cfg, 'predump_heatpump_enable', False),
                'heater': cfg_bool(cfg, 'predump_heater_enable', False),
            }
            pd_consumer_allowed_cfg = any(pd_allow_cfg.values())
            pd_start_margin = 0.5 if pd_consumer_allowed_cfg else 2.0
            pd_start_floor = pd_target_soc + pd_start_margin if pd_target_soc is not None else 999.0
            if pd_morning_guard:
                pd_start_floor = max(pd_start_floor, pd_morning_guard_soc)

            if pd_ladestart is not None and pd_target_soc is not None:
                # HARTER ABBRUCH: Ladestart erreicht -> IMMER beenden
                if t0 >= pd_ladestart:
                    if pd_state == 'active':
                        log.info('[%s] PRE-DISCH Abbruch: Ladestart! SOC=%.1f%% '
                                 '(Ziel war %.1f%%)' % (
                                 dt.strftime('%H:%M'), fBatt_SOC, pd_target_soc))
                        ctrl.send(MODE_AUTO, maximumLadeleistung, force=True)  # sofort freigeben
                    write_predump_consumer_plan(False, reason='Ladestart erreicht')
                    pd_state = 'done'
                    pd_consumer_wait_since = 0.0

                # ZIEL ERREICHT: Entladen abschliessen
                elif pd_state == 'active' and fBatt_SOC <= pd_target_soc + 0.5:
                    log.info('[%s] PRE-DISCH Ziel erreicht: SOC=%.1f%% <= %.1f%%' % (
                             dt.strftime('%H:%M'), fBatt_SOC, pd_target_soc))
                    ctrl.send(MODE_AUTO, maximumLadeleistung, force=True)
                    write_predump_consumer_plan(False, reason='Pre-Dump Ziel erreicht')
                    pd_state = 'done'
                    pd_consumer_wait_since = 0.0

                # START: Bedingungen pruefen
                # Normaler Nacht-Start: kein PV, SOC > Ziel, im Zeitfenster
                elif (pd_state is None
                      and not pd_plan_unreachable
                      and iPower_PV == 0                          # kein PV (Nacht)
                      and fBatt_SOC > pd_start_floor              # Ziel-Hysterese / Morgenpuffer
                      and fBatt_SOC > ep_reserve + 5.0           # Sicherheitsabstand
                      and hours_remaining_chk <= pd_max_h):       # Startfenster!
                    pd_state = 'active'
                    pd_start_soc = fBatt_SOC
                    pd_start_ts = t0
                    pd_consumer_wait_since = 0.0
                    log.info('[%s] PRE-DISCH Start: SOC=%.1f%% Ziel=%.1f%% '
                             'Ladestart=%s (%.1fh)' % (
                             dt.strftime('%H:%M'), fBatt_SOC, pd_target_soc,
                             datetime.datetime.fromtimestamp(pd_ladestart).strftime('%H:%M'),
                             hours_remaining_chk))

                # Wiederaufnahme nach Neustart: PV kann aktiv sein, aber SOC noch ueber Ziel
                # -> Pre-Discharge war aktiv und wurde durch Neustart unterbrochen
                elif (pd_state is None
                      and not pd_plan_unreachable
                      and fBatt_SOC > pd_start_floor              # SOC noch nicht am Ziel
                      and fBatt_SOC > ep_reserve + 5.0           # Sicherheitsabstand
                      and hours_remaining_chk <= pd_max_h         # Noch im Zeitfenster
                      and hours_remaining_chk > 0):               # Ladestart noch nicht vorbei
                    pd_state = 'active'
                    pd_start_soc = fBatt_SOC
                    pd_start_ts = t0
                    pd_consumer_wait_since = 0.0
                    log.info('[%s] PRE-DISCH Wiederaufnahme nach Neustart: SOC=%.1f%% Ziel=%.1f%% '
                             'Ladestart=%s (%.1fh, PV=%.0fW)' % (
                             dt.strftime('%H:%M'), fBatt_SOC, pd_target_soc,
                             datetime.datetime.fromtimestamp(pd_ladestart).strftime('%H:%M'),
                             hours_remaining_chk, iPower_PV))

            # AKTIV: Dynamische Entladeleistung - Punktlandung auf pd_target_soc
            if pd_state == 'active':
                hours_remaining = max(0.25, (pd_ladestart - t0) / 3600.0)
                capacity_wh = 34000.0  # Fallback
                if pd_start_soc is None or pd_start_ts is None:
                    pd_start_soc = fBatt_SOC
                    pd_start_ts = t0
                _pd_total_s = max(300.0, pd_ladestart - pd_start_ts)
                _pd_elapsed_s = min(_pd_total_s, max(0.0, t0 - pd_start_ts))
                _pd_progress = _pd_elapsed_s / _pd_total_s
                # Eigene Entladerampe: nicht die normale Ladezielkurve verwenden.
                trajectory_soc = pd_start_soc + (pd_target_soc - pd_start_soc) * _pd_progress
                pd_eco_dump_today = False
                pd_plan_today = False
                try:
                    _pdplan = json.load(open(PLAN_F,'r',encoding='utf-8'))
                    capacity_wh = float(_pdplan.get('battery_capacity', capacity_wh))
                    pd_eco_dump_today = (_pdplan.get('eco_dump_date') == dt.date().isoformat())
                    _pd_ladestart_ts = float(_pdplan.get('ladestart_ts') or 0.0)
                    if _pd_ladestart_ts > 0:
                        pd_plan_today = (datetime.datetime.fromtimestamp(_pd_ladestart_ts / 1000.0).date() == dt.date())
                except: pass

                pd_allow = dict(pd_allow_cfg)
                pd_consumer_allowed = any(pd_allow.values())

                # WALLBOX-PAUSE: Ohne explizite Pre-Dump-Freigabe bleibt die
                # alte Schutzlogik aktiv. Ist die WB freigegeben, darf sie als
                # lokaler Verbraucher mitlaufen und der Pre-Dump wird nicht
                # zyklisch weggenommen.
                _pd_live_wb_w = abs(float(live.get('power_wb_w', live.get('pvi_power_w',0)) or 0))
                fPower_WB = max(abs(float(fPower_WB or 0.0)), _pd_live_wb_w)
                wb_active = fPower_WB > 500
                _pd_live_wp_w = abs(float(live.get('WP_Power', 0) or 0))
                _pd_live_heater_w = abs(float(live.get('heizstab_power',
                                                       live.get('Heizstab_Power', 0)) or 0))
                pd_grid_guard_w = int(gf(cfg, 'predump_grid_guard_w', 800))
                pd_wallbox_active = bool(pd_allow.get('wallbox') and wb_active)
                pd_heatpump_active = bool(pd_allow.get('heatpump') and _pd_live_wp_w > 300)
                pd_heater_active = bool(pd_allow.get('heater') and _pd_live_heater_w > 300)
                pd_local_consumer_active = (
                    pd_wallbox_active or pd_heatpump_active or pd_heater_active
                )

                def _pd_pause_control(reason, phase, hold_storage=False):
                    nonlocal pd_grid_guard_hold_w, pd_grid_guard_hold_until
                    grid_deadband_w = int(gf(cfg, 'predump_pause_grid_guard_w', 120))
                    grid_import_w = max(0, int(fPower_Grid) - grid_deadband_w)
                    floor_soc = max(float(ep_reserve) + 0.5, float(pd_target_soc or ep_reserve) + 0.5)
                    if grid_import_w > 0 and fBatt_SOC > floor_soc:
                        discharge_w = int(min(maximumLadeleistung * 0.95, grid_import_w + 350))
                        pd_grid_guard_hold_w = max(int(pd_grid_guard_hold_w), discharge_w)
                        pd_grid_guard_hold_until = max(float(pd_grid_guard_hold_until), t0 + 45.0)
                        guard_reason = (reason + ' | Grid-Waechter: Netzbezug %.0fW -> DISCH %dW') % (
                            fPower_Grid, discharge_w)
                        ctrl.send(MODE_DISCH, discharge_w, force=True)
                        log.info('[%s] PRE-DISCH Pause-Grid-Waechter (%s): Netzbezug %.0fW -> DISCH %dW' % (
                            dt.strftime('%H:%M'), phase, fPower_Grid, discharge_w))
                        return MODE_DISCH, discharge_w, guard_reason
                    if pd_grid_guard_hold_until > t0 and pd_grid_guard_hold_w > 0 and fBatt_SOC > floor_soc:
                        discharge_w = int(min(maximumLadeleistung * 0.95, pd_grid_guard_hold_w))
                        guard_reason = (reason + ' | Grid-Waechter-Halt: DISCH %dW noch %.0fs') % (
                            discharge_w, max(0.0, pd_grid_guard_hold_until - t0))
                        ctrl.send(MODE_DISCH, discharge_w, force=force)
                        return MODE_DISCH, discharge_w, guard_reason
                    if hold_storage and fBatt_SOC > floor_soc and (pd_ladestart is None or t0 < pd_ladestart):
                        ctrl.send(MODE_IDLE, 0, force=force)
                        return MODE_IDLE, 0, reason + ' | Speicher-Ladepause bis Ladestart'
                    ctrl.send(MODE_AUTO, maximumLadeleistung, force=(force or fPower_Grid > grid_deadband_w))
                    return MODE_AUTO, int(maximumLadeleistung), reason

                if wb_active and not pd_allow.get('wallbox'):
                    _pd_pause_reason = '[%s] PRE-DISCH pausiert: WB laedt %.0fW (Auto entlaedt Batterie natural)' % (
                             dt.strftime('%H:%M'), fPower_WB)
                    log.info(_pd_pause_reason)
                    _pd_mode, _pd_val, _pd_pause_reason = _pd_pause_control(_pd_pause_reason, 'wb_pause')
                    write_predump_consumer_plan(False, reason=_pd_pause_reason)
                    write_state({'state':'pre_discharge','phase':'wb_pause',
                                 'reason':_pd_pause_reason,'mode':_pd_mode,
                                 'val':int(_pd_val),'soc':fBatt_SOC,
                                 'target_soc':pd_target_soc,'traj_soc':round(trajectory_soc,1),
                                 'wb_w':round(fPower_WB),'hours_remaining':round(hours_remaining,2),
                                 'pv_w':iPower_PV,'grid_w':int(fPower_Grid)})
                    write_wb_budget({
                        'budget_w': 0,
                        'iAVal_w': 0,
                        'iFc_w': 0.0,
                        'iMinLade_w': 0.0,
                        'state': 'pre_discharge_pause',
                        'storage_state': 'pre_discharge',
                        'reason': _pd_pause_reason[:100],
                        'energy_score': {
                            'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                            'free_for_limbs_w': 0,
                            'bat_charge_request_w': 0,
                            'prio_factor': 0.0,
                            'prio_reason': 'pre_discharge_wb_pause',
                        }
                    })
                    time.sleep(CYCLE_S); continue

                # MORGENPUFFER-GUARD: Ein alter oder sehr tiefer Pre-Dump-Plan
                # darf die am Morgen gewuenschte Startreserve nicht wegtakten.
                if pd_morning_guard and fBatt_SOC <= pd_morning_guard_soc:
                    _pd_pause_reason = ('[%s] PRE-DISCH pausiert: Morgenpuffer SOC=%.1f%% <= %.1f%%+%.1f%% '
                                        '(Plan-Ziel %.1f%%, Ladestart=%s)') % (
                             dt.strftime('%H:%M'), fBatt_SOC, morning_target, pd_morning_hyst,
                             pd_target_soc,
                             datetime.datetime.fromtimestamp(pd_ladestart).strftime('%H:%M'))
                    log.info(_pd_pause_reason)
                    _pd_mode, _pd_val, _pd_pause_reason = _pd_pause_control(_pd_pause_reason, 'morning_pause')
                    write_predump_consumer_plan(False, reason=_pd_pause_reason)
                    write_state({'state':'pre_discharge','phase':'morning_pause',
                                 'reason':_pd_pause_reason,'mode':_pd_mode,
                                 'val':int(_pd_val),'soc':fBatt_SOC,
                                 'target_soc':pd_target_soc,'traj_soc':round(trajectory_soc,1),
                                 'morning_target_soc':morning_target,
                                 'pd_morning_hyst':pd_morning_hyst,
                                 'wb_w':round(fPower_WB),'hours_remaining':round(hours_remaining,2),
                                 'pv_w':iPower_PV,'grid_w':int(fPower_Grid)})
                    write_wb_budget({
                        'budget_w': 0,
                        'iAVal_w': 0,
                        'iFc_w': 0.0,
                        'iMinLade_w': 0.0,
                        'state': 'pre_discharge_pause',
                        'storage_state': 'pre_discharge',
                        'reason': _pd_pause_reason[:100],
                        'energy_score': {
                            'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                            'free_for_limbs_w': 0,
                            'bat_charge_request_w': 0,
                            'prio_factor': 0.0,
                            'prio_reason': 'pre_discharge_morning_pause',
                        }
                    })
                    time.sleep(CYCLE_S); continue

                # SCHLECHTES WETTER / Trajektorie: SoC schon auf Kurs? -> pausieren
                # Wenn Simulator schlechtes Wetter erkennt, sinkt pd_target_soc
                # -> kein Pre-Discharge noetig
                if (not pd_consumer_allowed) and pd_target_soc is not None and fBatt_SOC <= pd_target_soc + 2.0:
                    _pd_pause_reason = ('[%s] PRE-DISCH pausiert: SOC=%.1f%% nahe Ziel=%.1f%% '
                                        '(Prognose-Update oder auf Kurs)') % (
                             dt.strftime('%H:%M'), fBatt_SOC, pd_target_soc)
                    log.info(_pd_pause_reason)
                    _pd_mode, _pd_val, _pd_pause_reason = _pd_pause_control(_pd_pause_reason, 'target_pause', hold_storage=True)
                    write_predump_consumer_plan(False, reason=_pd_pause_reason)
                    write_state({'state':'pre_discharge','phase':'target_pause',
                                 'reason':_pd_pause_reason,'mode':_pd_mode,
                                 'val':int(_pd_val),'soc':fBatt_SOC,
                                 'target_soc':pd_target_soc,'traj_soc':round(trajectory_soc,1),
                                 'wb_w':round(fPower_WB),'hours_remaining':round(hours_remaining,2),
                                 'pv_w':iPower_PV,'grid_w':int(fPower_Grid)})
                    write_wb_budget({
                        'budget_w': 0,
                        'iAVal_w': 0,
                        'iFc_w': 0.0,
                        'iMinLade_w': 0.0,
                        'state': 'pre_discharge_pause',
                        'storage_state': 'pre_discharge',
                        'reason': _pd_pause_reason[:100],
                        'energy_score': {
                            'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                            'free_for_limbs_w': 0,
                            'bat_charge_request_w': 0,
                            'prio_factor': 0.0,
                            'prio_reason': 'pre_discharge_target_pause',
                        }
                    })
                    time.sleep(CYCLE_S); continue

                # Trajektorie-Pause: nur pausieren, wenn der SoC mit Hysterese
                # unter der eigenen Pre-Discharge-Rampe liegt.
                _pv_soc_correction = max(0.0, (iPower_PV - 1000) / (capacity_wh * 10.0)) if iPower_PV > 1000 else 0.0
                _traj_korr = trajectory_soc + _pv_soc_correction
                pd_traj_hyst = gf(cfg, 'pd_traj_hyst', 0.7)
                if (
                    (not pd_consumer_allowed)
                    and trajectory_soc > pd_target_soc + 2.0
                    and fBatt_SOC < (_traj_korr - pd_traj_hyst)
                ):
                    _pd_pause_reason = ('[%s] PRE-DISCH pausiert: SOC=%.1f%% < Rampe=%.1f%%-%.1f%% '
                             '(Ziel=%.1f%% in %.1fh, PV=%.0fW Korr=+%.2f%%)') % (
                             dt.strftime('%H:%M'), fBatt_SOC, trajectory_soc, pd_traj_hyst,
                             pd_target_soc, hours_remaining, iPower_PV, _pv_soc_correction)
                    log.info(_pd_pause_reason)
                    _pd_mode, _pd_val, _pd_pause_reason = _pd_pause_control(_pd_pause_reason, 'ramp_pause', hold_storage=True)
                    write_predump_consumer_plan(False, reason=_pd_pause_reason)
                    write_state({'state':'pre_discharge','phase':'ramp_pause',
                                 'reason':_pd_pause_reason,'mode':_pd_mode,
                                 'val':int(_pd_val),'soc':fBatt_SOC,
                                 'target_soc':pd_target_soc,'traj_soc':round(trajectory_soc,1),
                                 'wb_w':round(fPower_WB),'hours_remaining':round(hours_remaining,2),
                                 'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                                 'pd_traj_hyst':pd_traj_hyst})
                    write_wb_budget({
                        'budget_w': 0,
                        'iAVal_w': 0,
                        'iFc_w': 0.0,
                        'iMinLade_w': 0.0,
                        'state': 'pre_discharge_pause',
                        'storage_state': 'pre_discharge',
                        'reason': _pd_pause_reason[:100],
                        'energy_score': {
                            'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                            'free_for_limbs_w': 0,
                            'bat_charge_request_w': 0,
                            'prio_factor': 0.0,
                            'prio_reason': 'pre_discharge_ramp_pause',
                        }
                    })
                    time.sleep(CYCLE_S); continue

                # Energie-Berechnung fuer Punktlandung
                wh_to_dump = max(0.0, (fBatt_SOC - pd_target_soc) * capacity_wh / 100.0)
                required_w = wh_to_dump / hours_remaining

                # KRITISCH: Um Netzbezug zu vermeiden, MUSS die Entladeleistung immer
                # MINDESTENS den aktuellen Hausverbrauch (abzueglich PV) decken!
                # MODE_DISCH ueberschreibt die E3DC Automatik -> Batterie liefert dann exakt disch_w.
                # Ist disch_w < Hausverbrauch, wird der Rest aus dem Netz bezogen!
                pd_local_load_w = iPowerHome + int(wp_w)
                if pd_allow.get('wallbox'):
                    pd_local_load_w += int(fPower_WB)
                if pd_allow.get('heater'):
                    pd_local_load_w += int(_pd_live_heater_w)
                natural_discharge_w = max(0, pd_local_load_w - iPower_PV)

                disch_w_base = max(required_w, natural_discharge_w, 300)

                # PV-Kompensation: Wenn wir zusaetzlich ins Netz dumpen wollen, waehrend starkes PV
                # laeuft, muessen wir den PV-Ueberschuss addieren, da E3DC diesen sonst
                # ignorieren und nur den eingestellten Batteriestrom liefern wuerde.
                pv_kompensation = 0
                if required_w > natural_discharge_w and iPower_PV > pd_local_load_w:
                    pv_kompensation = iPower_PV - pd_local_load_w

                disch_w = int(min(disch_w_base + pv_kompensation, maximumLadeleistung * 0.8))

                if pv_kompensation > 100 or natural_discharge_w > required_w:
                    log.debug('[%s] PRE-DISCH Calc: req=%dW nat=%dW pv_komp=%dW -> disch_w=%dW' % (
                              dt.strftime('%H:%M'), required_w, natural_discharge_w, pv_kompensation, disch_w))

                # --- Eco-Score: Netzstabilitaet beachten ---
                # Bei hohem Score (Strom billig, Netz voll mit Wind/Solar) Pre-Dump pausieren/drosseln,
                # da Einspeisen jetzt unlukrativ und netzschaedlich ist.
                # Wir entladen (DUMP) gezielt bei NIEDRIGEM Score (Strom teuer, Netz braucht Energie).
                eco_s       = read_eco_score()
                pd_eco_min  = gf(cfg, 'pd_eco_min', 25.0)  # Ab hier (und tiefer) = Vollgas Dump
                pd_eco_max  = gf(cfg, 'pd_eco_max', 60.0)  # Ab hier (und hoeher) = Dump pausiert
                eco_factor  = 1.0
                if eco_s > pd_eco_max:
                    pd_local_consumer_active = (
                        (pd_allow.get('wallbox') and wb_active)
                        or (pd_allow.get('heatpump') and _pd_live_wp_w > 300)
                        or (pd_allow.get('heater') and _pd_live_heater_w > 300)
                    )
                    _eco_override_reason = None
                    if pd_eco_dump_today and wh_to_dump > 200.0:
                        _eco_override_reason = 'Eco-Dump-Fenster'
                    elif pd_local_consumer_active and wh_to_dump > 200.0:
                        _eco_override_reason = 'lokaler Verbraucher aktiv'

                    if _eco_override_reason:
                        log.info('[%s] PRE-DISCH EcoScore-Override: Score=%.0f > %.0f, '
                                 '%s und %.0fWh bis Ziel fehlen -> %dW' % (
                                 dt.strftime('%H:%M'), eco_s, pd_eco_max,
                                 _eco_override_reason, wh_to_dump, disch_w))
                    else:
                        log.info('[%s] PRE-DISCH pausiert: EcoScore=%.0f > %.0f (Strom zu billig zum Einspeisen)' % (
                            dt.strftime('%H:%M'), eco_s, pd_eco_max))
                        _pd_mode, _pd_val, _eco_reason = _pd_pause_control('EcoScore-Pause', 'eco_pause', hold_storage=True)
                        write_predump_consumer_plan(False, reason='EcoScore-Pause')
                        write_state({'state':'pre_discharge','phase':'eco_pause',
                                     'reason':_eco_reason + ' (Score=%.0f > %.0f, Strom billig)' % (eco_s, pd_eco_max),
                                     'mode':_pd_mode,'val':int(_pd_val),'soc':fBatt_SOC,
                                     'target_soc':pd_target_soc,'eco_score':eco_s,
                                     'pv_w':iPower_PV,'grid_w':int(fPower_Grid)})
                        write_wb_budget({
                            'budget_w': 0,
                            'iAVal_w': 0,
                            'iFc_w': 0.0,
                            'iMinLade_w': 0.0,
                            'state': 'pre_discharge_pause',
                            'storage_state': 'pre_discharge',
                            'reason': 'EcoScore-Pause',
                            'eco_score': eco_s,
                            'energy_score': {
                                'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                                'free_for_limbs_w': 0,
                                'bat_charge_request_w': 0,
                                'prio_factor': 0.0,
                                'prio_reason': 'pre_discharge_eco_pause',
                            }
                        })
                        time.sleep(CYCLE_S); continue
                elif eco_s > pd_eco_min:
                    # Gedrosselt: linear zwischen pd_eco_min (Vollgas=1.0) und pd_eco_max (Pause=0.0)
                    eco_factor = (pd_eco_max - eco_s) / max(1.0, pd_eco_max - pd_eco_min)
                    disch_w = max(300, int(disch_w * eco_factor))
                    log.info('[%s] PRE-DISCH gedrosselt: EcoScore=%.0f -> Faktor=%.2f -> %dW' % (
                             dt.strftime('%H:%M'), eco_s, eco_factor, disch_w))

                # Grid-Waechter: Eco-Drossel darf keinen Netzbezug erzwingen.
                # Import-Spitzen setzen eine kurze Mindest-Entladung, damit
                # WB/WP-Schaltstufen nicht im naechsten Zyklus wieder gegen die
                # Eco-Drossel laufen.
                grid_import_guard_w = max(0, int(fPower_Grid) - 50)
                if grid_import_guard_w > 0:
                    old_disch_w = disch_w
                    disch_w = int(min(maximumLadeleistung * 0.95, disch_w + grid_import_guard_w + 250))
                    pd_grid_guard_hold_w = max(int(pd_grid_guard_hold_w), int(disch_w))
                    pd_grid_guard_hold_until = max(float(pd_grid_guard_hold_until), t0 + 45.0)
                    if disch_w > old_disch_w:
                        log.info('[%s] PRE-DISCH Grid-Waechter: Netzbezug %.0fW -> Entladung %dW -> %dW' % (
                                 dt.strftime('%H:%M'), fPower_Grid, old_disch_w, disch_w))
                elif pd_grid_guard_hold_until > t0 and pd_grid_guard_hold_w > disch_w:
                    old_disch_w = disch_w
                    disch_w = int(min(maximumLadeleistung * 0.95, pd_grid_guard_hold_w))
                    log.info('[%s] PRE-DISCH Grid-Waechter Halt: Entladung %dW -> %dW (noch %.0fs)' % (
                             dt.strftime('%H:%M'), old_disch_w, disch_w,
                             max(0.0, pd_grid_guard_hold_until - t0)))
                elif pd_grid_guard_hold_until <= t0:
                    pd_grid_guard_hold_w = 0

                # Consumer-first mode: Verbraucher bekommen den Pre-Dump zuerst
                # angeboten. Der Ziel-SoC bleibt aber verbindlich; wenn kein
                # lokaler Verbraucher Leistung nimmt oder die Zeit knapp wird,
                # darf kontrolliert ins Netz entladen werden.
                pd_potential_consumer_budget = int(min(
                    maximumLadeleistung * 0.8,
                    max(required_w, disch_w, 0.0)
                ))
                pd_no_grid_cap_w = max(0, int(
                    iPowerHome
                    + int(wp_w)
                    + (int(fPower_WB) if pd_wallbox_active else 0)
                    + (int(_pd_live_heater_w) if pd_heater_active else 0)
                    - iPower_PV
                    + pd_grid_guard_w
                ))
                predump_consumer_wait_s = max(0.0, gf(cfg, 'predump_consumer_wait_s', 180))
                predump_force_grid_before_ladestart_s = max(
                    0.0, gf(cfg, 'predump_force_grid_before_ladestart_s', 900)
                )
                pd_consumer_shortfall = bool(
                    pd_consumer_allowed
                    and pd_local_consumer_active
                    and disch_w > pd_no_grid_cap_w + 250
                )
                pd_waiting_for_consumers = bool(
                    pd_consumer_allowed
                    and (not pd_local_consumer_active or pd_consumer_shortfall)
                )
                if pd_waiting_for_consumers:
                    if pd_consumer_wait_since <= 0:
                        pd_consumer_wait_since = t0
                    pd_consumer_wait_elapsed_s = max(0.0, t0 - pd_consumer_wait_since)
                else:
                    pd_consumer_wait_since = 0.0
                    pd_consumer_wait_elapsed_s = 0.0
                pd_deadline_s = max(0.0, float(pd_ladestart or t0) - t0)
                pd_grid_fallback = bool(
                    pd_waiting_for_consumers
                    and (
                        pd_consumer_wait_elapsed_s >= predump_consumer_wait_s
                        or pd_deadline_s <= predump_force_grid_before_ladestart_s
                    )
                )

                if pd_consumer_allowed and pd_grid_fallback:
                    log_status_throttled(
                        'predump_grid_fallback',
                        ('[%s] PRE-DISCH Grid-Fallback: Verbraucher nehmen zu wenig '
                         'Leistung (real %.0fW, Cap %dW, Wartezeit %.0fs, '
                         'Ladestart in %.0fs) -> Entladung bis Ziel') % (
                            dt.strftime('%H:%M'),
                            (fPower_WB if pd_wallbox_active else 0)
                            + (_pd_live_wp_w if pd_heatpump_active else 0)
                            + (_pd_live_heater_w if pd_heater_active else 0),
                            pd_no_grid_cap_w,
                            pd_consumer_wait_elapsed_s,
                            pd_deadline_s,
                        ),
                        60,
                    )

                if pd_consumer_allowed and not pd_grid_fallback and not pd_local_consumer_active:
                    pd_wait_budget = max(0, pd_potential_consumer_budget)
                    _wait_left_s = max(0.0, predump_consumer_wait_s - pd_consumer_wait_elapsed_s)
                    pd_wait_reason = (
                        '[%s] PRE-DISCH wartet auf Verbraucher: Budget %dW, '
                        'keine reale WB/WP/Heizstab-Last -> Grid-Fallback in %.0fs'
                    ) % (dt.strftime('%H:%M'), pd_wait_budget, _wait_left_s)
                    _pd_mode, _pd_val, pd_wait_state_reason = _pd_pause_control(
                        pd_wait_reason, 'consumer_wait', hold_storage=True)
                    log_status_throttled('predump_consumer_wait', pd_wait_state_reason, 60)
                    write_predump_consumer_plan(
                        True,
                        allow=pd_allow,
                        budget_w=pd_wait_budget,
                        discharge_w=0,
                        reason=pd_wait_state_reason,
                        target_soc=pd_target_soc,
                    )
                    write_wb_budget({
                        'budget_w':       pd_wait_budget if pd_allow.get('wallbox') else 0,
                        'iAVal_w':        pd_wait_budget if pd_allow.get('wallbox') else 0,
                        'iFc_w':          0.0,
                        'iMinLade_w':     0.0,
                        'state':          'pre_discharge_wait',
                        'storage_state':  'pre_discharge',
                        'predump_active': True,
                        'predump_allow_wallbox': bool(pd_allow.get('wallbox')),
                        'predump_no_grid': True,
                        'predump_grid_fallback': False,
                        'predump_target_soc': round(float(pd_target_soc), 1),
                        'predump_floor_soc': round(float(pd_target_soc), 1),
                        'force_wb_mode':  10 if pd_allow.get('wallbox') else 0,
                        'reason':         pd_wait_state_reason[:100],
                        'eco_score':      eco_s,
                        'energy_score': {
                            'pv_surplus_w':        pd_wait_budget if pd_allow.get('wallbox') else 0,
                            'free_for_limbs_w':    pd_wait_budget if pd_allow.get('wallbox') else 0,
                            'bat_charge_request_w': 0,
                            'prio_factor':         eco_factor,
                            'prio_reason':         'pre_discharge_wait_for_consumer',
                        }
                    })
                    write_state({'state':'pre_discharge','phase':'consumer_wait',
                                 'reason':pd_wait_state_reason,
                                 'mode':_pd_mode,'val':int(_pd_val),
                                 'soc':fBatt_SOC,'target_soc':pd_target_soc,
                                 'traj_soc':round(trajectory_soc,1),
                                 'wb_w':round(fPower_WB),
                                 'wp_w':round(_pd_live_wp_w),
                                 'heater_w':round(_pd_live_heater_w),
                                 'hours_remaining':round(hours_remaining,2),
                                 'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                                 'pd_wb_budget_w':pd_wait_budget if pd_allow.get('wallbox') else 0,
                                 'pd_consumer_budget_w':pd_wait_budget,
                                 'pd_grid_guard_w':pd_grid_guard_w,
                                 'pd_consumer_wait_s':round(pd_consumer_wait_elapsed_s,1),
                                 'predump_allow':pd_allow,'eco_score':eco_s})
                    time.sleep(CYCLE_S); continue

                if pd_consumer_allowed and not pd_grid_fallback:
                    if disch_w > pd_no_grid_cap_w:
                        log.info('[%s] PRE-DISCH No-Grid-Cap: Entladung %dW -> %dW '
                                 '(reale Verbraucher %.0fW, Grid %.0fW)' % (
                                     dt.strftime('%H:%M'), disch_w, pd_no_grid_cap_w,
                                     (fPower_WB if pd_wallbox_active else 0)
                                     + (_pd_live_wp_w if pd_heatpump_active else 0)
                                     + (_pd_live_heater_w if pd_heater_active else 0),
                                     fPower_Grid))
                        disch_w = pd_no_grid_cap_w
                    if disch_w < 300:
                        pd_cap_reason = (
                            '[%s] PRE-DISCH pausiert: No-Grid-Cap %dW, Verbraucherlast '
                            'zu klein -> kein Netzdump'
                        ) % (dt.strftime('%H:%M'), pd_no_grid_cap_w)
                        _pd_mode, _pd_val, pd_cap_state_reason = _pd_pause_control(
                            pd_cap_reason, 'no_grid_pause', hold_storage=True)
                        log_status_throttled('predump_no_grid_pause', pd_cap_state_reason, 60)
                        write_predump_consumer_plan(True, allow=pd_allow,
                                                    budget_w=pd_potential_consumer_budget,
                                                    discharge_w=0,
                                                    reason=pd_cap_state_reason,
                                                    target_soc=pd_target_soc)
                        write_wb_budget({
                            'budget_w':       pd_potential_consumer_budget if pd_allow.get('wallbox') else 0,
                            'iAVal_w':        pd_potential_consumer_budget if pd_allow.get('wallbox') else 0,
                            'iFc_w':          0.0,
                            'iMinLade_w':     0.0,
                            'state':          'pre_discharge_wait',
                            'storage_state':  'pre_discharge',
                            'predump_active': True,
                            'predump_allow_wallbox': bool(pd_allow.get('wallbox')),
                            'predump_no_grid': True,
                            'predump_grid_fallback': False,
                            'predump_target_soc': round(float(pd_target_soc), 1),
                            'predump_floor_soc': round(float(pd_target_soc), 1),
                            'force_wb_mode':  10 if pd_allow.get('wallbox') else 0,
                            'reason':         pd_cap_state_reason[:100],
                            'eco_score':      eco_s,
                            'energy_score': {
                                'pv_surplus_w':        pd_potential_consumer_budget if pd_allow.get('wallbox') else 0,
                                'free_for_limbs_w':    pd_potential_consumer_budget if pd_allow.get('wallbox') else 0,
                                'bat_charge_request_w': 0,
                                'prio_factor':         eco_factor,
                                'prio_reason':         'pre_discharge_no_grid_cap',
                            }
                        })
                        write_state({'state':'pre_discharge','phase':'no_grid_pause',
                                     'reason':pd_cap_state_reason,
                                     'mode':_pd_mode,'val':int(_pd_val),
                                     'soc':fBatt_SOC,'target_soc':pd_target_soc,
                                     'traj_soc':round(trajectory_soc,1),
                                     'wb_w':round(fPower_WB),
                                     'hours_remaining':round(hours_remaining,2),
                                     'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                                     'pd_consumer_budget_w':pd_potential_consumer_budget,
                                     'pd_grid_guard_w':pd_grid_guard_w,
                                     'pd_consumer_wait_s':round(pd_consumer_wait_elapsed_s,1),
                                     'predump_allow':pd_allow,'eco_score':eco_s})
                        time.sleep(CYCLE_S); continue

                pd_discharge_budget = max(0, disch_w - iPowerHome - int(wp_w) - int(fPower_WB))
                pd_export_budget = max(0, int(-float(fPower_Grid) + fPower_WB - pd_grid_guard_w))
                pd_consumer_budget = max(0, max(disch_w - iPowerHome, pd_export_budget))
                pd_wb_budget = max(pd_discharge_budget, pd_export_budget) if pd_allow.get('wallbox') else 0

                ctrl.send(MODE_DISCH, disch_w, force=force)
                pd_reason = ('[%s] PRE-DISCH SOC=%.1f%% Traj=%.1f%% Ziel=%.1f%% '
                             'Ladestart=%s %dW (%.1fh, Eco=%.0f)') % (
                    dt.strftime('%H:%M'), fBatt_SOC, trajectory_soc, pd_target_soc,
                    datetime.datetime.fromtimestamp(pd_ladestart).strftime('%H:%M'),
                    disch_w, hours_remaining, eco_s)
                log.info(pd_reason)
                write_predump_consumer_plan(
                    True,
                    allow=pd_allow,
                    budget_w=pd_consumer_budget,
                    discharge_w=disch_w,
                    reason=pd_reason,
                    target_soc=pd_target_soc,
                    no_grid=not pd_grid_fallback,
                )

                # --- WB/WP-Budget: Entladeleistung sinnvoll weitergeben ---
                # Energie die sonst ins Netz laufen wuerde, zuerst in lokale
                # Verbraucher geben. Das folgt der C++-Logik: Haus/WB/WP sind
                # sinnvoller als Einspeisung, aber mit Netzbezug-Reserve.
                write_wb_budget({
                    'budget_w':       pd_wb_budget,
                    'iAVal_w':        pd_wb_budget,
                    'iFc_w':          0.0,
                    'iMinLade_w':     0.0,
                    'state':          'pre_discharge',
                    'storage_state':  'pre_discharge',
                    'predump_active': True,
                    'predump_allow_wallbox': bool(pd_allow.get('wallbox')),
                    'predump_no_grid': not pd_grid_fallback,
                    'predump_grid_fallback': bool(pd_grid_fallback),
                    'predump_target_soc': round(float(pd_target_soc), 1),
                    'predump_floor_soc': round(float(pd_target_soc), 1),
                    'force_wb_mode':  10 if pd_allow.get('wallbox') else 0,
                    'reason':         pd_reason[:100],
                    'eco_score':      eco_s,
                    'energy_score': {
                        'pv_surplus_w':        pd_wb_budget,
                        'free_for_limbs_w':    pd_wb_budget,
                        'bat_charge_request_w': 0,
                        'prio_factor':         eco_factor,
                        'prio_reason':         'pre_discharge_wallbox' if pd_allow.get('wallbox') else 'pre_discharge',
                    }
                })

                write_state({'state':'pre_discharge','phase':'entladen',
                             'reason':pd_reason,
                             'mode':MODE_DISCH,'val':disch_w,'soc':fBatt_SOC,
                             'target_soc':pd_target_soc,'traj_soc':round(trajectory_soc,1),
                             'wb_w':round(fPower_WB),'hours_remaining':round(hours_remaining,2),
                             'pv_w':iPower_PV,'grid_w':int(fPower_Grid),
                             'pd_wb_budget_w':pd_wb_budget,
                             'pd_consumer_budget_w':pd_consumer_budget,
                             'pd_export_budget_w':pd_export_budget,
                             'pd_grid_guard_w':pd_grid_guard_w,
                             'pd_consumer_wait_s':round(pd_consumer_wait_elapsed_s,1),
                             'predump_grid_fallback':bool(pd_grid_fallback),
                             'predump_allow':pd_allow,'eco_score':eco_s})
                time.sleep(CYCLE_S); continue


        # ----------------------------------------------------------------
        # Rolling averages (Eba Zeile 4935-4941)
        # fAvBatterie: 30-Zyklen-Avg, fAvBatterie900: 900-Zyklen-Avg
        # ----------------------------------------------------------------
        if iAvBatt_Count<30: iAvBatt_Count+=1
        fAvBatterie=fAvBatterie*(iAvBatt_Count-1)/iAvBatt_Count + float(iPower_Bat)/iAvBatt_Count

        if iAvBatt_Count900<900: iAvBatt_Count900+=1
        fAvBatterie900=fAvBatterie900*(iAvBatt_Count900-1)/iAvBatt_Count900 + float(iPower_Bat)/iAvBatt_Count900

        # ----------------------------------------------------------------
        # Trajectory-Clamping: Ladekurve aus target_timeline verfolgen
        # ----------------------------------------------------------------
        # Trajectory-Clamping: Ladekurve aus target_timeline verfolgen
        # Wenn SOC bereits deutlich ueber Soll-Kurve liegt -> Bremse (IDLE)
        # damit E3DC nicht sinnlos ueberlaed und die geplante Kurve verletzt.
        # Schutzmechanismus:
        #   - Nur bei aktivem PV (>500W) damit Nacht/Bewolkung nie gebremst wird
        #   - Niemals waehrend Pre-Discharge (der hat absolute Prioritaet)
        #   - Niemals bei Netzladen (awattar_mode=2)
        #   - Niemals bei EP-Reserve
        #   - Konfigurierbar per tl_enable=0 zum Deaktivieren
        # WICHTIG: tl_soc_now/target/active sind PERSISTENT (nicht per Zyklus ruecksetzen!)
        # Bei Plan-Lesefehler (Race Condition) bleiben letzte Werte erhalten -> Bremse bleibt aktiv.
        # ----------------------------------------------------------------
        strict_legacy  = int(gf(cfg, 'eba_strict_legacy_mode', 1))
        # Die TL-Kurve ist nur das dynamische Ladeende fuer die Eba-Formel.
        # Sie darf auch im strikten Legacy-Modus laufen, sonst kommen Wetter/ML-
        # Updates aus storage_simulator.py nie in der Regelung an.
        tl_enable      = int(gf(cfg, 'tl_enable', 1))
        _mid_target_cfg = gf(cfg, 'storage_mid_target_soc', 0.0)
        _noon_target_cfg = gf(cfg, 'storage_noon_target_soc', 0.0)
        if tl_enable == 0 and max(_mid_target_cfg, _noon_target_cfg) > 0.0:
            # Ein Zwischenziel ohne TL-Fuehrung ist wirkungslos und fuehrt zu
            # Freilauf/Volladen. Alte Configs werden hier zur Laufzeit geheilt.
            tl_enable = 1
            if not _tl_forced_by_noon_logged:
                log.warning(
                    '[TL] tl_enable=0, aber Zwischenziel %.1f%% aktiv - '
                    'Ladekurvenfuehrung wird fuer dieses Zwischenziel erzwungen.' %
                    max(_mid_target_cfg, _noon_target_cfg)
                )
                _tl_forced_by_noon_logged = True
        # Eba-nahe Fuehrung: target_timeline liefert das dynamische iFc-Zwischenziel.
        # Die harte TL-Bremse ist nur noch Notbremse, nicht Alltagsregler.
        tl_tolerance = max(0.0, gf(cfg, 'tl_tolerance_pct', 3.0))
        _tl_emergency_tolerance = max(
            tl_tolerance,
            gf(cfg, 'tl_emergency_tolerance_pct', 30.0)
        )
        # Normale Kurvenbremse folgt der konfigurierten Toleranz. Die groessere
        # Notfalltoleranz bleibt Diagnose/Reserve und darf den Alltagsregler nicht
        # auf 30% aufweiten.
        tl_lookahead_h = gf(cfg, 'tl_lookahead_h',  2.0)    # h Vorausschau fuer iFc-Zwischenziel
        tl_grid_limit_w = gf(cfg, 'tl_grid_limit_w', 100.0)  # Netzbezug hebt Notbremse nach Hysterese auf
        tl_auto_quiet_enable = int(gf(cfg, 'tl_auto_quiet_enable', 1))
        tl_auto_quiet_hold_s = max(20.0, float(gf(cfg, 'tl_auto_quiet_hold_s', 75.0)))
        tl_auto_quiet_margin_w = max(150, int(gf(cfg, 'tl_auto_quiet_margin_w', 350)))
        tl_auto_quiet_min_chrg_w = max(300, int(gf(cfg, 'tl_auto_quiet_min_chrg_w', 600)))
        tl_soft_tolerance = gf(cfg, 'tl_soft_tolerance_pct', 0.5)
        tl_soft_release_pct = max(tl_soft_tolerance + 0.2,
                                  gf(cfg, 'tl_soft_release_pct', tl_soft_tolerance + 0.5))
        tl_autodump_enable = int(gf(cfg, 'tl_autodump_enable', 1))
        # C++-nahe Beruhigung: Active-Dump ist nur noch eine grobe
        # Ausnahme, kein enger Kurvenregler. Kleine Abweichungen erledigen
        # Hausverbrauch und AUTO deutlich ruhiger.
        tl_autodump_start_pct = max(10.0, gf(cfg, 'tl_autodump_start_pct', 10.0))
        tl_autodump_release_pct = min(
            tl_autodump_start_pct,
            max(5.0, gf(cfg, 'tl_autodump_release_pct', 7.0))
        )
        tl_autodump_horizon_h = max(0.25, gf(cfg, 'tl_autodump_horizon_h', 1.0))
        tl_autodump_min_w = max(300, int(gf(cfg, 'tl_autodump_min_w', 300)))
        tl_plan_unreachable = False

        def _curve_wb_relief_budget(curve_ref_soc=None):
            """Gibt WB-Budget frei, wenn der Speicher bewusst ueber der TL-Kurve liegt."""
            try:
                if pv_collapse_active:
                    return {
                        'active': False,
                        'budget_w': 0,
                        'pv_collapse_active': True,
                        'forecast_pv_w': int(forecast_pv_now_w),
                        'pv_collapse_ratio': round(pv_collapse_ratio, 3),
                    }

                _modes = [normalize_wb_mode(cfg.get(f'wb{i}_mode', 0)) for i in [1, 2]]
                if wb_intent_fresh and wb_intent_car_active:
                    _modes.append(normalize_wb_mode(wb_intent_mode))
                if not any(m != MODE_OFF for m in _modes):
                    return {'active': False, 'budget_w': 0}

                _curve_ref = float(curve_ref_soc if curve_ref_soc is not None else (
                    tl_soc_now if tl_soc_now is not None else fLadeende
                ))
                _excess_pct = max(0.0, float(fBatt_SOC) - _curve_ref)
                _start_pct = max(0.2, float(gf(cfg, 'tl_curve_wb_relief_start_pct',
                                               max(0.8, tl_soft_tolerance))))
                if _excess_pct < _start_pct or fBatt_SOC <= ep_reserve + 0.5:
                    return {
                        'active': False,
                        'budget_w': 0,
                        'curve_ref_soc': round(_curve_ref, 2),
                        'curve_excess_pct': round(_excess_pct, 2),
                    }

                _capacity_wh = max(1000.0, float(speichergroesse) * 1000.0)
                _horizon_h = max(0.25, float(gf(cfg, 'tl_curve_wb_relief_horizon_h', 2.0)))
                _usable_pct = max(0.0, _excess_pct - min(tl_soft_tolerance, _start_pct))
                _curve_w = int(_usable_pct * _capacity_wh / 100.0 / _horizon_h)
                _pv_free_w = max(0, int(iPower_PV - iPowerHome_budget - int(wp_w) - int(fPower_WB)))
                _export_room_w = max(0, int(-float(fPower_Grid_ctrl) - 150.0))
                _max_discharge = max(0, int(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung)))
                _budget_w = min(_max_discharge, max(_pv_free_w, _export_room_w, _curve_w))
                _min_w = max(250, int(gf(cfg, 'tl_curve_wb_relief_min_w', 300)))
                if _budget_w < _min_w:
                    _budget_w = 0

                return {
                    'active': _budget_w > 0,
                    'budget_w': max(0, int(_budget_w)),
                    'curve_ref_soc': round(_curve_ref, 2),
                    'curve_excess_pct': round(_excess_pct, 2),
                    'curve_relief_from_soc_w': max(0, int(_curve_w)),
                    'pv_free_w': max(0, int(_pv_free_w)),
                    'export_room_w': max(0, int(_export_room_w)),
                }
            except Exception:
                return {'active': False, 'budget_w': 0}

        def _curve_relief_has_consumer():
            """True wenn die Kurvenentlastung eine reale oder gerade freigegebene WB bedienen kann."""
            try:
                return bool(
                    wb_measured_for_storage
                    or wb_intent_set_amp > 0
                    or abs(float(fPower_WB or 0.0)) > 500
                )
            except Exception:
                return False

        def _curve_relief_has_real_consumer():
            """True nur bei gemessener Verbraucherleistung, nicht bei reiner Startfreigabe."""
            try:
                return bool(
                    wb_measured_for_storage
                    or abs(float(fPower_WB or 0.0)) > 500
                )
            except Exception:
                return False

        def _curve_brake_should_auto_release(relief=None):
            """Bei Wetter-/Lastspruengen ist AUTO ruhiger als ein hartes IDLE."""
            try:
                _relief = relief or {}
                _min_start_w = max(1200.0, 6.0 * 230.0 * max(1, int(wb_intent_phases or 1)))
                return bool(
                    pv_collapse_active
                    or
                    fPower_Grid_ctrl > tl_grid_limit_w
                    or (iPower_PV < _min_start_w and (_relief.get('active') or wb_intent_set_amp > 0))
                )
            except Exception:
                return bool(fPower_Grid_ctrl > tl_grid_limit_w)

        def _grid_guard_discharge_w(reserve_w=350.0):
            """DISCH-Limit fuer ruhigen Netzpunkt: aktuelle Entladung plus Netzbezug."""
            try:
                _max_discharge = max(0, int(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung)))
                _current_discharge_w = max(0.0, -float(iPower_Bat or 0.0))
                _grid_import_w = max(0.0, float(fPower_Grid_ctrl or 0.0))
                return int(min(_max_discharge, max(0.0, _current_discharge_w + _grid_import_w + reserve_w)))
            except Exception:
                return 0

        if tl_enable:
            try:
                _plan_tl = json.load(open(PLAN_F, 'r', encoding='utf-8'))
                tl_plan_unreachable = (
                    _plan_tl.get('can_reach_target') is False
                    or bool((_plan_tl.get('target_curve_meta') or {}).get('target_capped_unreachable'))
                )
                _new_soc_now, _new_soc_target, _new_ts_target = get_tl_target(
                    _plan_tl, t0, tl_lookahead_h)
                # Nur uebernehmen wenn Plan valide gelesen (nicht None zuruecksetzen!)
                if _new_soc_now is not None and _new_soc_target is not None:
                    if wb_curve_target_soc is not None:
                        _new_soc_now = min(float(_new_soc_now), float(wb_curve_target_soc))
                        _new_soc_target = min(float(_new_soc_target), float(wb_curve_target_soc))
                    tl_soc_now   = _new_soc_now
                    tl_soc_target= _new_soc_target
                    tl_ts_target = _new_ts_target
                    tl_active    = True
                elif tl_active:
                    log.debug('[TL] Plan-Slots alle vergangen - behalte letzte Werte soc_now=%.1f%%' % tl_soc_now)
            except Exception as _tl_err:
                log.warning('[TL] Plan-Lesefehler (behalte letzte Werte): %s' % _tl_err)
        else:
            tl_active = False

        # Abregel-Druck frueh erkennen: TL-Bremse darf nicht greifen, wenn
        # Einspeiselimit/Derating gerade Speicherladung erzwingen sollte.
        _ab_puffer    = int(gf(cfg, 'abregel_puffer_w', 300))
        _ab_hysterese = int(gf(cfg, 'abregel_hysterese_w', 2000))
        _ab_threshold = -(einspeiselimit_w - _ab_puffer)
        _ab_release   = _ab_threshold + _ab_hysterese
        _abregel_pressure = (
            einspeiselimit_w > 0
            and iPower_PV > 500
            and fBatt_SOC < 99.5
            and awattar_mode not in (0, 2)
            and (
                fPower_Grid < _ab_threshold
                or (_abregel_was_aktiv and fPower_Grid < _ab_release)
            )
        )

        # Tagesziel erreicht: kein weiteres Halten an der Kurve. C++-nah darf
        # der E3DC ab dem erreichten Ziel autonom Hausverbrauch und Vollzustand
        # behandeln. Abregelschutz, Preis-/Netzladen, Pre-Dump und WB-Sperren
        # bleiben hoeherrangig.
        _day_target_release_hyst = max(0.5, gf(cfg, 'target_reached_release_hyst_pct', 0.5))
        _wb_hold_request = (wb_intent_request == 'hold_discharge' and wb_intent_charging)
        if (tl_active
                and tl_pv_day_active
                and pd_state != 'active'
                and awattar_mode not in (0, 2)
                and not _abregel_pressure
                and not _price_boost_any
                and not _wb_hold_request
                and fBatt_SOC >= day_target_soc + _day_target_release_hyst):
            _tl_softcap_gate = False
            ctrl.set_max_charge_power(0)
            _target_raw_grid_limit = max(350.0, float(tl_grid_limit_w) * 2.0)
            _target_grid_import_w = max(
                0.0,
                float(fPower_Grid_ctrl or 0.0),
                float(fPower_Grid or 0.0) if float(fPower_Grid or 0.0) > _target_raw_grid_limit else 0.0,
            )
            _target_grid_guard = (
                _target_grid_import_w > float(tl_grid_limit_w)
                and fBatt_SOC > ep_reserve + 0.5
            )
            if _target_grid_guard:
                _target_val = int(min(
                    max(0, int(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung))),
                    max(300.0, max(0.0, -float(iPower_Bat or 0.0)) + _target_grid_import_w + 350.0)
                ))
                _target_mode = MODE_DISCH
                _target_state = 'target_grid_guard'
                _target_storage_state = 'target_reached_grid_guard'
                ctrl.send(MODE_DISCH, _target_val, force=True)
                _target_reason = (
                    '[%s] ZIEL-HALTEWAECHTER: SOC=%.1f%% >= Tagesziel %.1f%%+%.1f%%, '
                    'Grid=%.0fW/%.0fW -> DISCH %dW statt IDLE'
                ) % (
                    dt.strftime('%H:%M'), fBatt_SOC, day_target_soc, _day_target_release_hyst,
                    fPower_Grid, fPower_Grid_ctrl, _target_val
                )
            else:
                _target_val = 0
                _target_mode = MODE_IDLE
                _target_state = 'target_hold'
                _target_storage_state = 'target_reached_hold'
                ctrl.send(MODE_IDLE, 0, force=True)
                _target_reason = (
                    '[%s] ZIEL-HALT: SOC=%.1f%% >= Tagesziel %.1f%%+%.1f%% '
                    '-> IDLE, Speicherladung gesperrt; Netzbezug gibt Entladung wieder frei'
                ) % (dt.strftime('%H:%M'), fBatt_SOC, day_target_soc, _day_target_release_hyst)
            log_status_throttled('tl_target_release', _target_reason, 300)
            write_state({'state': _target_state, 'phase': 'Ziel erreicht',
                         'reason': _target_reason,
                         'mode': _target_mode, 'val': _target_val,
                         'soc': fBatt_SOC, 'ladeende': day_target_soc,
                         'end_h': round(day_end_h, 2),
                         'tl_soc_now': tl_soc_now,
                         'tl_soc_target': tl_soc_target,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                         'grid_ema_w': int(fPower_Grid_ctrl),
                         'bat_w': iPower_Bat})
            _target_free_w = max(0, int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)))
            write_wb_budget({
                'budget_w': _target_free_w,
                'iAVal_w': _target_free_w,
                'iFc_w': 0.0,
                'iMinLade_w': 0.0,
                'state': _target_state,
                'storage_state': _target_storage_state,
                'reason': _target_reason[:100],
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': _target_free_w,
                    'bat_charge_request_w': 0,
                    'prio_factor': 1.0,
                    'prio_reason': _target_storage_state,
                }
            })
            time.sleep(CYCLE_S); continue

        if fPower_Grid > tl_grid_limit_w:
            _tl_grid_consec = min(_TL_GRID_CONSEC_LIMIT, _tl_grid_consec + 1)
        else:
            _tl_grid_consec = 0

        # TL-Bremse: SOC ueber Kurve + Toleranz UND PV aktiv (Tages-Betrieb)
        tl_brake_condition = (tl_active
                and pd_state != 'active'          # kein Pre-Discharge
                and awattar_mode != 2             # kein Netzladen
                and not _abregel_pressure          # Abregelschutz hat Vorrang
                and _tl_grid_consec < _TL_GRID_CONSEC_LIMIT  # Grid-Guard: Netzbezug loest Bremse
                and tl_pv_day_active             # nur bei aktivem PV (kein Nacht-/Abend-Bremsen)
                and tl_soc_now is not None
                and fBatt_SOC > (tl_soc_now + tl_tolerance))
        if tl_brake_condition:
            _tl_relief = _curve_wb_relief_budget(tl_soc_now)
            _tl_brake_state = 'tl_brake'
            _tl_brake_mode = MODE_IDLE
            _tl_brake_val = 0
            _tl_relief_has_real_wb = bool(_tl_relief.get('active') and _curve_relief_has_real_consumer())
            if _tl_relief_has_real_wb:
                # Bei Fremd-WB ist der E3DC-Rohhausverbrauch Teil der Regelstrecke.
                # Hartes IDLE/DISCH pro Zyklus erzeugt dann Scheinlastspruenge;
                # AUTO laesst den E3DC den Netzpunkt ruhiger fuehren. Wir geben
                # hier nur das Wallbox-Budget vor.
                _tl_brake_mode = MODE_AUTO
                _tl_brake_val = int(maximumLadeleistung)
                _tl_brake_state = 'tl_curve_auto_relief'
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
            elif _curve_brake_should_auto_release(_tl_relief):
                _tl_brake_mode = MODE_AUTO
                _tl_brake_val = int(maximumLadeleistung)
                _tl_brake_state = 'tl_curve_auto_relief'
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
            else:
                ctrl.send(MODE_IDLE, 0, force=True)
            tl_reason = ('[%s] KURVEN-BREMSE: SOC=%.1f%% > Kurve=%.1f%%+%.1f%% '
                          '(Naechstes Ziel: %.1f%%@%s PV=%.0fW)') % (
                dt.strftime('%H:%M'), fBatt_SOC, tl_soc_now, tl_tolerance,
                tl_soc_target,
                datetime.datetime.fromtimestamp(tl_ts_target).strftime('%H:%M') if tl_ts_target else '?',
                iPower_PV)
            if _tl_relief.get('active'):
                tl_reason += ' [WB-Kurvenentlastung %dW, +%.1f%%]' % (
                    int(_tl_relief.get('budget_w', 0)),
                    float(_tl_relief.get('curve_excess_pct', 0.0))
                )
            if pv_collapse_active:
                tl_reason += ' [PV-Einbruch %.0fW statt %.0fW, %.0f%% -> Wetter-AUTO]' % (
                    iPower_PV, forecast_pv_now_w, pv_collapse_ratio * 100.0)
            if _tl_brake_mode == MODE_AUTO:
                if _tl_relief_has_real_wb:
                    tl_reason += ' [WB-AUTO: Fremd-WB/Netzpunkt zu unruhig fuer hartes IDLE]'
                else:
                    tl_reason += ' [Wetter-AUTO: PV/Netzpunkt zu unruhig fuer hartes IDLE]'
            log.info(tl_reason)
            write_state({'state': _tl_brake_state, 'reason': tl_reason,
                         'mode': _tl_brake_mode, 'val': _tl_brake_val, 'soc': fBatt_SOC,
                         'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                         'tl_ts_target': tl_ts_target,
                         'ladeende': fLadeende, 'end_h': end_h,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid)})
            # Budget schreiben damit PHP kein TIMEOUT zeigt
            # Bei Kurven-Ueberschuss bekommt die WB bewusst Budget, damit die
            # Batterie wieder Richtung Kurve entlastet wird statt sinnlos zu
            # exportieren oder durch ein hartes IDLE Netzbezug zu erzeugen.
            try:
                _pv_surplus = max(0, iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB))
                _relief_budget_w = int(_tl_relief.get('budget_w', 0) or 0)
                _tl_budget = {
                    'budget_w':       _relief_budget_w,
                    'iAVal_w':        _relief_budget_w,
                    'raw_iAVal_w':    _relief_budget_w,
                    'iFc_w':          0.0,
                    'iMinLade_w':     0.0,
                    'state':          _tl_brake_state,
                    'storage_state':  _tl_brake_state,
                    'reason':         tl_reason[:100],
                    'ts':             t0,
                    'curve_wb_relief': bool(_tl_relief.get('active')),
                    'curve_ref_soc': _tl_relief.get('curve_ref_soc'),
                    'curve_excess_pct': _tl_relief.get('curve_excess_pct'),
                    'curve_relief_from_soc_w': _tl_relief.get('curve_relief_from_soc_w', 0),
                    'wb_storage_extra_w': _relief_budget_w,
                    'wb_storage_cap_w': max(0, int(fPower_WB) + _relief_budget_w),
                    'energy_score': {
                        'pv_surplus_w':        _pv_surplus,
                        'free_for_limbs_w':    _relief_budget_w,
                        'bat_charge_request_w': 0,
                        'prio_factor':         1.0,
                        'prio_reason':         'tl_curve_wb_relief' if _tl_relief.get('active') else 'tl_brake',
                    }
                }
                write_wb_budget(_tl_budget)
            except: pass
            iE3DC_Req_Load_alt = -_tl_brake_val if _tl_brake_mode == MODE_DISCH else 0
            time.sleep(CYCLE_S); continue

        # ----------------------------------------------------------------
        # ABREGELSCHUTZ (Prioritaet 3, nach TL-Bremse, vor Eba-Logik)
        # Wenn Einspeisung die konfigurierte Grenze erreicht:
        # -> continue-Block: umgeht iLMStatus-Countdown komplett
        # -> CHRG(3) dynamisch: Zwingt den E3DC, exakt so viel zu laden,
        #    um das Hausnetz am ab_threshold (Einspeiselimit - Puffer) zu halten.
        # Hysterese: bleibt aktiv bis grid_w um abregel_hysterese_w steigt
        # ----------------------------------------------------------------
        _ab_puffer    = int(gf(cfg, 'abregel_puffer_w', 300))
        _ab_hysterese = int(gf(cfg, 'abregel_hysterese_w', 2000))
        _ab_max_charge = max(0, int(maximumLadeleistung))
        _ab_min_charge = min(max(300, int(gf(cfg, 'abregel_min_charge_w', minimumLadeleistung))), _ab_max_charge)
        _ab_threshold = -(einspeiselimit_w - _ab_puffer)         # Aktivierung: z.B. -9700W
        _ab_release   = _ab_threshold + _ab_hysterese            # Freigabe:    z.B. -7700W
        _ab_hold_active = (
            _abregel_was_aktiv
            and t0 < _abregel_hold_until
            and fPower_Grid < -1000
        )
        _abregel_trigger = (
            einspeiselimit_w > 0
            and iPower_PV > 500
            and fBatt_SOC < 99.5
            and pd_state != 'active'
            and awattar_mode not in (0, 2)
            and (
                fPower_Grid < _ab_threshold                           # Erstaktivierung
                or (_abregel_was_aktiv and fPower_Grid < _ab_release) # Hysterese
                or _ab_hold_active                                    # kurzer Halteanker gegen TL-IDLE-Puls
            )
        )
        if _abregel_trigger:
            # Atmende Berechnung: Wie viel muessen wir in die Batterie schieben,
            # um fPower_Grid exakt auf _ab_threshold zu heben?
            _dynamic_charge_raw = int(iPower_Bat - (fPower_Grid - _ab_threshold))
            _dynamic_charge = min(max(_dynamic_charge_raw, _ab_min_charge), _ab_max_charge)
            _min_note = (' [min %dW]' % _ab_min_charge) if _dynamic_charge_raw < _ab_min_charge else ''
            _tl_note = ''
            _ramp_note = ''
            _hold_note = ' [Haltezeit]' if _ab_hold_active else ''
            _tl_deficit_now = 0.0
            _tl_deficit_target = 0.0

            # Wenn der Speicher unter der Ladekurve liegt, darf der
            # Abregelschutz nicht nur "atmend" am Einspeiselimit kleben.
            # Dann wird der verfuegbare PV-Ueberschuss zum Kurvennachlauf
            # genutzt, ohne absichtlich Netzbezug zu erzeugen.
            _tl_catchup_charge = 0
            try:
                _tl_deficit_now = (
                    float(tl_soc_now) - float(fBatt_SOC)
                    if tl_active and tl_soc_now is not None else 0.0
                )
                _tl_deficit_target = (
                    float(tl_soc_target) - float(fBatt_SOC)
                    if tl_active and tl_soc_target is not None else 0.0
                )
                if _tl_deficit_now > tl_soft_tolerance:
                    _export_charge_room = max(
                        _ab_min_charge,
                        int(iPower_Bat - fPower_Grid - 100)
                    )
                    _tl_catchup_charge = min(_ab_max_charge, _export_charge_room)
                    _tl_note = ' [Kurvennachlauf %.1f%%]' % _tl_deficit_now
                elif (_tl_deficit_target > tl_soft_tolerance
                        and tl_ts_target is not None
                        and float(tl_ts_target) > t0):
                    _rest_s = max(300.0, float(tl_ts_target) - t0)
                    _need_w = int(_tl_deficit_target * float(speichergroesse) * 10.0 * 3600.0 / _rest_s)
                    _export_charge_room = max(
                        _ab_min_charge,
                        int(iPower_Bat - fPower_Grid - 100)
                    )
                    _tl_catchup_charge = min(_ab_max_charge, _export_charge_room, max(0, _need_w))
                    if _tl_catchup_charge > 0:
                        _tl_note = ' [Zielnachlauf %.1f%%]' % _tl_deficit_target
                if _tl_catchup_charge > _dynamic_charge:
                    _dynamic_charge = _tl_catchup_charge
            except Exception:
                pass

            # Wenn AUTO bereits laedt und wir unter der Kurve oder in einem
            # nicht erreichbaren Tagesziel liegen, darf eine kurze Abregelspitze
            # den internen E3DC-Regler nicht alle paar Sekunden neu starten.
            # Dann beobachten wir im AUTO-Modus und eskalieren erst bei
            # dauerhafter, deutlicher Rest-Einspeisung in den harten CHRG-Pfad.
            _ab_over_w = max(0, int(_ab_threshold - fPower_Grid))
            _ab_auto_band_w = max(300, int(gf(cfg, 'abregel_auto_band_w', 1800)))
            _ab_auto_grace_s = max(0.0, gf(cfg, 'abregel_auto_grace_s', 30.0))
            _ab_under_curve_auto = bool(
                tl_active
                and (
                    _tl_deficit_now > tl_soft_tolerance
                    or (tl_plan_unreachable and (tl_soc_now is None or fBatt_SOC < float(tl_soc_now)))
                )
            )
            _ab_auto_charging = iPower_Bat >= max(_ab_min_charge, int(_dynamic_charge * 0.55))
            if _ab_under_curve_auto and _ab_auto_charging:
                if _abregel_auto_since <= 0:
                    _abregel_auto_since = t0
                _ab_auto_age_s = max(0.0, t0 - _abregel_auto_since)
                _ab_auto_wait = _ab_auto_age_s < _ab_auto_grace_s
                _ab_auto_band_ok = _ab_over_w <= _ab_auto_band_w
                if _ab_auto_wait or _ab_auto_band_ok:
                    if _abregel_was_aktiv:
                        log.info('[%s] ABREGELSCHUTZ -> AUTO Beobachtung (Grid=%.0fW, Ueberhang=%dW)' % (
                            dt.strftime('%H:%M'), fPower_Grid, _ab_over_w))
                        ctrl.set_max_charge_power(0)
                        ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=False)
                    _abregel_was_aktiv = False
                    _abregel_charge_last = 0
                    _abregel_hold_until = 0.0
                    _ab_reason = (
                        '[%s] AUTO-FREIGABE/ABREGEL-BEOBACHTUNG: Grid=%.0fW, '
                        'Ueberhang=%dW <= %dW oder Wartezeit %.0fs/%.0fs; '
                        'Akku laedt bereits %.0fW -> kein CHRG-Neustart'
                    ) % (
                        dt.strftime('%H:%M'), fPower_Grid, _ab_over_w, _ab_auto_band_w,
                        _ab_auto_age_s, _ab_auto_grace_s, iPower_Bat)
                    log_status_throttled('abregel_auto_observe', _ab_reason, interval_s=60)
                    write_state({'state': 'auto', 'reason': _ab_reason,
                                 'mode': MODE_AUTO, 'val': int(maximumLadeleistung),
                                 'soc': fBatt_SOC, 'ladeende': fLadeende,
                                 'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                                 'tl_ts_target': tl_ts_target,
                                 'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                                 'bat_w': iPower_Bat,
                                 'abregel_auto_observe': True,
                                 'abregel_over_w': _ab_over_w})
                    _auto_free_w = max(0, int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)))
                    write_wb_budget({
                        'budget_w': _auto_free_w,
                        'iAVal_w': _auto_free_w,
                        'iFc_w': 0.0,
                        'iMinLade_w': 0.0,
                        'state': 'auto',
                        'storage_state': 'auto',
                        'reason': _ab_reason[:100],
                        'energy_score': {
                            'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                            'free_for_limbs_w': _auto_free_w,
                            'bat_charge_request_w': 0,
                            'prio_factor': 1.0,
                            'prio_reason': 'abregel_auto_observe',
                        }
                    })
                    time.sleep(CYCLE_S); continue
            else:
                _abregel_auto_since = 0.0

            # Die Messwerte laufen dem RSCP-Befehl um einige Sekunden hinterher.
            # Ohne Rampe springt der Abregelschutz deshalb zwischen Minimalwert
            # und hohem Nachladewert. Solange wir klar exportieren, glätten wir
            # den Sollwert; bei echter Freigabe/Netzbezug faellt der Block unten
            # ohnehin heraus und gibt den E3DC frei.
            if _abregel_charge_last > 0 and fPower_Grid < -1000:
                _ramp_up = max(300, int(gf(cfg, 'abregel_ramp_up_w', 1200)))
                _ramp_down = max(300, int(gf(cfg, 'abregel_ramp_down_w', 700)))
                _before_ramp = _dynamic_charge
                _dynamic_charge = min(
                    max(_dynamic_charge, _abregel_charge_last - _ramp_down),
                    _abregel_charge_last + _ramp_up
                )
                _dynamic_charge = max(_ab_min_charge, min(_ab_max_charge, _dynamic_charge))
                if _dynamic_charge != _before_ramp:
                    _ramp_note = ' [Rampe %d->%dW]' % (_before_ramp, _dynamic_charge)

            # Abregelschutz muss aktiv laden: einige E3DC-Firmwares nehmen den
            # kleinen Ueberschuss bei AUTO+Cap nicht in den Akku und bleiben bei
            # Battery_Power=0. CHRG wird dynamisch aus dem aktuellen Grid-Export
            # berechnet und daher bei PV-Einbruch automatisch reduziert.
            ctrl.set_max_charge_power(_dynamic_charge)
            ctrl.send(MODE_CHRG, _dynamic_charge, force=True)
            _abregel_charge_last = _dynamic_charge
            _abregel_hold_until = max(_abregel_hold_until, t0 + float(gf(cfg, 'abregel_hold_s', 45)))
            _ab_reason = ('[%s] ABREGELSCHUTZ: Grid=%.0fW Limit=-%.0fW -> Atmend: CHRG %dW%s%s%s%s%s') % (
                dt.strftime('%H:%M'), fPower_Grid, einspeiselimit_w, _dynamic_charge,
                _min_note, _tl_note, _ramp_note, _hold_note, ' [Hysterese]' if _abregel_was_aktiv else '')
            log.info(_ab_reason)
            write_state({'state': 'abregelschutz', 'reason': _ab_reason,
                         'mode': MODE_CHRG, 'val': _dynamic_charge, 'soc': fBatt_SOC,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                         'einspeiselimit_w': einspeiselimit_w})
            _ab_free_w = max(0, int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB) - _dynamic_charge))
            write_wb_budget({
                'budget_w': _ab_free_w,
                'iAVal_w': _ab_free_w,
                'iFc_w': 0.0,
                'iMinLade_w': float(_dynamic_charge),
                'state': 'abregelschutz',
                'storage_state': 'abregelschutz',
                'reason': _ab_reason[:100],
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': _ab_free_w,
                    'bat_charge_request_w': int(_dynamic_charge),
                    'prio_factor': 1.0,
                    'prio_reason': 'abregelschutz',
                }
            })
            _abregel_was_aktiv = True
            _abregel_auto_since = 0.0
            iE3DC_Req_Load_alt = 0
            time.sleep(CYCLE_S); continue
        else:
            if _abregel_was_aktiv:
                log.info('[%s] ABREGELSCHUTZ deaktiviert (Grid=%.0fW)' % (dt.strftime('%H:%M'), fPower_Grid))
                ctrl.set_max_charge_power(0)  # Sicherheits-Reset falls vorher genutzt
            _abregel_was_aktiv = False
            _abregel_charge_last = 0
            _abregel_hold_until = 0.0
            _abregel_auto_since = 0.0

        _tl_unreachable_under_curve = False
        _tl_under_curve_auto = False
        if tl_plan_unreachable:
            try:
                if tl_soc_now is not None:
                    _tl_unreachable_under_curve = fBatt_SOC < (float(tl_soc_now) - tl_soft_tolerance)
                else:
                    _tl_unreachable_under_curve = fBatt_SOC < (float(fLadeende) - 0.5)
                if not tl_pv_day_active:
                    _tl_unreachable_under_curve = True
            except Exception:
                _tl_unreachable_under_curve = fBatt_SOC < (float(fLadeende) - 0.5)
        try:
            _tl_under_curve_auto = (
                tl_active and tl_soc_now is not None
                and fBatt_SOC < (float(tl_soc_now) - tl_soft_tolerance)
            )
        except Exception:
            _tl_under_curve_auto = False

        if (tl_active
                and (_tl_under_curve_auto or (tl_plan_unreachable and _tl_unreachable_under_curve))
                and pd_state != 'active'
                and awattar_mode not in (0, 2)
                and not _abregel_pressure):
            _wb_modes_now = [normalize_wb_mode(cfg.get(f'wb{i}_mode', 0)) for i in [1, 2]]
            _wb_curve_needs_storage = (
                any(storage_floor_mode(m) for m in _wb_modes_now)
                and wb_measured_for_storage
            )
            if not _wb_curve_needs_storage:
                ctrl.send(MODE_AUTO, maximumLadeleistung, force=False)
                if not tl_pv_day_active:
                    _auto_reason = (
                        '[%s] AUTO-FREIGABE: Heute keine relevante PV mehr, Tagesziel laut Prognose '
                        'nicht mehr erreichbar -> E3DC Auto, keine Kurvenjagd'
                    ) % dt.strftime('%H:%M')
                elif _tl_under_curve_auto and not tl_plan_unreachable:
                    _auto_reason = (
                        '[%s] AUTO-FREIGABE: SOC=%.1f%% < Sollkurve %.1f%% '
                        '-> E3DC Auto bis Kurve wieder erreicht ist'
                    ) % (dt.strftime('%H:%M'), fBatt_SOC, tl_soc_now or 0.0)
                else:
                    _auto_reason = (
                        '[%s] AUTO-FREIGABE: Tagesziel laut Prognose nicht mehr erreichbar, '
                        'SOC=%.1f%% < Kurve %.1f%% / Ziel %.1f%% -> E3DC Auto, keine Kurvenjagd'
                    ) % (dt.strftime('%H:%M'), fBatt_SOC, tl_soc_now or 0.0, fLadeende)
                _auto_key = 'tl_auto_under_curve' if (_tl_under_curve_auto and not tl_plan_unreachable) else 'tl_auto_unreachable'
                log_status_throttled(_auto_key, _auto_reason)
                write_state({'state': 'auto', 'reason': _auto_reason,
                             'mode': MODE_AUTO, 'val': int(maximumLadeleistung),
                             'soc': fBatt_SOC, 'ladeende': fLadeende,
                             'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                             'tl_ts_target': tl_ts_target,
                             'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                             'bat_w': iPower_Bat})
                _auto_free_w = max(0, int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)))
                write_wb_budget({
                    'budget_w': _auto_free_w,
                    'iAVal_w': _auto_free_w,
                    'iFc_w': 0.0,
                    'iMinLade_w': 0.0,
                    'state': 'auto',
                    'storage_state': 'auto',
                    'reason': _auto_reason[:100],
                    'energy_score': {
                        'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                        'free_for_limbs_w': _auto_free_w,
                        'bat_charge_request_w': 0,
                        'prio_factor': 1.0,
                        'prio_reason': 'tl_auto',
                    }
                })
                time.sleep(CYCLE_S); continue

        # (statt sofort auf 95%@21:45 zu zielen -> sanftere Ladung nach Kurve)
        # Nur wenn Zwischenpunkt VOR dem Gesamtende liegt (sonst unveraendert lassen)
        tl_soc_cap_active = False  # Merker: aktuelles Laden wird unterbunden
        tl_soc_cap_label = 'Zwischenziel'
        tl_soc_cap_soc = tl_soc_target
        tl_soc_cap_ts = tl_ts_target
        tl_softcap_candidate = None
        if (tl_active and tl_ts_target is not None and tl_soc_target is not None
                and pd_state != 'active' and awattar_mode != 2):
            _tl_end_h_target = (datetime.datetime.fromtimestamp(tl_ts_target).hour
                                 + datetime.datetime.fromtimestamp(tl_ts_target).minute / 60.0)
            # Nur uebernehmen wenn Zwischenziel realistisch kleiner als Gesamtziel
            # (verhindert rueckwaerts-Ziele nachts wenn target_timeline auslaeuft)
            if _tl_end_h_target < end_h and tl_soc_target < fLadeende and _tl_end_h_target > now_h:
                end_h         = _tl_end_h_target
                fLadeende     = tl_soc_target
                tLadezeitende = int(_tl_end_h_target * 3600)
                log.debug('[%s] TL-Zwischenziel: %.1f%%@%.2fh (Gesamt: %.1f%%@%.2fh)' % (
                    dt.strftime('%H:%M'), fLadeende, end_h,
                    gf(cfg,'storage_target_soc',95), end_h))
                # Soft-Cap: Wenn SOC schon ueber dem Zwischenziel liegt,
                # blockiere jede weitere Ladung bis TL-Bremse wieder greift.
                # Verhindert Oszillieren: Bremse loest aus -> Eba laed sofort
                # wieder voll -> Bremse loest wieder aus -> Loop!
                if tl_pv_day_active and _tl_grid_consec < _TL_GRID_CONSEC_LIMIT:
                    tl_softcap_candidate = ('Zwischenziel', tl_soc_target, tl_ts_target, 0.0)

            # Aktuelle Kurve hat Vorrang vor dem 2h-Ausblick:
            # Der Lookahead berechnet die sanfte Ladeleistung, darf aber nicht
            # erzwingen, dass ein Akku weiterlaedt, der bereits ueber dem
            # aktuellen Kurvenpunkt liegt. Abregelschutz und Grid-Guard bleiben
            # hoeherrangig.
            if (tl_softcap_candidate is None
                    and tl_soc_now is not None
                    and not _abregel_pressure
                    and _tl_grid_consec < _TL_GRID_CONSEC_LIMIT
                    and tl_pv_day_active):
                tl_softcap_candidate = ('Kurve', tl_soc_now, t0, tl_soft_tolerance)

        if tl_softcap_candidate is not None:
            _cand_label, _cand_soc, _cand_ts, _cand_start_margin = tl_softcap_candidate
            _start_soc = float(_cand_soc) + float(_cand_start_margin)
            _release_soc = float(_cand_soc) - float(tl_soft_release_pct)
            if _tl_softcap_gate:
                # Ziel kann mit der rollenden Kurve wandern; darum wird der
                # aktive Gate-Anker pro Zyklus aktualisiert, aber erst deutlich
                # unterhalb der Kurve wieder freigegeben.
                _tl_softcap_label = _cand_label
                _tl_softcap_soc = float(_cand_soc)
                _tl_softcap_ts = _cand_ts
                if (fBatt_SOC <= _release_soc
                        or _tl_grid_consec >= _TL_GRID_CONSEC_LIMIT
                        or not tl_pv_day_active
                        or _abregel_pressure):
                    _tl_softcap_gate = False
            elif fBatt_SOC >= _start_soc:
                _tl_softcap_gate = True
                _tl_softcap_label = _cand_label
                _tl_softcap_soc = float(_cand_soc)
                _tl_softcap_ts = _cand_ts
        elif _tl_softcap_gate:
            _tl_softcap_gate = False

        if _tl_softcap_gate:
            tl_soc_cap_active = True
            tl_soc_cap_label = _tl_softcap_label
            tl_soc_cap_soc = _tl_softcap_soc
            tl_soc_cap_ts = _tl_softcap_ts

        # TL-Autodump:
        # Wenn eine Wolke/Hauslast stabil Netzbezug erzeugt UND der Speicher ueber
        # der aktuellen Kurve liegt, darf die Batterie aktiv in den Bedarf bzw.
        # bis zur freien Einspeise-Reserve entladen. Das bildet Eba's Verhalten
        # nach, ohne in eine Schleife mit Abregelschutz/Wallbox-Budget zu geraten.
        if (tl_autodump_enable == 1
                and tl_active
                and tl_soc_now is not None
                and pd_state != 'active'
                and awattar_mode not in (0, 2)
                and not _abregel_pressure
                and tl_pv_day_active
                and not pv_collapse_active
                and _tl_grid_consec >= _TL_GRID_CONSEC_LIMIT
                and fBatt_SOC > ep_reserve + 5.0):
            _tl_relief_for_wb = _curve_wb_relief_budget(tl_soc_now)
            if _tl_relief_for_wb.get('active') and _curve_relief_has_consumer():
                # Die Wallbox ist bereits der gewollte Verbraucher fuer den
                # Kurvenueberschuss. Hier darf kein allgemeiner KURVEN-DUMP
                # oder schneller DISCH-Grid-Waechter anspringen, sonst pendelt
                # das System zwischen Netzbezug und Batterieentladung. Der E3DC
                # bleibt autonom, die Wallbox bekommt nur ihr Budget.
                _tl_autodump_gate = False
                _relief_budget_w = int(_tl_relief_for_wb.get('budget_w', 0) or 0)
                ctrl.set_max_charge_power(0)
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
                _tl_relief_reason = (
                    '[%s] WB-KURVENENTLASTUNG AUTO: SOC=%.1f%% > Kurve=%.1f%% '
                    '[WB-Budget %dW, WB=%.0fW, Grid=%.0fW] -> E3DC AUTO, kein Kurven-Dump'
                ) % (
                    dt.strftime('%H:%M'), fBatt_SOC, tl_soc_now,
                    _relief_budget_w, abs(float(fPower_WB or 0.0)), fPower_Grid_ctrl)
                log.info(_tl_relief_reason)
                iE3DC_Req_Load_alt = 0
                write_state({'state': 'tl_curve_auto_relief', 'reason': _tl_relief_reason,
                             'mode': MODE_AUTO, 'val': int(maximumLadeleistung), 'soc': fBatt_SOC,
                             'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                             'tl_ts_target': tl_ts_target,
                             'ladeende': tl_soc_now,
                             'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                             'bat_w': iPower_Bat})
                try:
                    write_wb_budget({
                        'budget_w': _relief_budget_w,
                        'iAVal_w': _relief_budget_w,
                        'raw_iAVal_w': _relief_budget_w,
                        'iFc_w': 0.0,
                        'iMinLade_w': 0.0,
                        'state': 'tl_curve_auto_relief',
                        'storage_state': 'tl_curve_auto_relief',
                        'reason': _tl_relief_reason[:100],
                        'ts': t0,
                        'curve_wb_relief': True,
                        'curve_ref_soc': _tl_relief_for_wb.get('curve_ref_soc'),
                        'curve_excess_pct': _tl_relief_for_wb.get('curve_excess_pct'),
                        'curve_relief_from_soc_w': _tl_relief_for_wb.get('curve_relief_from_soc_w', 0),
                        'wb_storage_extra_w': _relief_budget_w,
                        'wb_storage_cap_w': max(0, int(fPower_WB) + _relief_budget_w),
                        'energy_score': {
                            'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                            'free_for_limbs_w': _relief_budget_w,
                            'bat_charge_request_w': 0,
                            'prio_factor': 1.0,
                            'prio_reason': 'tl_curve_wb_relief',
                        }
                    })
                except Exception:
                    pass
                time.sleep(CYCLE_S); continue

            _tl_excess_pct = fBatt_SOC - float(tl_soc_now)
            if _tl_autodump_gate:
                if _tl_excess_pct <= tl_autodump_release_pct:
                    _tl_autodump_gate = False
            elif _tl_excess_pct >= tl_autodump_start_pct:
                _tl_autodump_gate = True

            if _tl_autodump_gate:
                _max_discharge = max(0, int(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung)))
                _load_deficit_w = max(0, int(iPowerHome) + int(wp_w) + int(fPower_WB) - int(iPower_PV))
                _grid_import_w = max(0, int(fPower_Grid))
                _need_w = max(_load_deficit_w, _grid_import_w)
                _capacity_wh_tl = max(1000.0, float(speichergroesse) * 1000.0)
                _excess_wh = max(0.0, (_tl_excess_pct - tl_soft_tolerance) * _capacity_wh_tl / 100.0)
                _curve_dump_w = int(_excess_wh / tl_autodump_horizon_h)
                _export_room_w = max(0, int(einspeiselimit_w - _ab_puffer + fPower_Grid))
                _dump_w = min(_max_discharge, _export_room_w, max(_need_w, _curve_dump_w))

                if _dump_w >= tl_autodump_min_w:
                    ctrl.set_max_charge_power(0)
                    ctrl.send(MODE_DISCH, _dump_w, force=force)
                    _tl_dump_reason = (
                        '[%s] KURVEN-DUMP: SOC=%.1f%% > Kurve=%.1f%% + %.1f%%, '
                        'Grid=%.0fW PV=%dW -> DISCH %dW'
                    ) % (
                        dt.strftime('%H:%M'), fBatt_SOC, tl_soc_now, tl_autodump_start_pct,
                        fPower_Grid, iPower_PV, _dump_w)
                    log.info(_tl_dump_reason)
                    iE3DC_Req_Load_alt = -_dump_w
                    write_state({'state': 'tl_autodump', 'reason': _tl_dump_reason,
                                 'mode': MODE_DISCH, 'val': _dump_w, 'soc': fBatt_SOC,
                                 'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                                 'tl_ts_target': tl_ts_target,
                                 'ladeende': tl_soc_now,
                                 'iFc_w': 0, 'iMinLade_w': 0,
                                 'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                                 'bat_w': iPower_Bat,
                                 'autodump_excess_pct': round(_tl_excess_pct, 2),
                                 'autodump_release_pct': round(tl_autodump_release_pct, 2),
                                 'autodump_export_room_w': _export_room_w})
                    try:
                        _tl_relief = _curve_wb_relief_budget(tl_soc_now)
                        _tl_relief_w = int(_tl_relief.get('budget_w', 0) or 0)
                        _tl_dump_budget = {
                            'budget_w': _tl_relief_w,
                            'iAVal_w': _tl_relief_w,
                            'raw_iAVal_w': _tl_relief_w,
                            'iFc_w': 0.0,
                            'iMinLade_w': 0.0,
                            'state': 'tl_autodump',
                            'storage_state': 'tl_autodump',
                            'reason': _tl_dump_reason[:100],
                            'ts': t0,
                            'curve_wb_relief': bool(_tl_relief.get('active')),
                            'curve_ref_soc': _tl_relief.get('curve_ref_soc'),
                            'curve_excess_pct': _tl_relief.get('curve_excess_pct'),
                            'curve_relief_from_soc_w': _tl_relief.get('curve_relief_from_soc_w', 0),
                            'wb_storage_extra_w': _tl_relief_w,
                            'wb_storage_cap_w': max(0, int(fPower_WB) + _tl_relief_w),
                            'energy_score': {
                                'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                                'free_for_limbs_w': _tl_relief_w,
                                'bat_charge_request_w': 0,
                                'prio_factor': 1.0,
                                'prio_reason': 'tl_curve_wb_relief' if _tl_relief.get('active') else 'tl_autodump',
                            }
                        }
                        write_wb_budget(_tl_dump_budget)
                    except Exception:
                        pass
                    time.sleep(CYCLE_S); continue
        elif _tl_autodump_gate:
            _tl_autodump_gate = False

        tl_end_auto_coast = False
        tl_end_auto_coast_rest_s = 0.0
        if tl_active:
            try:
                _tl_end_coast_min = max(0.0, float(gf(cfg, 'tl_end_auto_coast_min', 45.0)))
                _tl_end_gap_pct = max(0.0, float(gf(cfg, 'tl_end_auto_coast_target_gap_pct', 3.0)))
                tl_end_auto_coast_rest_s = float(tLadezeitende - t)
                tl_end_auto_coast = (
                    _tl_end_coast_min > 0.0
                    and tl_soc_cap_active
                    and tl_pv_day_active
                    and pd_state != 'active'
                    and awattar_mode not in (0, 2)
                    and not _price_boost_any
                    and not _abregel_pressure
                    and _tl_grid_consec < _TL_GRID_CONSEC_LIMIT
                    and 0.0 <= tl_end_auto_coast_rest_s <= (_tl_end_coast_min * 60.0)
                    and fBatt_SOC >= (float(fLadeende) - _tl_end_gap_pct)
                )
            except Exception:
                tl_end_auto_coast = False

        if tl_soc_cap_active and _tl_grid_consec >= _TL_GRID_CONSEC_LIMIT:
            # Stabiler Netzbezug hebt auch die weiche Zwischenziel-IDLE auf.
            # Entweder hat TL-Autodump oben bereits uebernommen, oder die
            # normale Eba-Logik darf jetzt den Bedarf aus der Batterie decken.
            tl_soc_cap_active = False

        wb_curve_bypass_for_wb = False
        if tl_soc_cap_active and not _price_boost_any:
            try:
                _wb_bypass_minsoc = float(cfg.get('wbminsoc', 80))
                _wb_bypass_mode = normalize_wb_mode(wb_intent_mode)
                if not storage_floor_mode(_wb_bypass_mode):
                    _wb_bypass_mode = max(
                        [normalize_wb_mode(cfg.get(f'wb{i}_mode', 0)) for i in [1, 2]],
                        default=MODE_OFF
                    )
                _wb_bypass_vehicle = (
                    wb_intent_car_active
                    or wb_intent_charging
                    or wb_real_power_active
                )
                wb_curve_bypass_for_wb = (
                    storage_floor_mode(_wb_bypass_mode)
                    and _wb_bypass_vehicle
                    and fBatt_SOC > (_wb_bypass_minsoc + 0.1)
                )
            except Exception:
                wb_curve_bypass_for_wb = False

        if tl_end_auto_coast and not wb_curve_bypass_for_wb:
            # C++-naher Kurvenauslauf: Kurz vor dem Ziel ist AUTO ruhiger als
            # ein hart gehaltener IDLE-Befehl. IDLE bleibt fuer echte Sperren
            # erhalten, aber der E3DC darf die letzten Minuten selbst auspendeln.
            ctrl.set_max_charge_power(0)
            ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=False)
            _tl_end_free_w = max(0, int(iPower_PV - iPowerHome_budget - int(wp_w) - int(fPower_WB)))
            _tl_end_reason = (
                '[%s] KURVEN-AUSLAUF: SOC=%.1f%% nahe Ziel %.1f%%, Rest %.0fmin '
                '-> E3DC Auto statt IDLE-Pendeln'
            ) % (dt.strftime('%H:%M'), fBatt_SOC, fLadeende, max(0.0, tl_end_auto_coast_rest_s) / 60.0)
            log_status_throttled('tl_end_auto_coast', _tl_end_reason, 60)
            write_state({'state': 'tl_end_auto_coast', 'reason': _tl_end_reason,
                         'mode': MODE_AUTO, 'val': int(maximumLadeleistung), 'soc': fBatt_SOC,
                         'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                         'tl_ts_target': tl_ts_target,
                         'ladeende': fLadeende, 'end_h': round(end_h, 2),
                         'iFc_w': 0, 'iMinLade_w': 0,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                         'bat_w': iPower_Bat})
            write_wb_budget({
                'budget_w': _tl_end_free_w,
                'iAVal_w': _tl_end_free_w,
                'iFc_w': 0.0,
                'iMinLade_w': 0.0,
                'state': 'tl_end_auto_coast',
                'storage_state': 'tl_end_auto_coast',
                'reason': _tl_end_reason[:100],
                'energy_score': {
                    'pv_surplus_w': _tl_end_free_w,
                    'free_for_limbs_w': _tl_end_free_w,
                    'bat_charge_request_w': 0,
                    'prio_factor': 1.0,
                    'prio_reason': 'tl_end_auto_coast',
                }
            })
            time.sleep(CYCLE_S); continue

        if tl_soc_cap_active and not wb_curve_bypass_for_wb:
            _tl_relief = _curve_wb_relief_budget(tl_soc_cap_soc)
            _tl_relief_has_real_wb = bool(_tl_relief.get('active') and _curve_relief_has_real_consumer())
            if (
                iPower_PV > 500
                and awattar_mode != 2
                and not storage_floor_mode(wb_intent_mode)
                and not _tl_relief_has_real_wb
                and fBatt_SOC > ep_reserve + 0.5
                and (fPower_Grid_ctrl > tl_grid_limit_w
                     or (tl_idle_grid_guard_hold_until > t0 and tl_idle_grid_guard_hold_w > 0))
            ):
                if fPower_Grid_ctrl > tl_grid_limit_w:
                    _tl_guard_w = int(min(maximumLadeleistung * 0.95,
                                          max(300.0, fPower_Grid_ctrl + 350.0)))
                    tl_idle_grid_guard_hold_w = max(int(tl_idle_grid_guard_hold_w), _tl_guard_w)
                    tl_idle_grid_guard_hold_until = max(float(tl_idle_grid_guard_hold_until), t0 + 45.0)
                    _tl_guard_reason = ('[%s] KURVEN-HALTEWAECHTER: Grid=%.0fW/%.0fW -> DISCH %dW statt IDLE') % (
                        dt.strftime('%H:%M'), fPower_Grid, fPower_Grid_ctrl, _tl_guard_w)
                else:
                    _tl_guard_w = int(min(maximumLadeleistung * 0.95, tl_idle_grid_guard_hold_w))
                    _tl_guard_reason = ('[%s] KURVEN-HALTEWAECHTER Halt: DISCH %dW noch %.0fs statt IDLE') % (
                        dt.strftime('%H:%M'), _tl_guard_w, max(0.0, tl_idle_grid_guard_hold_until - t0))
                ctrl.send(MODE_DISCH, _tl_guard_w, force=True)
                log.info(_tl_guard_reason)
                iE3DC_Req_Load_alt = -_tl_guard_w
                write_state({'state': 'tl_idle_grid_guard', 'reason': _tl_guard_reason,
                             'mode': MODE_DISCH, 'val': _tl_guard_w, 'soc': fBatt_SOC,
                             'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                             'tl_ts_target': tl_ts_target,
                             'tl_soft_release_pct': tl_soft_release_pct,
                             'ladeende': tl_soc_cap_soc,
                             'iFc_w': 0, 'iMinLade_w': 0,
                             'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                              'grid_ema_w': int(fPower_Grid_ctrl),
                              'bat_w': iPower_Bat})
                _tl_relief_w = int(_tl_relief.get('budget_w', 0) or 0)
                write_wb_budget({
                    'budget_w': _tl_relief_w,
                    'iAVal_w': _tl_relief_w,
                    'raw_iAVal_w': _tl_relief_w,
                    'iFc_w': 0.0,
                    'iMinLade_w': 0.0,
                    'state': 'tl_idle_grid_guard',
                    'storage_state': 'tl_idle_grid_guard',
                    'reason': _tl_guard_reason[:100],
                    'curve_wb_relief': bool(_tl_relief.get('active')),
                    'curve_ref_soc': _tl_relief.get('curve_ref_soc'),
                    'curve_excess_pct': _tl_relief.get('curve_excess_pct'),
                    'curve_relief_from_soc_w': _tl_relief.get('curve_relief_from_soc_w', 0),
                    'wb_storage_extra_w': _tl_relief_w,
                    'wb_storage_cap_w': max(0, int(fPower_WB) + _tl_relief_w),
                    'energy_score': {
                        'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                        'free_for_limbs_w': _tl_relief_w,
                        'bat_charge_request_w': 0,
                        'prio_factor': 1.0,
                        'prio_reason': 'tl_curve_wb_relief' if _tl_relief.get('active') else 'tl_idle_grid_guard',
                    }
                })
                time.sleep(CYCLE_S); continue

            _tl_idle_hold_s = max(float(CYCLE_S) * 2.0, float(gf(cfg, 'tl_idle_hold_s', 45.0)))
            if iPower_Bat > 300:
                _idle_charge_violation_count += 1
                if _idle_charge_violation_count >= 2:
                    tl_idle_charge_hold_until = max(float(tl_idle_charge_hold_until), t0 + _tl_idle_hold_s)
            elif tl_idle_charge_hold_until <= t0:
                _idle_charge_violation_count = 0
            else:
                # Kurz nach einem Verstoß IDLE weiter halten, auch wenn der
                # aktuelle Messwert schon wieder ruhig aussieht. Sonst startet
                # manche E3DC-Firmware das PV-Laden zwischen zwei Zyklen erneut.
                pass
            _idle_hold_active = tl_idle_charge_hold_until > t0
            _idle_force = _idle_charge_violation_count >= 2 or _idle_hold_active
            if _tl_relief_has_real_wb and not _idle_force:
                _tl_idle_mode = MODE_AUTO
                _tl_idle_val = int(maximumLadeleistung)
                _tl_idle_state = 'tl_curve_auto_relief'
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
                _tl_idle_reason = ('[%s] KURVEN-AUTO: SOC=%.1f%% >= %s %.1f%%@%s '
                                   '(Hyst %.1f%%, Fremd-WB aktiv, E3DC autonom)') % (
                    dt.strftime('%H:%M'), fBatt_SOC, tl_soc_cap_label, tl_soc_cap_soc,
                    datetime.datetime.fromtimestamp(tl_soc_cap_ts).strftime('%H:%M') if tl_soc_cap_ts else '?',
                    tl_soft_release_pct)
            else:
                _tl_idle_mode = MODE_IDLE
                _tl_idle_val = 0
                _tl_idle_state = 'idle'
                ctrl.send(MODE_IDLE, 0, force=_idle_force)
                _tl_idle_reason = ('[%s] KURVEN-HALT: SOC=%.1f%% >= %s %.1f%%@%s '
                                   '(Hyst %.1f%%, kein Speicher-Ladeauftrag, Abregelschutz inaktiv)') % (
                    dt.strftime('%H:%M'), fBatt_SOC, tl_soc_cap_label, tl_soc_cap_soc,
                    datetime.datetime.fromtimestamp(tl_soc_cap_ts).strftime('%H:%M') if tl_soc_cap_ts else '?',
                    tl_soft_release_pct)
            if _idle_charge_violation_count >= 2:
                _tl_idle_reason += ' [Nachdruecken: Akku laedt trotz IDLE %.0fW]' % iPower_Bat
            elif _idle_hold_active:
                _tl_idle_reason += ' [IDLE-Halteanker %.0fs]' % max(0.0, tl_idle_charge_hold_until - t0)
            log.info(_tl_idle_reason)
            iE3DC_Req_Load_alt = 0
            write_state({'state': _tl_idle_state, 'reason': _tl_idle_reason,
                          'mode': _tl_idle_mode, 'val': _tl_idle_val, 'soc': fBatt_SOC,
                          'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                          'tl_ts_target': tl_ts_target,
                          'tl_soft_release_pct': tl_soft_release_pct,
                          'ladeende': tl_soc_cap_soc, 'end_h': round((datetime.datetime.fromtimestamp(tl_soc_cap_ts).hour + datetime.datetime.fromtimestamp(tl_soc_cap_ts).minute / 60.0), 2) if tl_soc_cap_ts else round(end_h, 2),
                          'iFc_w': 0, 'iMinLade_w': 0,
                          'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                          'bat_w': iPower_Bat})
            try:
                _free_w = max(0, iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB))
                if _tl_relief.get('active'):
                    _free_w = max(_free_w, int(_tl_relief.get('budget_w', 0) or 0))
                _tl_idle_budget = {
                    'budget_w': _free_w,
                    'budget_amp_1ph': (max(6, min(32, int(_free_w / 230))) if _free_w >= 6*230 else 0),
                    'budget_amp_3ph': (max(6, min(32, int(_free_w / 690))) if _free_w >= 6*690 else 0),
                    'iAVal_w': _free_w,
                    'raw_iAVal_w': _free_w,
                    'iFc_w': 0.0,
                    'iMinLade_w': 0.0,
                    'state': _tl_idle_state,
                    'storage_state': _tl_idle_state,
                    'reason': _tl_idle_reason[:100],
                    'ts': t0,
                    'curve_wb_relief': bool(_tl_relief.get('active')),
                    'curve_ref_soc': _tl_relief.get('curve_ref_soc'),
                    'curve_excess_pct': _tl_relief.get('curve_excess_pct'),
                    'curve_relief_from_soc_w': _tl_relief.get('curve_relief_from_soc_w', 0),
                    'wb_storage_extra_w': int(_tl_relief.get('budget_w', 0) or 0),
                    'wb_storage_cap_w': max(0, int(fPower_WB) + int(_tl_relief.get('budget_w', 0) or 0)),
                    'energy_score': {
                        'pv_surplus_w': _free_w,
                        'free_for_limbs_w': _free_w,
                        'bat_charge_request_w': 0,
                        'prio_factor': 1.0,
                        'prio_reason': 'tl_curve_wb_relief' if _tl_relief.get('active') else 'tl_curve_idle',
                    }
                }
                tmp = WB_F + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(_tl_idle_budget, f)
                os.replace(tmp, WB_F)
            except Exception:
                pass
            time.sleep(CYCLE_S); continue

        if (tl_active
                and not tl_pv_day_active
                and pd_state != 'active'
                and awattar_mode not in (0, 2)):
            _night_wb_modes = [normalize_wb_mode(cfg.get(f'wb{i}_mode', 0)) for i in [1, 2]]
            _night_wb_mode = max(_night_wb_modes) if _night_wb_modes else MODE_OFF
            _night_wb_needs_storage = (
                storage_floor_mode(_night_wb_mode)
                and wb_measured_for_storage
            )
            if not _night_wb_needs_storage:
                ctrl.set_max_charge_power(0)
                ctrl.send(MODE_AUTO, maximumLadeleistung, force=False)
                _night_reason = (
                    '[%s] NACHTFREIGABE: PV=%dW, SOC=%.1f%% Kurve=%.1f%% - '
                    'E3DC Auto versorgt Haus, keine Kurven-Entladung'
                ) % (dt.strftime('%H:%M'), iPower_PV, fBatt_SOC, tl_soc_now or 0.0)
                log_status_throttled('tl_night_release', _night_reason)
                write_state({'state': 'auto', 'phase': 'Nachtfreigabe',
                             'reason': _night_reason,
                             'mode': MODE_AUTO, 'val': int(maximumLadeleistung),
                             'soc': fBatt_SOC, 'tl_soc_now': tl_soc_now,
                             'tl_soc_target': tl_soc_target, 'pv_w': iPower_PV,
                             'grid_w': int(fPower_Grid), 'bat_w': iPower_Bat})
                _night_free_w = max(0, int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)))
                write_wb_budget({
                    'budget_w': _night_free_w,
                    'iAVal_w': _night_free_w,
                    'iFc_w': 0.0,
                    'iMinLade_w': 0.0,
                    'state': 'auto',
                    'storage_state': 'auto',
                    'reason': _night_reason[:100],
                    'energy_score': {
                        'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                        'free_for_limbs_w': _night_free_w,
                        'bat_charge_request_w': 0,
                        'prio_factor': 1.0,
                        'prio_reason': 'tl_night_release',
                    }
                })
                time.sleep(CYCLE_S); continue

        # ----------------------------------------------------------------
        # iFc Berechnung (Eba Zeile 4099-4145)
        # Nur neu berechnen wenn SOC sich geaendert hat oder >300s alt (Zeile 4101)
        # ----------------------------------------------------------------
        if t<tLadezeitende:
            if (fBatt_SOC!=fBatt_SOC_alt or (t-tLadezeit_alt>300)
                    or tLadezeitende!=tLadezeitende_alt or iFc==0 or bCheckConfig):
                fBatt_SOC_alt=fBatt_SOC; bCheckConfig=False
                tLadezeitende_alt=tLadezeitende; tLadezeit_alt=t

                # Kernformel (Zeile 4112): (Ziel-Ist)*kWh*10*3600
                iFc=(fLadeende-fBatt_SOC)*speichergroesse*10*3600

                # Latenzbereich 0.5% (Zeile 4114)
                if -0.6<(fLadeende-fBatt_SOC)<0 and iFc<0: iFc=0

                # Abend-Freilauf: Kurz vor Ladeende nicht mehr aktiv auf die
                # Kurve herunter entladen. Wenn der Speicher nur wenige Prozent
                # ueber Ziel liegt, erledigt der normale Hausverbrauch das
                # Absenken ruhiger und C++-nah ohne DISCH-Pendel.
                _evening_guard_min = float(gf(cfg, 'tl_evening_guard_min', 90))
                _evening_deadband_pct = float(gf(cfg, 'tl_evening_deadband_pct', 3.0))
                _evening_pv_max_w = float(gf(cfg, 'tl_evening_pv_max_w', 1500))
                _rest_s = tLadezeitende - t
                _soc_over_target = fBatt_SOC - fLadeende
                if (tl_active and iFc < 0
                        and 0 <= _rest_s <= _evening_guard_min * 60
                        and iPower_PV <= _evening_pv_max_w
                        and 0 < _soc_over_target <= _evening_deadband_pct):
                    iFc = 0
                    log.info('[%s] Abendfreilauf: SOC=%.1f%% liegt %.1f%% ueber Ziel %.1f%% -> kein aktives DISCH' % (
                        dt.strftime('%H:%M'), fBatt_SOC, _soc_over_target, fLadeende))

                # Durch Restzeit teilen, min 300s (Zeile 4116)
                restzeit=tLadezeitende-t
                if restzeit>300: iFc=iFc/restzeit
                else:            iFc=iFc/300

                # iMinLade = iFc begrenzt (Zeile 4121)
                iMinLade=min(iFc,maximumLadeleistung) if iFc>0 else 0

                # Totband (Zeile 4125)
                if   iFc>=untererLadekorridor:  iFc-=untererLadekorridor
                elif abs(iFc)>=untererLadekorridor: iFc+=untererLadekorridor
                else: iFc=0

                # Powerfaktor + Grenzen (Zeile 4132-4135)
                # FIX: Wenn tl_active (Zielkurve) genutzt wird, ist die Kurve bereits perfekt
                # berechnet. Der powerfaktor wuerde sie kuenstlich zu steil machen!
                if not tl_active:
                    iFc*=powerfaktor

                if iFc>maximumLadeleistung:    iFc=maximumLadeleistung
                if abs(iFc)>maximumLadeleistung: iFc=-maximumLadeleistung
                if abs(iFc)<minimumLadeleistung: iFc=0
        else:
            # Nach tLadezeitende: Freilauf (Zeile 4140)
            iFc=maximumLadeleistung
            iMinLade=0 if fBatt_SOC>=fLadeende else maximumLadeleistung

        iFc=int(iFc); iMinLade=int(iMinLade)

        # TL-Soft-Cap: SOC ist bereits ueber dem 2h-Zwischenziel der Kurve.
        # iFc=0 setzen -> E3DC bekommt keinen Ladeauftrag vom Eba-Algorithmus.
        # Batterieladung nur noch durch echten PV-Ueberschuss (E3DC Eigenstrategie)
        # oder bis TL-Bremse (IDLE) wieder greift wenn SOC > Kurve+Toleranz.
        if tl_soc_cap_active:
            iFc = 0
            iMinLade = 0
            log.debug('[%s] TL-SoftCap: SOC=%.1f%% >= Ziel=%.1f%% -> iFc=0 (kein Ladeauftrag)' % (
                dt.strftime('%H:%M'), fBatt_SOC, tl_soc_target if tl_soc_target else 0))

        # ----------------------------------------------------------------
        # iPower: Ueberschussleistung ermitteln (Eba Zeile 4945)
        # iPower = (-iPower_Bat + fPower_Grid - einspeiselimit*-1000)*-1
        #        =  iPower_Bat - fPower_Grid - einspeiselimit_w
        # ----------------------------------------------------------------
        iPower=int(iPower_Bat - fPower_Grid - einspeiselimit_w)

        # Wenn PV-Leistung WR-Leistung ueberschreitet (Zeile 4949) - vereinfacht
        # wrleistung ignorieren (nicht in unserer Config)

        # ----------------------------------------------------------------
        # Lade-Steuerung (Eba Zeile 4975-4991)
        # if (SOC > ladeschwelle && t < tLadezeitende) || (SOC > ladeende)
        # ----------------------------------------------------------------
        if ((fBatt_SOC>ladeschwelle and t<tLadezeitende) or fBatt_SOC>fLadeende):
            # Boost auf iFc wenn noetig
            if iPower<iFc:
                iPower=iFc
                if iPower>maximumLadeleistung: iPower=maximumLadeleistung
        else:
            # Freilauf: SOC unter Ladeschwelle oder nach Ladeende (Zeile 4991)
            # WICHTIG: Kein Freilauf erzwingen, wenn wir aktuell Netzstrom beziehen!
            # UND: TL-Kurve hat Vorrang - niemals Freilauf wenn TL mit iFc>0 aktiv!
            if tl_active and iFc > 0:
                iPower = iFc  # TL-Kurve: exakt iFc, kein Freilauf
            elif fPower_Grid <= 100:
                iPower = maximumLadeleistung

        # ----------------------------------------------------------------
        # iLMStatus Steuerblock (Eba Zeile 5039)
        # Vereinfacht: kein Countdown, wir pruefen direkt.
        # ----------------------------------------------------------------
        # ----------------------------------------------------------------
        # Ziel-/Preislimit-Modi: WB PV+Speicher-Floor.
        # Ziel wbminSoC ist fachlich Grundladung stabil ohne Grundladung:
        # die Ladekurve endet bei wbminSoC, und oberhalb dieser Grenze darf
        # der Speicher die Wallbox bis zur maximalen Entladeleistung stuetzen.
        # Mode 5 ist dieser Pfad plus Preisfreigabe; darunter laedt die WB nur
        # weiter aus dem Netz, wenn der Wallbox Manager das Preisfenster oeffnet.
        # ----------------------------------------------------------------
        wb_modes = [normalize_wb_mode(cfg.get(f'wb{i}_mode', 0)) for i in [1, 2]]
        # Wallbox Manager ist nicht mehr EMS-Besitzer. Bei frischem Intent
        # zaehlt nur der real gesteckte/aktive Ladepunkt; alte Config-Modi
        # sind nur Fallback, wenn der Wallbox Manager gerade kein Signal liefert.
        # Der Storage Manager entscheidet zentral, ob daraus IDLE/DISCH/AUTO wird.
        wb_intent_modes = (
            [normalize_wb_mode(wb_intent_mode)]
            if wb_intent_fresh and wb_intent_car_active and storage_floor_mode(wb_intent_mode)
            else []
        )
        wb_mode_candidates = wb_intent_modes if wb_intent_modes else wb_modes
        wb_mode_active = 0 if _price_boost_any else max(
            [m for m in wb_mode_candidates if storage_floor_mode(m)],
            default=0
        )
        wb_control_present = bool(wb_measured_for_storage)
        wb9_mode = storage_floor_mode(wb_mode_active) and wb_control_present
        wb_floor_intent = (
            not _price_boost_any
            and wb_intent_request == 'hold_discharge'
            and storage_floor_mode(wb_intent_mode)
            and wb_measured_for_storage
        )
        wb9_active = False
        wb9_floor_hold = False
        if wb9_mode or wb_floor_intent:
            wbminsoc    = float(cfg.get('wbminsoc', 80))
            wb_soc_hyst = float(gf(cfg, 'wb_soc_hysterese_pct',
                                gf(cfg, 'wb_hysterese_pct', 0.7)))
            wb_soc_hyst = max(0.1, min(2.0, wb_soc_hyst))
            max_entlade = float(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung))
            wb_charging = bool(
                wb_measured_for_storage
                or (wb_intent_request == 'hold_discharge' and wb_intent_charging)
            )
            pv_ok       = True

            if wb_floor_intent and not wb9_mode:
                wb9_active = False
                wb9_floor_hold = wb_charging
            else:
                if _wb9_discharge_gate:
                    if fBatt_SOC <= wbminsoc or not wb_charging or not pv_ok:
                        _wb9_discharge_gate = False
                else:
                    if fBatt_SOC >= (wbminsoc + wb_soc_hyst) and wb_charging and pv_ok:
                        _wb9_discharge_gate = True

                if not _wb9_discharge_gate:
                    wb9_active = False
                    wb9_floor_hold = wb_charging and pv_ok
                else:
                    wb9_state = "auto_fuer_wb_mode%d" % wb_mode_active
                    soc_diff = max(0.0, fBatt_SOC - wbminsoc)
                    if wb_mode_active == MODE_TARGET:
                        discharge_w = int(max_entlade)
                    else:
                        discharge_w = int(min(max_entlade * soc_diff**3 / 4.0, max_entlade * 0.9))
                    if fPower_Grid > 100:
                        # Direkt oberhalb wbminSoC ist die kubische C++-Kurve sehr
                        # vorsichtig. Bei echter Haus-/Wallbox-Last darf sie aber
                        # Netzbezug nicht stehen lassen. Deshalb Grid-Guard:
                        # nur bei offenem wbmin-Gate, weiterhin gedeckelt durch
                        # maximale Entladeleistung und Hysterese.
                        grid_guard_w = int(fPower_Grid * 1.3)
                        discharge_w = max(discharge_w, min(int(max_entlade), discharge_w + grid_guard_w))
                    if wb_charging:
                        # Mode 9/10: Die Wallbox bestimmt, dass der Speicher bis
                        # wbminSoC helfen darf. Die tatsaechliche Entladung folgt
                        # aber dem Netzuebergabepunkt: bei Bezug nachschieben, bei
                        # Einspeisung zuruecknehmen. So gibt es keinen 10-kW-Dump
                        # ins Netz, besonders nicht bei einphasigem Fahrzeug.
                        _wb_need_w = max(
                            abs(float(fPower_WB or 0.0)),
                            float(wb_intent_power_w),
                            float(live_wb_phase_sum)
                        )
                        if _wb_need_w <= 0:
                            # Ohne echte Messleistung keine Batterieentladung.
                            wb9_active = False
                            wb9_floor_hold = False
                            continue
                        _grid_target_w = -150.0
                        _current_discharge_w = max(0.0, -float(iPower_Bat))
                        _home_for_wb_discharge_w = max(
                            0.0,
                            min(
                                float(iPowerHome_budget),
                                float(iPowerHome) + 150.0,
                            )
                        )
                        # Direkte Lastfuehrung statt Sollwert-/Altwert-Folge:
                        # Entladung soll die echte Wallbox plus Hauslast tragen
                        # und am Netzpunkt eine kleine Einspeisereserve lassen.
                        # Das ist ruhiger als nur aus der alten Batterieleistung
                        # nachzuziehen und verhindert Mode-10-Ueberentladung bei
                        # einphasigem Laden.
                        _load_target_discharge_w = max(
                            0.0,
                            _wb_need_w + _home_for_wb_discharge_w - float(iPower_PV) - _grid_target_w
                        )
                        _grid_follow_w = max(
                            0.0,
                            _current_discharge_w
                            + (float(fPower_Grid_ctrl) - _grid_target_w) * 0.75
                        )
                        if float(fPower_Grid_ctrl) > _grid_target_w:
                            _target_discharge_w = max(_load_target_discharge_w, _grid_follow_w)
                        else:
                            _target_discharge_w = min(_load_target_discharge_w, _grid_follow_w)
                        if _target_discharge_w > _current_discharge_w:
                            _target_discharge_w = min(_target_discharge_w, _current_discharge_w + 600.0)
                        else:
                            _target_discharge_w = max(_target_discharge_w, _current_discharge_w - 900.0)
                        # Oberkante nur aus echter WB-Leistung plus Reserve.
                        # Soll-Ampere oder Anlaufdeckel bleiben draussen.
                        _wb_discharge_cap_w = _wb_need_w + max(500.0, _home_for_wb_discharge_w + 350.0)
                        if abs(fPower_WB) > 500:
                            _wb_discharge_cap_w = max(
                                _wb_discharge_cap_w,
                                abs(float(fPower_WB)) + max(500.0, _home_for_wb_discharge_w + 350.0)
                            )
                        discharge_w = int(min(float(max_entlade), _wb_discharge_cap_w, max(0.0, _target_discharge_w)))
                    discharge_w = max(0, min(discharge_w, int(max_entlade)))
                    wb9_active = True
                    iLMStatus = 1
        wbmin_recovery_active = False
        wbmin_recovery_w = 0
        wbmin_under_curve_recovery = False
        try:
            wbmin_under_curve_recovery = bool(
                tl_active
                and tl_soc_now is not None
                and fBatt_SOC < (float(tl_soc_now) - max(0.2, float(tl_soft_tolerance)))
            )
        except Exception:
            wbmin_under_curve_recovery = False
        if wb9_floor_hold and wb_mode_active == 4 and wbmin_under_curve_recovery:
            # Mode 4: wbminSoC ist das Ziel der Ladekurve, kein eigener
            # Aufhol-Waechter. Speicherladung gibt es nur, wenn wir die aktuelle
            # Kurve mit Hysterese unterschreiten; sonst bleibt der E3DC autonom
            # und die Wallbox ist die geregelte Last.
            _wbmin_recovery_reserve_w = 300.0
            _wbmin_recovery_charge_w = int(max(
                0.0,
                iPower_Bat + max(0.0, -fPower_Grid) - _wbmin_recovery_reserve_w
            ))
            _wbmin_recovery_charge_w = min(
                max(0, int(maximumLadeleistung) - 1),
                _wbmin_recovery_charge_w
            )
            if _wbmin_recovery_charge_w >= 300:
                wbmin_recovery_w = max(int(iMinLade), int(iFc), _wbmin_recovery_charge_w)
                wbmin_recovery_w = min(max(0, int(maximumLadeleistung) - 1), wbmin_recovery_w)
                iFc = max(int(iFc), wbmin_recovery_w)
                iMinLade = max(int(iMinLade), wbmin_recovery_w)
                iPower = max(int(iPower), wbmin_recovery_w)
                wbmin_recovery_active = True

        price_plan_storage_hold = (
            wb_intent_request == 'hold_discharge'
            and wb_intent_reason in ('price_plan_storage_protection', 'slot_grid_storage_protection')
            and wb_intent_fresh
            and wb_intent_car_active
            and (wb_intent_charging or wb_intent_power_w > 500 or wb_intent_set_amp > 0)
            and not _price_boost_any
            and pd_state != 'active'
            and awattar_mode != 2
            and not _abregel_pressure
        )
        if price_plan_storage_hold:
            # Beim geplanten oder guenstigen Netzladen darf E3DC AUTO den
            # Hausakku nicht als Netzbezugs-Puffer fuer die Wallbox leersaugen.
            # Trotzdem soll PV-Ueberschuss unter der Ladekurve in den Speicher
            # laufen und normaler Hausverbrauch darf traege aus dem Akku kommen.
            iAVal = int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB))
            _price_house_load_w = max(0, int(iPowerHome) + int(wp_w))
            _price_house_deficit_w = max(0, _price_house_load_w - int(iPower_PV))
            _price_curve_need_w = max(0, int(iFc), int(iMinLade))
            _price_export_w = max(0, int(-fPower_Grid), iAVal)
            _price_auto_candidate = (
                _price_curve_need_w > 0
                and (
                    iAVal > 500
                    or fPower_Grid < -500
                    or (iPower_Bat > 300 and fPower_Grid < 250)
                )
            )
            if _price_auto_candidate:
                price_hold_auto_until = max(price_hold_auto_until, t0 + max(CYCLE_S * 2.0, 20.0))
            _price_auto_charge = (
                _price_auto_candidate
                or (
                    price_hold_auto_until > t0
                    and _price_curve_need_w > 0
                    and fPower_Grid < 250
                    and iPower_Bat >= -150
                )
            )

            if _price_auto_charge:
                price_hold_discharge_w = 0
                ctrl.set_max_charge_power(0)
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
                _price_mode = MODE_AUTO
                _price_val = int(maximumLadeleistung)
                _price_state = 'price_plan_storage_auto'
                _price_storage_state = 'price_plan_storage_auto'
                reason = ('[%s] PREISPLAN-AUTO SOC=%.1f%% WB=%.0fW '
                          'Ueberschuss=%dW Kurvenbedarf=%dW -> PV laedt Speicher, '
                          'Wallbox bleibt im Preisfenster') % (
                              dt.strftime('%H:%M'), fBatt_SOC, fPower_WB,
                              _price_export_w, _price_curve_need_w)
            else:
                price_hold_auto_until = 0.0
                _max_house_discharge = max(0, int(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung)))
                _target_house_discharge = 0
                if _price_house_deficit_w >= 300 and fBatt_SOC > ep_reserve + 0.5:
                    _target_house_discharge = min(_max_house_discharge, int(_price_house_deficit_w + 150))
                if _target_house_discharge > 0:
                    if price_hold_discharge_w <= 0:
                        price_hold_discharge_w = min(_target_house_discharge, 300)
                    elif _target_house_discharge > price_hold_discharge_w:
                        price_hold_discharge_w = min(_target_house_discharge, price_hold_discharge_w + 300)
                    else:
                        price_hold_discharge_w = max(_target_house_discharge, price_hold_discharge_w - 600)
                    ctrl.set_max_charge_power(0)
                    ctrl.send(MODE_DISCH, int(price_hold_discharge_w), force=True)
                    _price_mode = MODE_DISCH
                    _price_val = int(price_hold_discharge_w)
                    _price_state = 'price_plan_house_discharge'
                    _price_storage_state = 'price_plan_house_discharge'
                    reason = ('[%s] PREISPLAN-HAUSSTUETZE SOC=%.1f%% WB=%.0fW '
                              'Hausdefizit=%dW -> DISCH %dW, Wallbox-Netzbezug bleibt erlaubt') % (
                                  dt.strftime('%H:%M'), fBatt_SOC, fPower_WB,
                                  _price_house_deficit_w, _price_val)
                else:
                    price_hold_discharge_w = 0
                    ctrl.send(MODE_IDLE, 0, force=True)
                    _price_mode = MODE_IDLE
                    _price_val = 0
                    _price_state = 'price_plan_storage_hold'
                    _price_storage_state = 'price_plan_storage_hold'
                    reason = ('[%s] PREISPLAN-SPEICHERSCHUTZ SOC=%.1f%% WB=%.0fW '
                              'Req=%s -> Batterie IDLE, guenstiges Ladefenster nutzt Netz') % (
                                  dt.strftime('%H:%M'), fBatt_SOC, fPower_WB, wb_intent_request)

            log_status_throttled(_price_state, reason, 60)
            write_wb_budget({
                'budget_w': max(0, int(iAVal)),
                'iAVal_w': int(iAVal),
                'raw_iAVal_w': int(iAVal),
                'iFc_w': float(iFc),
                'iMinLade_w': float(iMinLade),
                'state': _price_state,
                'storage_state': _price_storage_state,
                'reason': reason[:100],
                'source': 'storage_manager',
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': max(0, int(iAVal)),
                    'bat_charge_request_w': int(iMinLade),
                    'prio_factor': 1.0,
                    'prio_reason': 'price_plan_storage_hold',
                }
            })
            write_state({'state': _price_state, 'reason': reason,
                         'mode': _price_mode, 'val': _price_val, 'soc': fBatt_SOC,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                         'bat_w': iPower_Bat, 'wb_w': int(fPower_WB),
                         'iAVal_w': int(iAVal), 'iFc_w': iFc,
                         'iMinLade_w': iMinLade})
            time.sleep(CYCLE_S)
            continue

        if wb9_floor_hold and iMinLade > 0:
            # Unter wbminSoC bleibt DISCH gesperrt, die eigene Speicherladung
            # darf aber nur als AUTO-Freigabe laufen. So entscheidet der E3DC
            # die echte Ladeleistung am Netzpunkt selbst, waehrend die WB die
            # geregelte Last bleibt.
            iAVal = iPower_PV - max(0, iMinLade) - iPowerHome - int(wp_w) - int(fPower_WB)
            reason = ('[%s] WB-MINSOC-AUTO SOC=%.1f%% <= %.1f%%+%.1f%% '
                      '(Mode%d, WB=%.0fW) -> E3DC Auto, Wallbox bleibt Stellglied%s') % (
                          dt.strftime('%H:%M'), fBatt_SOC, wbminsoc, wb_soc_hyst,
                          wb_mode_active, fPower_WB,
                          (' [Kurve unter Ziel]' if wbmin_recovery_active else ''))
            log_status_throttled('wbmin_auto_recovery', reason, 60)
            ctrl.set_max_charge_power(0)
            ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
            write_wb_budget({
                'budget_w': max(0, int(iAVal)),
                'iAVal_w': int(iAVal),
                'raw_iAVal_w': int(iAVal),
                'iFc_w': float(iFc),
                'iMinLade_w': float(iMinLade),
                'wbmin_recovery_w': int(wbmin_recovery_w),
                'state': 'wbmin_charge_recovery',
                'storage_state': 'wbmin_charge_recovery',
                'reason': reason[:100],
                'source': 'storage_manager',
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': max(0, int(iAVal)),
                    'bat_charge_request_w': int(iMinLade),
                    'prio_factor': 1.0,
                    'prio_reason': 'wbmin_charge_recovery',
                }
            })
            write_state({'state': 'wbmin_charge_recovery', 'reason': reason,
                         'mode': MODE_AUTO, 'val': int(maximumLadeleistung),
                         'soc': fBatt_SOC, 'wbminsoc': wbminsoc,
                         'wb_soc_hyst_pct': wb_soc_hyst,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                         'bat_w': iPower_Bat, 'wb_w': int(fPower_WB),
                         'iAVal_w': int(iAVal), 'iFc_w': iFc,
                         'iMinLade_w': iMinLade,
                         'wbmin_recovery_w': int(wbmin_recovery_w)})
            wb9_floor_hold = False
            time.sleep(CYCLE_S)
            continue

        if wb9_floor_hold:
            # WB laedt, aber der Speicher ist an/unter der wbminSoC-Schwelle
            # und die Ladekurve fordert keine Akkuladung. IDLE 0 sperrt nur
            # Entladung und verhindert ein zyklisches Nachladen aus der WB-Last.
            _tl_idle_hold_s = max(float(CYCLE_S) * 2.0, float(gf(cfg, 'tl_idle_hold_s', 45.0)))
            if iPower_Bat > 300:
                _idle_charge_violation_count += 1
                if _idle_charge_violation_count >= 2:
                    tl_idle_charge_hold_until = max(float(tl_idle_charge_hold_until), t0 + _tl_idle_hold_s)
            elif tl_idle_charge_hold_until <= t0:
                _idle_charge_violation_count = 0
            else:
                pass
            _idle_hold_active = tl_idle_charge_hold_until > t0
            _idle_force = _idle_charge_violation_count >= 2 or _idle_hold_active
            ctrl.send(MODE_IDLE, 0, force=_idle_force)
            state_name = 'wb9_wbminsoc_hold'
            reason = ('[%s] WB-MINSOC-HOLD SOC=%.1f%% <= %.1f%%+%.1f%% '
                      '(Mode%d, WB=%.0fW) -> Batterie IDLE, kein Ladebedarf') % (
                          dt.strftime('%H:%M'), fBatt_SOC, wbminsoc, wb_soc_hyst,
                          wb_mode_active, fPower_WB)
            if _idle_charge_violation_count >= 2:
                reason += ' [Nachdruecken: Akku laedt trotz IDLE %.0fW]' % iPower_Bat
            elif _idle_hold_active:
                reason += ' [IDLE-Halteanker %.0fs]' % max(0.0, tl_idle_charge_hold_until - t0)
            log.info(reason)
            iAVal = iPower_PV - max(0, iFc) - iPowerHome - int(wp_w) - int(fPower_WB)
            write_wb_budget({
                'budget_w': max(0, int(iAVal)),
                'iAVal_w': int(iAVal),
                'raw_iAVal_w': int(iAVal),
                'iFc_w': float(iFc),
                'iMinLade_w': float(iMinLade),
                'state': 'hold',
                'storage_state': state_name,
                'source': 'storage_manager',
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': max(0, int(iAVal)),
                    'bat_charge_request_w': int(iMinLade),
                    'prio_factor': 1.0,
                    'prio_reason': 'wbmin_hold',
                }
            })
            write_state({'state': state_name, 'reason': reason,
                         'mode': MODE_IDLE, 'val': 0, 'soc': fBatt_SOC,
                         'wbminsoc': wbminsoc, 'wb_soc_hyst_pct': wb_soc_hyst,
                         'wb9_discharge_gate': False,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                          'bat_w': iPower_Bat, 'wb_w': int(fPower_WB),
                          'iAVal_w': int(iAVal), 'iFc_w': iFc,
                          'iMinLade_w': iMinLade})
            time.sleep(CYCLE_S)
            continue
        # TL-IDLE ist kein Freilauf. Wenn die Sollkurve aktiv ist und iFc=0
        # lautet der C++-nahe Auftrag eindeutig: IDLE 0 und Zyklus beenden.
        # Nur iLMStatus=1 reicht nicht: der alte Eba-Pfad berechnet aus einer
        # bereits laufenden Batterieladung sonst wieder iPower/Req und faellt
        # trotz "kein Ladeauftrag" zurueck in CHRG.
        if (tl_active and iFc <= 0 and iPower_PV > 500 and awattar_mode != 2
                and not _price_boost_any
                and not _abregel_pressure and pd_state != 'active'):
            _curve_ref = tl_soc_now if tl_soc_now is not None else fLadeende
            _tl_relief = _curve_wb_relief_budget(_curve_ref)
            _tl_relief_has_real_wb = bool(_tl_relief.get('active') and _curve_relief_has_real_consumer())
            if (
                fPower_Grid_ctrl > tl_grid_limit_w
                and fBatt_SOC > ep_reserve + 0.5
                and not _tl_relief_has_real_wb
            ):
                _tl_guard_w = int(min(
                    maximumLadeleistung * 0.95,
                    max(300.0, fPower_Grid_ctrl + 350.0)
                ))
                ctrl.send(MODE_DISCH, _tl_guard_w, force=True)
                _tl_idle_reason = (
                    '[%s] KURVEN-HALTEWAECHTER: iFc=0, SOC=%.1f%% >= Kurve %.1f%%, '
                    'Grid=%.0fW/%.0fW -> DISCH %dW statt Auto/CHRG'
                ) % (
                    dt.strftime('%H:%M'), fBatt_SOC, _curve_ref,
                    fPower_Grid, fPower_Grid_ctrl, _tl_guard_w
                )
                _tl_idle_mode = MODE_DISCH
                _tl_idle_val = _tl_guard_w
                _tl_idle_state = 'tl_idle_grid_guard'
                _tl_idle_budget_w = int(_tl_relief.get('budget_w', 0) or 0)
            else:
                _tl_idle_hold_s = max(float(CYCLE_S) * 2.0, float(gf(cfg, 'tl_idle_hold_s', 45.0)))
                if iPower_Bat > 300:
                    _idle_charge_violation_count += 1
                    if _idle_charge_violation_count >= 2:
                        tl_idle_charge_hold_until = max(float(tl_idle_charge_hold_until), t0 + _tl_idle_hold_s)
                elif tl_idle_charge_hold_until <= t0:
                    _idle_charge_violation_count = 0
                else:
                    pass
                _idle_hold_active = tl_idle_charge_hold_until > t0
                _idle_force = _idle_charge_violation_count >= 2 or _idle_hold_active
                if _tl_relief_has_real_wb and not _idle_force:
                    ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
                    _tl_idle_reason = (
                        '[%s] KURVEN-AUTO: iFc=0, SOC=%.1f%% >= Kurve %.1f%% '
                        '-> Fremd-WB aktiv, E3DC autonom'
                    ) % (dt.strftime('%H:%M'), fBatt_SOC, _curve_ref)
                    _tl_idle_mode = MODE_AUTO
                    _tl_idle_val = int(maximumLadeleistung)
                    _tl_idle_state = 'tl_curve_auto_relief'
                else:
                    ctrl.send(MODE_IDLE, 0, force=_idle_force)
                    _tl_idle_reason = (
                        '[%s] KURVEN-HALT: iFc=0, SOC=%.1f%% >= Kurve %.1f%% '
                        '-> IDLE, kein Auto/CHRG'
                    ) % (dt.strftime('%H:%M'), fBatt_SOC, _curve_ref)
                    _tl_idle_mode = MODE_IDLE
                    _tl_idle_val = 0
                    _tl_idle_state = 'idle'
                if _idle_charge_violation_count >= 2:
                    _tl_idle_reason += ' [Nachdruecken: Akku laedt trotz IDLE %.0fW]' % iPower_Bat
                elif _idle_hold_active:
                    _tl_idle_reason += ' [IDLE-Halteanker %.0fs]' % max(0.0, tl_idle_charge_hold_until - t0)
                _tl_idle_budget_w = max(0, int(iPower_PV - iPowerHome_budget - int(wp_w) - int(fPower_WB)))
                if _tl_relief.get('active'):
                    _tl_idle_budget_w = max(_tl_idle_budget_w, int(_tl_relief.get('budget_w', 0) or 0))
            if _tl_relief.get('active'):
                _tl_idle_reason += ' [WB-Kurvenentlastung %dW, +%.1f%%]' % (
                    int(_tl_relief.get('budget_w', 0) or 0),
                    float(_tl_relief.get('curve_excess_pct', 0.0))
                )

            log.info(_tl_idle_reason)
            iE3DC_Req_Load_alt = 0
            write_state({'state': _tl_idle_state, 'reason': _tl_idle_reason,
                         'mode': _tl_idle_mode, 'val': _tl_idle_val,
                         'soc': fBatt_SOC, 'ladeende': _curve_ref,
                         'tl_soc_now': tl_soc_now, 'tl_soc_target': tl_soc_target,
                         'tl_ts_target': tl_ts_target,
                         'iFc_w': 0, 'iMinLade_w': 0,
                         'pv_w': iPower_PV, 'grid_w': int(fPower_Grid),
                         'grid_ema_w': int(fPower_Grid_ctrl),
                         'bat_w': iPower_Bat})
            write_wb_budget({
                'budget_w': _tl_idle_budget_w,
                'iAVal_w': _tl_idle_budget_w,
                'raw_iAVal_w': _tl_idle_budget_w,
                'iFc_w': 0.0,
                'iMinLade_w': 0.0,
                'state': _tl_idle_state,
                'storage_state': _tl_idle_state,
                'reason': _tl_idle_reason[:100],
                'curve_wb_relief': bool(_tl_relief.get('active')),
                'curve_ref_soc': _tl_relief.get('curve_ref_soc'),
                'curve_excess_pct': _tl_relief.get('curve_excess_pct'),
                'curve_relief_from_soc_w': _tl_relief.get('curve_relief_from_soc_w', 0),
                'wb_storage_extra_w': int(_tl_relief.get('budget_w', 0) or 0),
                'wb_storage_cap_w': max(0, int(fPower_WB) + int(_tl_relief.get('budget_w', 0) or 0)),
                'energy_score': {
                    'pv_surplus_w': int(iPower_PV - iPowerHome_budget - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w': _tl_idle_budget_w,
                    'bat_charge_request_w': 0,
                    'prio_factor': 1.0,
                    'prio_reason': 'tl_curve_wb_relief' if _tl_relief.get('active') else 'tl_curve_idle',
                }
            })
            time.sleep(CYCLE_S)
            continue

        if iLMStatus==1:
            # iDiffLadeleistung Korrektur (Zeile 5044-5049)
            iDiffLadeleistung=iBattLoad-iPower_Bat+iDiffLadeleistung
            if iDiffLadeleistung<0 or abs(iBattLoad)<=100: iDiffLadeleistung=0
            if iDiffLadeleistung>100: iDiffLadeleistung=100
            if abs(iPower+iDiffLadeleistung)>maximumLadeleistung: iDiffLadeleistung=0

            iBattLoad=iPower  # (Zeile 5062)

            # Freilauf-Check / TL-Kurven-Modus:
            # Wenn TL-Kurve aktiv und iFc > 0: iPower IMMER hart auf iFc begrenzen.
            # WICHTIG: Kein fPower_Grid check hier - der Freilauf darf die TL-Kurve NIEMALS
            # aufbrechen. Sonst setzt Freilauf iPower=12000 -> Req=12000 -> unlimitiertes Laden!
            if wb9_active:
                # C++-nah: WBProcess() berechnet iAvalPower lokal fuer die
                # Wallbox-Rampe. Es schickt daraus keinen direkten Speicher-
                # DISCH-Befehl. Der Speicher wird fuer die WB deshalb auf AUTO
                # freigegeben; der E3DC sieht die WB als Last und regelt Batterie
                # und Netzpunkt selbst. So vermeiden wir Phantom-/Sollwert-Dumps.
                iPower = maximumLadeleistung
            elif tl_active and iFc > 0:
                iPower = iFc  # Kurvenfolge: exakt iFc, immer (TL-Grenze gilt immer!)
            else:
                freilauf_erlaubt = (
                    iPower < maximumLadeleistung
                    and iPower > (iPower_Bat - int(fPower_Grid)) / 2
                    and iPower * 2 > iPower_Bat
                )

                if freilauf_erlaubt:
                    # Im Freilauf: Grid-Waechter (Zeile 5093)
                    if fPower_Grid>100 and iE3DC_Req_Load_alt<(maximumLadeleistung-1):
                        # Netzbezug vorhanden: iPower auf bat-grid reduzieren
                        iPower=int(iPower_Bat-fPower_Grid)
                        if iPower<-maximumLadeleistung: iPower=-maximumLadeleistung
                    else:
                        # Echter Freilauf (Zeile 5106): max = AUTO
                        iPower = maximumLadeleistung

            # iE3DC_Req_Load setzen (Zeile 5116-5120)
            if iPower>maximumLadeleistung:
                iE3DC_Req_Load=maximumLadeleistung-1
            else:
                iE3DC_Req_Load=int(iPower+iDiffLadeleistung)
            if iE3DC_Req_Load>maximumLadeleistung: iE3DC_Req_Load=maximumLadeleistung
            if wb9_active:
                # C++-nah: Mode 9/10 oeffnet die Speicherfreigabe, erzwingt
                # aber keine direkte DISCH-Leistung. Die Wallbox-Ampere werden
                # ueber iAval/Netzpunkt gefuehrt, der Speicher bleibt autonom.
                iE3DC_Req_Load = int(maximumLadeleistung)
            elif iFc != 0 and abs(iFc) <= maximumLadeleistung:
                # Bei aktiver Ladekurve ist iFc selbst die Fuehrungsgroesse:
                # positiv = CHRG, negativ = DISCH. Die Eba-Diff-Korrektur
                # wuerde sonst zyklisch +/-100W aufschlagen, waehrend der
                # iFc-Cap direkt danach wieder auf iFc begrenzt.
                # Das erzeugt sichtbare Grid-/Hausleistungswellen.
                iE3DC_Req_Load = int(iFc)

            # Eba sendet im Normalpfad nur bei PV. WB-Mode 10 ist die bewusste
            # Ausnahme: Batterie fuer die Wallbox auch ohne PV freigeben.
            if iPower_PV>0 or wb9_active:
                # Freilauf-Erkennung (Zeile 5134-5166)
                # WICHTIG: iFc verhindert Freilauf! Wenn eine Kurvenfuehrung aktiv ist,
                # niemals iLMStatus=3 setzen - das wuerde MAX_CHARGE_POWER aufheben!
                if not (iFc > 0 and iFc <= maximumLadeleistung):  # Nur Freilauf wenn keine iFc-Kurve aktiv
                    if ((iE3DC_Req_Load_alt>=(maximumLadeleistung-1)
                         and iE3DC_Req_Load>=(maximumLadeleistung-1))):
                        iLMStatus=3  # Freilauf
                    elif iE3DC_Req_Load==maximumLadeleistung:
                        iLMStatus=3
                        iE3DC_Req_Load_alt=iE3DC_Req_Load
                    else:
                        iLMStatus=-6  # Senden initiieren
                else:
                    # TL aktiv: immer explizit senden (nie Freilauf)
                    iLMStatus=-6

                # Mode-Mapping und RSCP senden (normaler Eba-Modus, kein TL)
                if (
                    tl_active and iFc <= 0 and iE3DC_Req_Load == 0
                    and iPower_PV > 500 and awattar_mode != 2 and not wb9_active
                    and fPower_Grid_ctrl > tl_grid_limit_w
                    and fBatt_SOC > ep_reserve + 0.5
                ):
                    _tl_grid_guard_discharge = int(min(
                        maximumLadeleistung * 0.95,
                        max(300.0, fPower_Grid_ctrl + 350.0)
                    ))
                    tl_idle_grid_guard_hold_w = max(int(tl_idle_grid_guard_hold_w), _tl_grid_guard_discharge)
                    tl_idle_grid_guard_hold_until = max(float(tl_idle_grid_guard_hold_until), t0 + 45.0)
                    iE3DC_Req_Load = -_tl_grid_guard_discharge
                    log.info('KURVEN-HALTEWAECHTER: Grid=%.0fW/%.0fW -> DISCH %dW statt IDLE' % (
                        fPower_Grid, fPower_Grid_ctrl, _tl_grid_guard_discharge))
                elif (
                    tl_active and iFc <= 0 and iE3DC_Req_Load == 0
                    and iPower_PV > 500 and awattar_mode != 2 and not wb9_active
                    and tl_idle_grid_guard_hold_until > t0
                    and tl_idle_grid_guard_hold_w > 0
                    and fBatt_SOC > ep_reserve + 0.5
                ):
                    _tl_grid_guard_discharge = int(min(
                        maximumLadeleistung * 0.95,
                        tl_idle_grid_guard_hold_w
                    ))
                    iE3DC_Req_Load = -_tl_grid_guard_discharge
                    log.info('KURVEN-HALTEWAECHTER Halt: DISCH %dW noch %.0fs statt IDLE' % (
                        _tl_grid_guard_discharge,
                        max(0.0, tl_idle_grid_guard_hold_until - t0)))
                elif tl_idle_grid_guard_hold_until <= t0:
                    tl_idle_grid_guard_hold_w = 0

                _tl_idle_charge_hold = (
                    tl_active and iFc <= 0 and iE3DC_Req_Load == 0
                    and iPower_PV > 500 and awattar_mode != 2 and not wb9_active
                )
                if _tl_idle_charge_hold:
                    _tl_idle_hold_s = max(float(CYCLE_S) * 2.0, float(gf(cfg, 'tl_idle_hold_s', 45.0)))
                    if iPower_Bat > 300:
                        _idle_charge_violation_count += 1
                        if _idle_charge_violation_count >= 2:
                            tl_idle_charge_hold_until = max(float(tl_idle_charge_hold_until), t0 + _tl_idle_hold_s)
                    elif tl_idle_charge_hold_until <= t0:
                        _idle_charge_violation_count = 0
                    else:
                        pass
                    # C++-nah: im TL-IDLE-Haltefall nur EMS_REQ_SET_POWER
                    # IDLE 0 senden. Kein zusaetzliches MAX_CHARGE_POWER, weil
                    # diese Begrenzung auf manchen Anlagen selbst wieder Pulse
                    # anstossen kann.
                    _idle_force = True
                elif tl_active and iFc <= 0:
                    _idle_charge_violation_count = 0
                    ctrl.set_max_charge_power(0)  # Freigabe nach TL-Ende
                mode,val=eba_mode(iE3DC_Req_Load,maximumLadeleistung)

                # Eba/C++ sendet im normalen Pfad nur EMS_REQ_SET_POWER.
                # In der TL-Kurve darf hier kein CHRG vor dem iFc-Cap gesendet
                # werden: bei Netzbezug wuerde der harte CHRG-Befehl sonst schon
                # draussen sein, bevor der Grid-Waechter unten abbrechen kann.
                # Preis-Boost sendet ebenfalls exklusiv im Boost-Block.
                if not _price_boost_any and not (iFc > 0 and iFc <= maximumLadeleistung and not wb9_active):
                    ctrl.send(mode, val, force=(force or _tl_idle_charge_hold))

                iE3DC_Req_Load_alt=iE3DC_Req_Load
                # TL aktiv: Req_Load_alt auf iFc zuruecksetzen damit Freilauf-Erkennung
                # nicht durch alten Freilauf-Wert (12000W) erneut triggert!
                if iFc > 0 and iFc <= maximumLadeleistung and iE3DC_Req_Load_alt >= (maximumLadeleistung - 1):
                    iE3DC_Req_Load_alt = int(iFc)
            else:
                    # Kein PV: iLMStatus auf 11 setzen = warten (Zeile 5188)
                    if iLMStatus>0: iLMStatus=11

        # ----------------------------------------------------------------
        # iFc-Kurvencap: Eba berechnet iFc als Fuehrungsgroesse der Ladekurve.
        # Auf diesem System interpretiert die E3DC-Firmware CHRG(iFc) aber nicht
        # als harte Obergrenze und laedt freien PV-Ueberschuss trotzdem weiter ein.
        # Deshalb pflanzen wir iFc in den alten Eba-Pfad ein und geben ihn zusaetzlich
        # als MAX_CHARGE_POWER mit. Der normale SET_POWER-Pfad bleibt unveraendert.
        # ----------------------------------------------------------------
        _in_freilauf = (iE3DC_Req_Load >= maximumLadeleistung - 1)
        # TL+iFc aktiv: niemals Freilauf-Zustand annehmen, auch wenn Req noch alt=12000.
        if tl_active and iFc > 0:
            _in_freilauf = False

        _price_boost_battery = price_boost_allows_battery()
        _price_boost_sent = False
        _price_boost_w = 0
        _ifc_grid_blocked_this_cycle = False
        _ifc_cap_sent_this_cycle = False
        _ifc_cap_sent_w = 0
        _ifc_auto_quiet_this_cycle = False
        _ifc_auto_release_this_cycle = False
        _ifc_wb_auto_this_cycle = False
        _ifc_wb_auto_reason = ''
        _ifc_wb_possible_power_w = 0
        wb_fine_trim_w = 0
        wb_fine_trim_step_w = 0
        wb_fine_trim_reason = ''
        try:
            _wb_native_set_amp = int(float(_wb_native.get('set_amp', 0) or 0))
            _wb_native_cap_amp = int(float(_wb_native.get('cap_amp', 0) or 0))
        except Exception:
            _wb_native_set_amp = 0
            _wb_native_cap_amp = 0
        _wb_control_active_for_ifc = bool(
            wb_measured_for_storage
            or (
                wb_intent_fresh
                and wb_intent_car_active
                and normalize_wb_mode(wb_intent_mode) != MODE_OFF
                and (int(wb_intent_set_amp) > 0 or _wb_native_set_amp > 0 or _wb_native_cap_amp > 0)
            )
        )
        _wb_can_absorb_pv_surplus = False
        try:
            _wb_possible_power_w = 0.0
            _wb_details = _wb_native.get('wb_details') or []
            for _d in _wb_details:
                _detail_active = bool(
                    _d.get('plug')
                    or _d.get('charging')
                    or int(float(_d.get('amp', 0) or 0)) > 0
                    or abs(float(_d.get('power_w', 0) or 0)) > 500
                )
                if not _detail_active:
                    continue
                _detail_amp = int(float(_d.get('max_amp', 0) or 0))
                if _detail_amp <= 0:
                    _detail_amp = int(float(_wb_native.get('wb_max_amp', gf(cfg, 'wbmaxladestrom', 16)) or 16))
                _detail_phase = int(float(
                    _d.get('phases_target',
                    _d.get('phases_in_use',
                    _d.get('phases_actual',
                    _wb_native.get('detected_phases', wb_intent_phases)))) or 1
                ))
                if _detail_phase not in (1, 2, 3):
                    _detail_phase = 1
                _wb_possible_power_w += max(0.0, min(32, max(0, _detail_amp)) * 230.0 * _detail_phase)
            if _wb_possible_power_w <= 0:
                _fallback_amp = max(
                    int(float(_wb_native.get('wb_max_amp', 0) or 0)),
                    int(float(_wb_native.get('wb_global_max_amp', 0) or 0)),
                    int(float(gf(cfg, 'wbmaxladestrom', 16))),
                )
                _fallback_amp = max(6, min(32, _fallback_amp))
                _fallback_phase = max(
                    int(float(_wb_native.get('detected_phases', 0) or 0)),
                    int(float(wb_intent_phases or 0)),
                    1,
                )
                if _fallback_phase not in (1, 2, 3):
                    _fallback_phase = 1
                _wb_possible_power_w = _fallback_amp * 230.0 * _fallback_phase
            _ifc_wb_possible_power_w = int(_wb_possible_power_w)
            _pv_after_fixed_load_w = max(0.0, float(iPower_PV) - float(iPowerHome_budget) - float(wp_w))
            _wb_can_absorb_pv_surplus = bool(
                _wb_control_active_for_ifc
                and _wb_possible_power_w > 0
                and _pv_after_fixed_load_w <= (_wb_possible_power_w + 350.0)
            )
        except Exception:
            _ifc_wb_possible_power_w = 0
            _wb_can_absorb_pv_surplus = False
        if _price_boost_battery and iPower_PV > 500 and not wb9_active and awattar_mode != 2:
            # Preis-Boost wie Eba awtest=3: Netzladen aktiv lassen.
            # Nicht auf IDLE ausweichen; einige E3DC-Firmwares ziehen dann bei
            # grosser Wallbox-Last sofort wieder aus dem Akku.
            _boost_w = int(gf(cfg, 'cheap_grid_battery_max_w', 0))
            if _boost_w <= 0:
                _boost_w = int(maximumLadeleistung)
            _boost_w = max(300, min(int(maximumLadeleistung), _boost_w))
            if 0 <= getattr(ctrl, '_ldcap', -1) <= 50:
                ctrl.set_max_discharge_power(int(maximumLadeleistung))
            ctrl.set_max_charge_power(_boost_w)
            ctrl.send(MODE_GRID, _boost_w, force=force)
            _price_boost_sent = True
            _price_boost_w = _boost_w
            log.info('Preis-Boost Speicher: GRID=%dW aktiv (WB=%.0fW Bat=%dW Grid=%dW)' % (
                _boost_w, fPower_WB, iPower_Bat, int(fPower_Grid)))
        elif iFc > 0 and iFc <= maximumLadeleistung and iPower_PV > 500 and not _in_freilauf and not wb9_active and awattar_mode != 2:
            if 0 <= getattr(ctrl, '_ldcap', -1) <= 50:
                ctrl.set_max_discharge_power(int(maximumLadeleistung))
            _ifc_base = int(iFc)
            _ifc_cap = _ifc_base
            # Wallbox-Feintrimm:
            # Die WB kann nur in ganzen Ampere-Stufen regeln. Solange die
            # aktuelle Einspeisereserve fuer +1A plus Puffer nicht reicht,
            # nimmt der Speicher diesen Rest oberhalb iFc auf. Sobald genug
            # fuer die naechste WB-Stufe vorhanden ist, faellt der Restpuffer
            # weg und die Wallbox bekommt Vorrang. C++-nah bleibt der
            # Netzpunkt leicht negativ: lieber kurz einspeisen als beziehen.
            try:
                _wb_charging_now = abs(float(fPower_WB or 0.0)) > 500 or wb_intent_charging
                _wb_set_amp = int(float(
                    _wb_native.get('set_amp',
                    _wb_native.get('cap_amp',
                    _wb_native.get('amp', 0))) or 0
                ))
                _wb_phases = int(float(
                    _wb_native.get('detected_phases',
                    _wb_native.get('phases',
                    _wb_native.get('phase_count', 0))) or 0
                ))
                if _wb_phases not in (1, 2, 3):
                    _wb_phases = 3 if abs(float(fPower_WB or 0.0)) > 4200 else 1
                if _wb_set_amp <= 0 and abs(float(fPower_WB or 0.0)) > 500:
                    _wb_set_amp = max(1, int(round(abs(float(fPower_WB or 0.0)) / (230.0 * max(1, _wb_phases)))))
                _wb_max_amp = int(float(_wb_native.get('wb_max_amp', gf(cfg, 'wbmaxladestrom', 16)) or 16))
                try:
                    _active_detail_limits = [
                        int(float(_d.get('max_amp', 0) or 0))
                        for _d in (_wb_native.get('wb_details') or [])
                        if (
                            bool(_d.get('charging', False))
                            or int(float(_d.get('amp', 0) or 0)) > 0
                            or abs(float(_d.get('power_w', 0) or 0)) > 500
                        )
                    ]
                    if _active_detail_limits:
                        _wb_max_amp = max(_active_detail_limits)
                except Exception:
                    pass
                _wb_max_amp = max(6, min(32, _wb_max_amp))
                wb_fine_trim_step_w = int(230 * max(1, _wb_phases))
                _wb_step_buffer_w = 450
                _grid_reserve_for_trim_w = 350
                _actual_above_ifc_w = max(0, int(iPower_Bat) - _ifc_base)
                if _wb_charging_now and fPower_Grid_ctrl <= -50 and wb_fine_trim_step_w > 0:
                    if _wb_set_amp != wb_fine_last_amp:
                        wb_fine_next_step_count = 0
                        wb_fine_trim_hold_w = 0
                        wb_fine_last_amp = _wb_set_amp
                    _raw_room_w = max(0, int(-float(fPower_Grid_ctrl) - _grid_reserve_for_trim_w))
                    _raw_room_w += _actual_above_ifc_w
                    _next_amp_raw = (
                        _wb_set_amp > 0
                        and _wb_set_amp < _wb_max_amp
                        and _raw_room_w >= (wb_fine_trim_step_w + _wb_step_buffer_w)
                    )
                    if _next_amp_raw:
                        wb_fine_next_step_count = min(10, wb_fine_next_step_count + 1)
                    else:
                        wb_fine_next_step_count = 0
                    _next_amp_possible = _next_amp_raw and wb_fine_next_step_count >= 6
                    _target_trim_w = 0
                    if not _next_amp_possible:
                        _max_rest_trim_w = max(0, wb_fine_trim_step_w - 250)
                        _target_trim_w = min(
                            max(0, _raw_room_w),
                            _max_rest_trim_w,
                            max(0, int(maximumLadeleistung) - _ifc_base)
                        )
                        if _target_trim_w < 150:
                            _target_trim_w = 0
                    if _target_trim_w > wb_fine_trim_hold_w:
                        wb_fine_trim_hold_w += min(150, int(_target_trim_w) - wb_fine_trim_hold_w)
                    else:
                        wb_fine_trim_hold_w -= min(300, wb_fine_trim_hold_w - int(_target_trim_w))
                    if wb_fine_trim_hold_w < 150:
                        wb_fine_trim_hold_w = 0
                    wb_fine_trim_w = int(max(0, wb_fine_trim_hold_w))
                    if wb_fine_trim_w > 0:
                        _ifc_cap = min(int(maximumLadeleistung), _ifc_base + int(wb_fine_trim_w))
                        wb_fine_trim_reason = (
                            'WB-Restpuffer %dW (Stufe %dW, Amp=%d/%d, stabil=%d/6)'
                            % (wb_fine_trim_w, wb_fine_trim_step_w, _wb_set_amp, _wb_max_amp, wb_fine_next_step_count)
                        )
                else:
                    wb_fine_next_step_count = 0
                    wb_fine_trim_hold_w = 0
            except Exception:
                wb_fine_trim_w = 0
                wb_fine_trim_step_w = 0
                wb_fine_trim_reason = ''
                wb_fine_next_step_count = 0
                wb_fine_trim_hold_w = 0
            _ifc_skip = False
            # C++-naher Netzpunkt-Waechter: bei Netzbezug nicht hart zwischen
            # CHRG und IDLE springen, sondern aus aktueller Batterieleistung
            # und Netzreserve die noch sichere Ladeleistung ableiten.
            _grid_reserve_w = 300
            _grid_safe_cap = int(max(0, iPower_Bat) - fPower_Grid_ctrl - _grid_reserve_w)
            if _grid_safe_cap < _ifc_cap:
                _ifc_cap = max(0, _grid_safe_cap)
            if _ifc_grid_block:
                if t0 >= _ifc_grid_block_until and _ifc_cap >= 300:
                    _ifc_grid_block = False
                else:
                    _ifc_skip = True
            elif _ifc_cap < 300:
                _ifc_grid_block = True
                _ifc_grid_block_until = t0 + 45.0
                _ifc_cap_ramp = 0
                _ifc_skip = True

            # C++-naher Auto-Ruhebereich:
            # Wenn iFc eine Ladung fordert, die sichere Ladeleistung aber nur
            # aus einem zappelnden Netzpunkt oder aus zu wenig PV-Rest entsteht,
            # blockieren wir uns mit IDLE/kleinem CHRG selbst. Das C++-Programm
            # fiel in solchen Freilauf-Situationen eher auf AUTO zurueck. Der
            # E3DC darf dann kurz Hauslast und Batterie selbst auspendeln; erst
            # bei stabiler Reserve greift der Kurvencap wieder.
            if _wb_control_active_for_ifc and t0 < _ifc_auto_quiet_until:
                _ifc_auto_quiet_until = 0.0
                _ifc_auto_quiet_reason = ''
                log.info(
                    'iFc-Cap Auto-Ruhe beendet: Wallbox-Freigabe aktiv '
                    '(Amp=%d/%d, echte WB=%.0fW) -> CHRG/iFc bleibt Speicherdeckel'
                    % (_wb_native_set_amp, _wb_native_cap_amp, abs(float(fPower_WB or 0.0)))
                )
            _ifc_under_curve_now = False
            _ifc_above_curve_brake_now = False
            _ifc_far_target_now = False
            try:
                _ifc_under_curve_now = (
                    tl_active and tl_soc_now is not None
                    and fBatt_SOC < (float(tl_soc_now) - tl_soft_tolerance)
                )
                _ifc_above_curve_brake_now = (
                    tl_active and tl_soc_now is not None
                    and fBatt_SOC > (float(tl_soc_now) + tl_tolerance)
                )
                _ifc_far_target_now = (
                    fBatt_SOC < (float(fLadeende) - 5.0)
                    and not _ifc_above_curve_brake_now
                )
            except Exception:
                _ifc_under_curve_now = False
                _ifc_above_curve_brake_now = False
                _ifc_far_target_now = False
            if (tl_auto_quiet_enable and not _price_boost_battery
                    and not _wb_control_active_for_ifc
                    and (_ifc_under_curve_now or _ifc_far_target_now or tl_plan_unreachable)):
                _ifc_need_w = max(int(iFc), int(iMinLade), int(minimumLadeleistung))
                _safe_need_w = max(
                    int(tl_auto_quiet_min_chrg_w),
                    min(int(maximumLadeleistung), _ifc_need_w) - int(tl_auto_quiet_margin_w)
                )
                _low_safe_power = _ifc_cap < _safe_need_w
                _near_grid_zero = fPower_Grid_ctrl > -max(500, int(tl_auto_quiet_margin_w))
                _grid_noisy = abs(float(fPower_Grid) - float(fPower_Grid_ctrl)) > max(500, int(tl_auto_quiet_margin_w))
                _pv_too_small_for_need = iPower_PV < (_ifc_need_w + int(iPowerHome_budget) + int(tl_auto_quiet_margin_w))
                if _low_safe_power and (_ifc_skip or _near_grid_zero or _grid_noisy or _pv_too_small_for_need):
                    _ifc_auto_quiet_until = max(_ifc_auto_quiet_until, t0 + float(tl_auto_quiet_hold_s))
                    _ifc_auto_quiet_reason = (
                        'sichere Ladeleistung %dW < Bedarf %dW (Grid %.0f/%.0fW, PV %dW)'
                        % (_ifc_cap, _safe_need_w, fPower_Grid, fPower_Grid_ctrl, iPower_PV)
                    )
                    _ifc_cap_ramp = 0
            if t0 < _ifc_auto_quiet_until and not _price_boost_battery and not _wb_control_active_for_ifc:
                _ifc_skip = True

            _ifc_wb_auto = bool(
                _wb_control_active_for_ifc
                and _wb_can_absorb_pv_surplus
                and not _price_boost_battery
                and not _price_boost_any
                and pd_state != 'active'
                and not _abregel_pressure
            )

            if _ifc_wb_auto:
                # C++-nahe Rollenverteilung: Wenn die Wallbox die PV nach
                # Haus/WP aufnehmen kann, ist sie das Stellglied. Der E3DC
                # bleibt autonom; Speicher-CHRG greift erst wieder bei echter
                # Kurven-/Abregel-Notwendigkeit.
                ctrl.set_max_charge_power(0)
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=False)
                _ifc_wb_auto_this_cycle = True
                _ifc_grid_blocked_this_cycle = False
                _ifc_auto_quiet_this_cycle = False
                _ifc_auto_release_this_cycle = False
                _ifc_skip = True
                _ifc_cap_ramp = 0
                iE3DC_Req_Load = int(maximumLadeleistung)
                _ifc_wb_auto_reason = (
                    'iFc-Cap WB-AUTO: WB kann PV-Rest aufnehmen '
                    '(WBmax=%dW, PV=%dW, Haus=%dW, WP=%dW) -> E3DC autonom'
                    % (_ifc_wb_possible_power_w, iPower_PV, iPowerHome_budget, int(wp_w))
                )
                log_status_throttled('ifc_wb_auto', _ifc_wb_auto_reason, 30)
            elif _ifc_skip and not _price_boost_battery:
                if t0 < _ifc_auto_quiet_until:
                    ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=False)
                    _ifc_auto_quiet_this_cycle = True
                    _ifc_grid_blocked_this_cycle = False
                    iE3DC_Req_Load = int(maximumLadeleistung)
                    if (t0 - _ifc_auto_quiet_last_log) >= 25.0:
                        _ifc_auto_quiet_last_log = t0
                        log.info('iFc-Cap Auto-Ruhe: %s -> AUTO noch %.0fs (unter Kurve/Zielrueckstand)' % (
                            _ifc_auto_quiet_reason, max(0.0, _ifc_auto_quiet_until - t0)))
                else:
                    # Unter der Kurve bzw. bei nicht erreichbarem Tagesziel darf
                    # der Netzpunkt-Halt nicht auf IDLE zurueckfallen. Das waere
                    # fachlich das Gegenteil der C++-nahen Freigabe: Wenn wir zu
                    # weit hinten liegen, soll der E3DC autonom Hauslast und
                    # Batterieladung regeln. IDLE ist nur oberhalb/nahe der Kurve
                    # ein sinnvoller Haltebefehl.
                    _ifc_auto_release = False
                    try:
                        _under_curve = (
                            tl_active and tl_soc_now is not None
                            and fBatt_SOC < (float(tl_soc_now) - tl_soft_tolerance)
                        )
                        _above_curve_brake = (
                            tl_active and tl_soc_now is not None
                            and fBatt_SOC > (float(tl_soc_now) + tl_tolerance)
                        )
                        _far_under_target = (
                            fBatt_SOC < (float(fLadeende) - 5.0)
                            and not _above_curve_brake
                        )
                        _ifc_auto_release = (
                            _under_curve
                            or _far_under_target
                            or (tl_plan_unreachable and not _above_curve_brake)
                            or tl_end_auto_coast
                        )
                    except Exception:
                        _ifc_auto_release = False

                    if _ifc_auto_release:
                        # C++-naher Rueckfall: Wenn iFc viel fordert, aber die
                        # sichere CHRG-Leistung nur wegen fehlender Reserve <300W
                        # ist, darf AUTO die geringe PV-Einspeisung aufnehmen.
                        # Sonst blockieren wir uns unter der Kurve mit IDLE selbst.
                        ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=False)
                        _ifc_grid_blocked_this_cycle = False
                        _ifc_auto_release_this_cycle = True
                        iE3DC_Req_Load = int(maximumLadeleistung)
                        log.info('iFc-Cap Auto-Freigabe: Grid=%.0fW/%.0fW PV=%dW Bat=%dW SOC=%.1f%% Ziel=%.1f%% -> AUTO statt IDLE (unter Kurve/Zielrueckstand)' % (
                            fPower_Grid, fPower_Grid_ctrl, iPower_PV, iPower_Bat, fBatt_SOC, fLadeende))
                    else:
                        ctrl.send(MODE_IDLE, 0, force=True)
                        _ifc_grid_blocked_this_cycle = True
                        iE3DC_Req_Load = 0
                        log.info('iFc-Cap Grid-Block: Grid=%.0fW/%.0fW PV=%dW Bat=%dW -> IDLE(0) bis sichere Ladeleistung' % (
                            fPower_Grid, fPower_Grid_ctrl, iPower_PV, iPower_Bat))
            elif _ifc_cap < int(iFc):
                log.debug('iFc-Cap Grid-Waechter: Grid=%.0fW/%.0fW Bat=%.0fW -> CHRG reduziert %dW->%dW' % (
                    fPower_Grid, fPower_Grid_ctrl, iPower_Bat, int(iFc), _ifc_cap))
            if not _ifc_skip:
                if _ifc_cap_ramp <= 0:
                    _ifc_cap_ramp = min(_ifc_cap, 600)
                else:
                    _ifc_cap_ramp = min(_ifc_cap, _ifc_cap_ramp + 300)
                _ifc_cap = min(_ifc_cap, _ifc_cap_ramp)
                ctrl.set_max_charge_power(_ifc_cap)
                _charge_locked = bool(live.get('ems_charge_locked', False) or live.get('ems_charge_lock_time', False))
                if _charge_locked and (fPower_Grid_ctrl < -(_ifc_cap + 300) or _price_boost_battery):
                    # Manche E3DC sperren CHRG trotz PV-Ueberschuss, wenn eine
                    # interne Ladezeit-Sperre aktiv ist. GRID hebt diese Sperre
                    # auf. Ohne Preis-Boost nur bei Export, im Preisfenster auch
                    # mit bewusstem Netzbezug.
                    ctrl.send(MODE_GRID, _ifc_cap, force=False)
                    log.info('iFc-Cap: GRID-Fallback=%dW wegen E3DC-Ladesperre (Grid=%dW)' % (
                        _ifc_cap, int(fPower_Grid)))
                elif _price_boost_battery and fPower_Grid > 100:
                    ctrl.send(MODE_GRID, _ifc_cap, force=False)
                    log.info('iFc-Cap: Preis-Boost GRID=%dW trotz Netzbezug (Grid=%dW)' % (
                        _ifc_cap, int(fPower_Grid)))
                else:
                    # C++-nah: aktive Ladebegrenzung zyklisch nachsetzen.
                    # Einige E3DC-Firmwares fallen sonst nach wenigen Sekunden
                    # wieder in die interne PV-Ladung zurueck und ueberfahren iFc.
                    ctrl.send(MODE_CHRG, _ifc_cap, force=True)
                iE3DC_Req_Load = _ifc_cap
                _ifc_cap_sent_this_cycle = True
                _ifc_cap_sent_w = _ifc_cap
                if wb_fine_trim_w > 0:
                    log.info('iFc-Cap: CHRG=%dW SOC=%.1f%%->%.1f%% [%s]' % (
                        _ifc_cap, fBatt_SOC, fLadeende, wb_fine_trim_reason))
                else:
                    log.info('iFc-Cap: CHRG=%dW SOC=%.1f%%->%.1f%%' % (_ifc_cap, fBatt_SOC, fLadeende))
        elif tl_active and _in_freilauf:
            # Im Freilauf UND TL aktiv: Ladelimit auf iFc halten, NICHT freigeben!
            if iFc > 0:
                ctrl.set_max_charge_power(int(iFc))
                ctrl.send(MODE_AUTO, int(maximumLadeleistung), force=True)
                log.debug('TL-Freilauf: CAP=%.0fW (TL-Grenze)' % iFc)
            else:
                ctrl.set_max_charge_power(0)  # iFc=0 -> wirklich freigeben
        elif iFc <= 0 or _in_freilauf or iPower_PV <= 500 or wb9_active or awattar_mode == 2:
            # Kein aktiver Kurvencap: Limit freigeben.
            if (tl_active and iFc <= 0 and iE3DC_Req_Load == 0
                    and awattar_mode != 2 and not wb9_active and iPower_PV > 500):
                pass
            else:
                ctrl.set_max_charge_power(0)

        if wb9_active:
            # C++-nah: keine aktive DISCH-Fuehrung aus WB-Sollwerten. Die
            # berechnete Referenz bleibt nur Diagnose; EMS ist AUTO.
            if wb9_state != _last_wb9_state:
                log.info("[Mode9/10] SOC=%.0f%% > Min=%.0f%% -> %s, Referenz %dW" % (
                    fBatt_SOC, wbminsoc, wb9_state, discharge_w))
                _last_wb9_state = wb9_state


        # iLMStatus Countdown (Zeile 5197)
        if iLMStatus>1: iLMStatus-=1

        # Wenn iLMStatus durch Countdown auf 1 zurueck: naechste Runde aktiv
        # Negative iLMStatus: neg. Countdown, bei -1 wird gesendet
        if iLMStatus<0: iLMStatus+=1
        if iLMStatus==0: iLMStatus=1  # Sicherheit

        # ----------------------------------------------------------------
        # State + iAVal + WB-Budget
        # ----------------------------------------------------------------
        iAVal=iPower_PV-max(0,iFc)-iPowerHome_budget-int(wp_w)-int(fPower_WB)

        if _price_boost_sent:
            mode_last = MODE_GRID
            val_last = _price_boost_w
            state_name = 'price_boost_grid'
            rscp_sent = True
        elif _ifc_grid_blocked_this_cycle:
            mode_last = MODE_IDLE
            val_last = 0
            state_name = 'ifc_grid_hold'
            rscp_sent = True
        elif _ifc_auto_quiet_this_cycle:
            mode_last = MODE_AUTO
            val_last = int(maximumLadeleistung)
            state_name = 'tl_auto_quiet'
            rscp_sent = True
        elif _ifc_auto_release_this_cycle:
            mode_last = MODE_AUTO
            val_last = int(maximumLadeleistung)
            state_name = 'tl_auto_release'
            rscp_sent = True
        elif _ifc_wb_auto_this_cycle:
            mode_last = MODE_AUTO
            val_last = int(maximumLadeleistung)
            state_name = 'ifc_wb_auto'
            rscp_sent = True
        elif _ifc_cap_sent_this_cycle:
            mode_last = MODE_CHRG
            val_last = _ifc_cap_sent_w
            state_name = 'wbmin_charge_recovery' if wbmin_recovery_active else 'charge'
            rscp_sent = True
        elif wb9_active:
            mode_last = MODE_AUTO
            val_last = int(maximumLadeleistung)
            state_name = f'wb9_{wb9_state}'
            rscp_sent = True
        else:
            mode_last,val_last=eba_mode(iE3DC_Req_Load,maximumLadeleistung)
            state_name=('freilauf' if mode_last==MODE_AUTO
                        else 'charge' if mode_last in(MODE_CHRG,MODE_GRID)
                        else 'discharge' if mode_last==MODE_DISCH else 'idle')
            # Zeigt ob tatsaechlich ein RSCP Befehl gesendet wurde
            rscp_sent = iPower_PV>0 and iLMStatus not in range(2,12)

        _wb_trim_suffix = (' | WB-Restpuffer=%dW' % int(wb_fine_trim_w)) if wb_fine_trim_w > 0 else ''
        state_reason_labels = {
            'tl_auto_quiet': 'AUTO-RUHE',
            'tl_auto_release': 'AUTO-FREIGABE',
            'tl_curve_auto_relief': 'KURVEN-AUTO',
            'ifc_grid_hold': 'NETZPUNKT-HALT',
            'ifc_wb_auto': 'WB-AUTO',
            'tl_idle_grid_guard': 'KURVEN-HALTEWAECHTER',
            'tl_brake': 'KURVEN-BREMSE',
            'tl_brake_wb_relief_guard': 'WB-KURVENENTLASTUNG',
            'tl_autodump': 'KURVEN-DUMP',
            'price_boost_grid': 'PREIS-BOOST',
            'wbmin_charge_recovery': 'WB-MIN-AUTO',
        }
        state_reason_label = state_reason_labels.get(state_name, state_name.upper())
        reason=('[%s] %s%s SOC=%.1f%% Ziel=%.0f%% | iFc=%dW iMinLade=%dW | '
                'iPower=%dW Req=%dW | iLMSt=%d | Grid=%dW PV=%dW') % (
                dt.strftime('%H:%M'),state_reason_label,
                '' if rscp_sent else '(kein Senden)',
                fBatt_SOC,fLadeende,
                iFc,iMinLade,iPower,iE3DC_Req_Load,iLMStatus,
                int(fPower_Grid),iPower_PV)
        if _ifc_wb_auto_this_cycle and _ifc_wb_auto_reason:
            reason += ' | ' + _ifc_wb_auto_reason
        reason += _wb_trim_suffix

        if not wb9_active:
            log.info(reason)

        # iAVal: verfuegbare Leistung fuer Wallbox/WP
        # Eba: iMinLade = linearer Mindestwert (was Batterie MINDESTENS braucht)
        #       iFc    = Boost-Wert (was Batterie IDEAL will)
        # Fuer WB-Budget: iMinLade verwenden, damit WB nicht zu wenig bekommt.
        # Ausnahme Mode 4/9/10: Wenn wbminSoC-Hysterese offen ist, hat die
        # Wallbox Vorrang vor weiterer Speicherladung. In Mode 9/10 bleibt der
        # Speicher dabei C++-nah auf AUTO; der Storage Manager gibt nur einen
        # Deckel fuer die Wallbox-Rampe vor.
        _wb_wbmin_prio_open = (
            not _price_boost_any
            and storage_floor_mode(wb_intent_mode)
            and wb_intent_request == 'allow_discharge'
            and wb_intent_car_active
        )
        _budget_iMinLade = 0 if _wb_wbmin_prio_open else max(0, iMinLade)
        if _ifc_wb_auto_this_cycle:
            _budget_iMinLade = 0
        if wb_fine_trim_w > 0:
            _budget_iMinLade = max(0, int(iFc) + int(wb_fine_trim_w))
        _wr_limit_w = float(gf(cfg, 'wr_ac_limit_w', live.get('ac_power_limit_w', 0) or 11900))
        _live_ac_limit_w = float(live.get('ac_power_limit_w', 0) or 0)
        if _live_ac_limit_w > 1000:
            _wr_limit_w = _live_ac_limit_w
        if _wr_limit_w < 5000:
            _wr_limit_w = 11900.0
        _ext_pv_w = float(live.get('Ext_PV_Power', 0) or 0)
        _wr_clip_margin_w = float(iPower_PV) - float(_wr_limit_w) - float(_budget_iMinLade)
        _wb_budget_uses_wr_limit = bool(_wr_clip_margin_w >= 1000.0)
        if _wb_budget_uses_wr_limit:
            _wb_budget_source_w = float(_wr_limit_w) + max(0.0, _ext_pv_w)
            _wb_budget_storage_req_w = 0.0
        else:
            _wb_budget_source_w = float(iPower_PV) + max(0.0, _ext_pv_w)
            _wb_budget_storage_req_w = float(_budget_iMinLade)
        _wb_budget_consumers_w = float(iPowerHome_budget) + float(wp_w) + float(fPower_WB)
        iAVal = int(_wb_budget_source_w - _wb_budget_storage_req_w - _wb_budget_consumers_w)
        _wb_storage_cap_w = 0
        _wb_storage_extra_w = 0
        _wb_storage_reserve_w = 0
        if wb9_active:
            # C++-nahe Rollenverteilung:
            # - Storage Manager sendet AUTO an den E3DC, kein erzwungenes DISCH.
            # - Er sagt der WB aber, wie hoch sie maximal gehen darf.
            # Beispiel: 4500W erlaubte Batterie - 800W Haus - Reserve => ca.
            # 3300-3700W Gesamtdeckel fuer das Auto. Der Wallbox Manager setzt
            # daraus nur ganze Ampere und prueft den Netzpunkt.
            _wb_storage_reserve_w = int(max(250.0, float(gf(cfg, 'wb_storage_reserve_w', 350))))
            _wb_storage_cap_w = int(max(
                0.0,
                min(
                    float(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung)),
                    float(iPower_PV)
                    + float(gf(cfg, 'maximaleentladeleistung', maximumLadeleistung))
                    - float(iPowerHome_budget)
                    - float(wp_w)
                    - float(_wb_storage_reserve_w)
                )
            ))
            _wb_storage_extra_w = max(0, _wb_storage_cap_w - max(0, int(fPower_WB)))
            iAVal = _wb_storage_extra_w

        # Wenn der Speicher wegen iFc/Ladekurve PV-Leistung "wegreserviert",
        # darf echte Einspeisung nicht als 0W Wallbox-Budget enden. Sonst bleibt
        # die WB bei 6A, obwohl der Netzpunkt mehrere kW exportiert.
        _wb_export_relief_w = 0
        _wb_export_relief_active = False
        try:
            _wb_export_demand = bool(
                wb_measured_for_storage
                or wb_intent_charging
                or wb_intent_set_amp > 0
                or (
                    wb_intent_fresh
                    and wb_intent_car_active
                    and normalize_wb_mode(wb_intent_mode) != MODE_OFF
                )
            )
            if _wb_export_demand and not _price_boost_any and (
                (fPower_Grid_ctrl < -800.0 and fPower_Grid < 300.0)
                or fPower_Grid < -1200.0
            ):
                _grid_export_ema_w = max(0, int(-float(fPower_Grid_ctrl) - 500.0))
                _grid_export_raw_w = max(0, int(-float(fPower_Grid) - 700.0))
                if _grid_export_ema_w > 0:
                    _raw_guarded_w = min(_grid_export_raw_w, _grid_export_ema_w + 1600)
                else:
                    _raw_guarded_w = _grid_export_raw_w
                _wb_export_relief_w = int(max(_grid_export_ema_w, _raw_guarded_w))
                if _wb_export_relief_w > max(0, int(iAVal)):
                    iAVal = _wb_export_relief_w
                    _wb_export_relief_active = True
        except Exception:
            _wb_export_relief_w = 0
            _wb_export_relief_active = False

        consumer_priority_order = priority_order_from_config(cfg)
        consumer_priority_key = priority_order_key(consumer_priority_order)
        if consumer_priority_key != consumer_priority_last_key:
            if consumer_priority_last_key is not None:
                consumer_priority_changed_at = t0
                log.info('Verbraucherprioritaet geaendert: %s' % consumer_priority_key)
            consumer_priority_last_key = consumer_priority_key

        _heatpump_enabled = bool(
            cfg_bool(cfg, 'luxtronik', False)
            or str(cfg.get('wp_type', '')).strip() in ('0', '1', '3')
            or str(cfg.get('idm_ip', '')).strip() not in ('', '0.0.0.0')
            or str(cfg.get('luxtronik_ip', '')).strip() not in ('', '0.0.0.0')
        )
        _heater_enabled = bool(
            cfg_bool(cfg, 'heizstab', False)
            or str(cfg.get('heizstab_ip', '')).strip() not in ('', '0.0.0.0')
            or str(cfg.get('shelly_heiz_ip', '')).strip() not in ('', '0.0.0.0')
        )
        _wallbox_request_w = 0
        if wb_intent_fresh and wb_intent_car_active and normalize_wb_mode(wb_intent_mode) != MODE_OFF:
            _wallbox_request_w = max(
                max(0, int(iAVal)),
                int(max(0, wb_intent_set_amp) * 230 * max(1, int(wb_intent_phases or 1))),
                int(max(0.0, fPower_WB)),
            )
        _heatpump_request_w = 0
        if _heatpump_enabled and cfg_bool(cfg, 'auto_mode', True):
            _heatpump_request_w = int(max(
                abs(gf(cfg, 'grid_start_limit', -3500)),
                gf(cfg, 'idm_pv_surplus_max_kw', 2.0) * 1000.0,
                float(wp_w or 0.0),
            ))
        _heater_request_w = 0
        if _heater_enabled and cfg_bool(cfg, 'hs_auto_mode', True):
            _heater_request_w = int(max(
                gf(cfg, 'hs_min_surplus_w', 500),
                gf(cfg, 'heizstab_max_w', 3000),
                gf(cfg, 'shelly_heiz_w', 1500),
            ))
        _consumer_budget = allocate_consumer_budget(
            max(0, int(iAVal)),
            {
                "heatpump": _heatpump_request_w,
                "wallbox": _wallbox_request_w,
                "heater": _heater_request_w,
            },
            consumer_priority_order,
            previous_active=consumer_previous_active,
            priority_changed_at_s=consumer_priority_changed_at,
            now_s=t0,
            wp_runon_s=priority_runon_s_from_config(cfg),
        )
        _consumer_alloc = _consumer_budget["allocations"]
        _wallbox_budget_w = int(_consumer_alloc.get("wallbox", 0))
        _heatpump_budget_w = int(_consumer_alloc.get("heatpump", 0))
        _heater_budget_w = int(_consumer_alloc.get("heater", 0))
        try:
            _heater_live_w = abs(float(live.get('heizstab_power', live.get('Heizstab_Power', 0)) or 0))
        except Exception:
            _heater_live_w = 0.0
        consumer_previous_active = {
            "heatpump": bool(float(wp_w or 0.0) > 300.0),
            "wallbox": bool(wb_measured_for_storage or wb_intent_set_amp > 0 or _wallbox_budget_w >= 6 * 230),
            "heater": bool(_heater_live_w > 300.0 or _heater_budget_w >= 500),
        }

        write_state({'state':state_name,'reason':reason,'mode':mode_last,'val':val_last,
                     'soc':fBatt_SOC,'ladeende':fLadeende,'iFc_w':iFc,'iMinLade_w':iMinLade,
                     'iPower_w':iPower,'iE3DC_Req_Load':iE3DC_Req_Load,'iLMStatus':iLMStatus,
                     'fAvBatterie':round(fAvBatterie,1),'fAvBatterie900':round(fAvBatterie900,1),
                     'pv_w':iPower_PV,'grid_w':int(fPower_Grid),'grid_ema_w':int(fPower_Grid_ctrl),
                     'home_ema_w':int(iPowerHome_budget),'bat_w':iPower_Bat,
                     'iAVal_w':int(iAVal),'end_h':round(end_h,2),
                     'consumer_priority_order': _consumer_budget['consumer_priority_order'],
                     'consumer_priority_effective_order': _consumer_budget['consumer_priority_effective_order'],
                     'consumer_allocations': _consumer_alloc,
                     'consumer_requests_w': _consumer_budget['requests_w'],
                     'consumer_priority_wp_runon_active': bool(_consumer_budget['consumer_priority_wp_runon_active']),
                     'wb_fine_trim_w': int(wb_fine_trim_w),
                     'wb_fine_trim_step_w': int(wb_fine_trim_step_w),
                     'wb_fine_next_step_count': int(wb_fine_next_step_count),
                     'wb_export_relief_w': int(_wb_export_relief_w),
                     'wb_export_relief_active': bool(_wb_export_relief_active),
                     'wb_budget_source_w': int(_wb_budget_source_w),
                      'wb_budget_uses_wr_limit': bool(_wb_budget_uses_wr_limit),
                      'wb_wr_clip_margin_w': int(_wr_clip_margin_w),
                      'wb_possible_power_w': int(_ifc_wb_possible_power_w),
                      'ifc_wb_auto': bool(_ifc_wb_auto_this_cycle),
                      'wbmin_recovery_w': int(wbmin_recovery_w)})
        try:
            wb_budget = {
                'budget_w':       max(0, int(_wallbox_budget_w)),
                'budget_amp_1ph': (max(6, min(32, int(_wallbox_budget_w / 230))) if _wallbox_budget_w >= 6*230 else 0),
                'budget_amp_3ph': (max(6, min(32, int(_wallbox_budget_w / 690))) if _wallbox_budget_w >= 6*690 else 0),
                'iAVal_w':        int(iAVal),
                'raw_iAVal_w':    int(iAVal),
                'consumer_available_w': max(0, int(iAVal)),
                'consumer_allocations': _consumer_alloc,
                'consumer_requests_w': _consumer_budget['requests_w'],
                'consumer_priority_order': _consumer_budget['consumer_priority_order'],
                'consumer_priority_effective_order': _consumer_budget['consumer_priority_effective_order'],
                'consumer_priority_wp_runon_s': _consumer_budget['consumer_priority_wp_runon_s'],
                'consumer_priority_wp_runon_active': bool(_consumer_budget['consumer_priority_wp_runon_active']),
                'heatpump_budget_w': int(_heatpump_budget_w),
                'heater_budget_w': int(_heater_budget_w),
                'grid_ema_w':     int(fPower_Grid_ctrl),
                'home_ema_w':     int(iPowerHome_budget),
                'iFc_w':          float(iFc),
                'iMinLade_w':     float(_budget_iMinLade),
                'iMinLade_raw_w': float(iMinLade),
                'iMinLade2_w':    float(iMinLade2),
                'iBattLoad_w':    float(iBattLoad),
                'iMaxBattLade_w': float(iMaxBattLade),
                'iPower_Bat_w':   float(iPower_Bat),
                'fAvBatterie_w':  round(float(fAvBatterie), 1),
                'fAvBatterie900_w': round(float(fAvBatterie900), 1),
                'maximumLadeleistung_w': float(maximumLadeleistung),
                'wb_fine_trim_w': int(wb_fine_trim_w),
                'wb_fine_trim_step_w': int(wb_fine_trim_step_w),
                'wb_fine_next_step_count': int(wb_fine_next_step_count),
                'wbmin_recovery_w': int(wbmin_recovery_w),
                'wb_storage_cap_w': int(_wb_storage_cap_w),
                'wb_storage_extra_w': int(_wb_storage_extra_w),
                'wb_storage_reserve_w': int(_wb_storage_reserve_w),
                'wb_export_relief_w': int(_wb_export_relief_w),
                'wb_export_relief_active': bool(_wb_export_relief_active),
                'wb_budget_source_w': int(_wb_budget_source_w),
                'wb_budget_storage_req_w': int(_wb_budget_storage_req_w),
                'wb_budget_consumers_w': int(_wb_budget_consumers_w),
                'wb_budget_uses_wr_limit': bool(_wb_budget_uses_wr_limit),
                'wb_wr_limit_w': int(_wr_limit_w),
                'wb_wr_clip_margin_w': int(_wr_clip_margin_w),
                'wb_possible_power_w': int(_ifc_wb_possible_power_w),
                'ifc_wb_auto': bool(_ifc_wb_auto_this_cycle),
                'state':          state_name,
                'storage_state':  state_name,
                'reason':         reason[:100],
                'ts':             t0,
                # Kompatibilitaet mit energy_score Format des alten Managers
                'energy_score': {
                    'pv_surplus_w':        int(iPower_PV - iPowerHome_budget - int(wp_w) - int(fPower_WB)),
                    'free_for_limbs_w':    max(0, int(_wallbox_budget_w)),
                    'free_for_limbs_raw_w': max(0, int(iAVal)),
                    'consumer_allocations': _consumer_alloc,
                    'consumer_requests_w': _consumer_budget['requests_w'],
                    'consumer_priority_order': _consumer_budget['consumer_priority_order'],
                    'consumer_priority_effective_order': _consumer_budget['consumer_priority_effective_order'],
                    'bat_charge_request_w': int(_budget_iMinLade),
                    'wb_fine_trim_w':       int(wb_fine_trim_w),
                    'wb_export_relief_w':    int(_wb_export_relief_w),
                    'wb_budget_source_w':     int(_wb_budget_source_w),
                    'wb_budget_uses_wr_limit': bool(_wb_budget_uses_wr_limit),
                    'prio_factor':         1.0,
                    'prio_reason':         ('wb_export_relief' if _wb_export_relief_active
                                            else 'ifc_wb_auto' if _ifc_wb_auto_this_cycle
                                            else 'wb_restpuffer' if wb_fine_trim_w > 0
                                            else 'wbmin_gate_open' if _wb_wbmin_prio_open
                                            else 'eba_klon'),
                }
            }
            with open(WB_F,'w',encoding='utf-8') as f:
                json.dump(wb_budget, f)
        except: pass

        elapsed=time.time()-t0
        time.sleep(max(0.2,CYCLE_S-elapsed))

    # Beim Beenden: begrenzte Zustände defensiv auf IDLE halten.
    # Verhindert Vollgas-Blitze zwischen systemd stop/start.
    _last_state = json.load(open(STATE_F,'r',encoding='utf-8')) if os.path.exists(STATE_F) else {}
    _hold_idle_states = {'tl_brake', 'idle', 'abregelschutz', 'manual_override',
                         'ifc_grid_hold', 'wb9_wbminsoc_hold'}
    _shutdown_hold_idle = (_last_state.get('state') in _hold_idle_states)
    log.info('Beende - %s...' % ('IDLE halten' if _shutdown_hold_idle else 'AUTO freigeben'))
    ctrl.release(hold_idle=_shutdown_hold_idle)
    ctrl.close()
    write_state({'state':'stopped','reason':'Dienst beendet'})
    log.info('E3DC Storage Manager beendet.')

if __name__=='__main__':
    main()
