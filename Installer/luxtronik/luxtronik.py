import socket
import struct
import time
import json
import os
import threading
import logging


SHI_MODE_NO_INFLUENCE = 0
SHI_MODE_SETPOINT = 1
SHI_MODE_OFFSET = 2

STATUS_OFF = 0
STATUS_NO_DEMAND = 1
STATUS_DEMAND = 2
STATUS_ACTIVE = 3

def _read_e3dc_config_value(key, default=None):
    """Liest einen Wert aus e3dc_v4.json (Single Source of Truth)."""
    try:
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.exists(v4_path):
            with open(v4_path, 'r', encoding='utf-8') as f:
                v4_data = json.load(f)
                for k, v in v4_data.items():
                    if k.strip().lower() == key.lower():
                        return str(v).strip()
    except: pass
    return default

class LuxtronikModbus:
    def __init__(self, host=None, port=502):
        if host is None:
            host = _read_e3dc_config_value('luxtronik_ip', '0.0.0.0')

        self.host = host
        self.port = port
        self.unit_id = 1
        self.socket = None
        # Luxtronik SHI reagiert empfindlich auf parallele Requests. Auch wenn
        # der Energy Manager aktuell einthreadig arbeitet, bleibt der Treiber
        # selbst strikt serialisiert.
        self._io_lock = threading.RLock()

    def connect(self):
        with self._io_lock:
            if not self.host or self.host == '0.0.0.0':
                return False
            if self.socket is not None:
                return True
            try:
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.settimeout(3)
                self.socket.connect((self.host, self.port))
                return True
            except Exception:
                self.close()
                return False

    def close(self):
        with self._io_lock:
            if self.socket:
                try:
                    self.socket.close()
                finally:
                    self.socket = None

    def _recv_exact(self, n):
        data = b''
        while len(data) < n:
            chunk = self.socket.recv(n - len(data))
            if not chunk: break
            data += chunk
        return data

    def _send_request(self, func_code, addr, val_or_count):
        with self._io_lock:
            if not self.socket:
                return None
            # Bewährte Pause und Schreibreihenfolge nicht verändern.
            time.sleep(0.2)
            transaction_id = 1
            req = struct.pack(
                '>HHHBBHH',
                transaction_id,
                0,
                6,
                self.unit_id,
                func_code,
                addr,
                val_or_count,
            )
            try:
                self.socket.sendall(req)
                header = self._recv_exact(8)
                if not header or len(header) < 8:
                    return None

                resp_transaction, protocol_id, _length, unit_id, resp_func = struct.unpack('>HHHBB', header)
                if (
                    resp_transaction != transaction_id
                    or protocol_id != 0
                    or unit_id != self.unit_id
                ):
                    return None

                if resp_func >= 0x80:
                    err_code = self._recv_exact(1)
                    code = err_code[0] if err_code else "unbekannt"
                    logging.getLogger("EnergyManager").error(
                        f"Modbus Exception {code} bei FC {func_code} Register {addr}"
                    )
                    return False

                if resp_func != func_code:
                    return None

                if func_code == 6:
                    echo = self._recv_exact(4)
                    if not echo or len(echo) != 4:
                        return None
                    echo_addr, echo_value = struct.unpack('>HH', echo)
                    return echo_addr == addr and echo_value == val_or_count

                byte_count_byte = self._recv_exact(1)
                if not byte_count_byte:
                    return None
                byte_count = byte_count_byte[0]
                if byte_count != int(val_or_count) * 2:
                    return None

                data = self._recv_exact(byte_count)
                if not data or len(data) != byte_count:
                    return None
                return struct.unpack('>' + 'H' * (byte_count // 2), data)
            except Exception:
                return None

    def read_runtime_status(self):
        """Liest nur zusammenhängende, offiziell belegte Input-Register."""
        status_wp = self._send_request(4, 10000, 1)
        status_modes = self._send_request(4, 10002, 3)
        result = {
            'Runtime_Status_Valid': bool(
                status_wp
                and status_modes
                and len(status_modes) >= 3
            ),
            'Runtime_Status_Source': 'documented_ranges',
        }
        if status_wp:
            status_mask = int(status_wp[0])
            result['Status_Waermepumpe_Bitmask'] = status_mask
            result['Verdichter_Ein'] = bool(status_mask & 0x03)
        if status_modes and len(status_modes) >= 3:
            result['Betriebsart'] = int(status_modes[0])
            result['Status_Heizen'] = int(status_modes[1])
            result['Status_Warmwasser'] = int(status_modes[2])
        return result

    def read_all_sensors(self):
        data = {}
        def to_s(v): return struct.unpack('>h', struct.pack('>H', v))[0] / 10

        # 1. Physische Zustände (10000-10007) in einem Request.
        data.update(self.read_runtime_status())

        # 3. Fehlernummer (10201)
        status_err = self._send_request(4, 10201, 1)
        if status_err:
            data['Fehler_Nr'] = status_err[0]

        # 4. Temperaturen Block A: Heizungskreis (10100 - 10106)
        t_regs_a = self._send_request(4, 10100, 7)
        if t_regs_a:
            data['Ruecklauf_Soll'] = to_s(t_regs_a[1])   # 10101
            data['Ruecklauf_Ist'] = to_s(t_regs_a[0])    # 10100
            data['Ruecklauf_Extern'] = to_s(t_regs_a[2]) # 10102
            data['Vorlauf_Ist'] = to_s(t_regs_a[5])      # 10105

        # 5. Temperaturen Block B: Umwelt (10108 - 10111)
        t_regs_b = self._send_request(4, 10108, 4)
        if t_regs_b:
            data['Aussentemp'] = to_s(t_regs_b[0])        # 10108
            data['Aussentemp_Mittel'] = to_s(t_regs_b[1]) # 10109
            data['Sole_Ein'] = to_s(t_regs_b[2])          # 10110
            data['Sole_Aus'] = to_s(t_regs_b[3])          # 10111

        # 6. Warmwasser Block (10120 - 10121)
        ww_regs = self._send_request(4, 10120, 2)
        if ww_regs:
            data['Warmwasser_Ist'] = to_s(ww_regs[0])
            data['Warmwasser_Soll'] = to_s(ww_regs[1])

        # 7. Aktuelle Leistung (Elektrisch & Thermisch)
        # 10300: Thermische Heizleistung in kW (x10) -> z.B. 65 = 6.5 kW
        # 10301: Elektrische Leistungsaufnahme in W (x100) -> z.B. 15 = 1500 W
        p_regs = self._send_request(4, 10300, 2)
        if p_regs:
            data['Leistung_Heiz_kW'] = p_regs[0] / 10.0
            # Skalierung x100 für grobe 100W-Schritte (15 -> 1500W)
            data['Leistung_Verdichter_W'] = p_regs[1] * 100.0

        # Energie Zähler
        e_elek_hw = self._send_request(4, 10310, 1)
        e_elek_lw = self._send_request(4, 10311, 1)
        if e_elek_hw and e_elek_lw:
            data['Energie_Elek_kWh'] = ((e_elek_hw[0] << 16) + e_elek_lw[0])
            
        e_waerme_hw = self._send_request(4, 10320, 1)
        e_waerme_lw = self._send_request(4, 10321, 1)
        if e_waerme_hw and e_waerme_lw:
            data['Energie_Waerme_kWh'] = ((e_waerme_hw[0] << 16) + e_waerme_lw[0])
        
        return data

    def read_shi_status(self):
        """Liest SHI-Auftragsmodi; diese Werte sind keine physischen Zustände."""
        data = {}
        # Holding Register ab 10000 (FC 03)
        regs = self._send_request(3, 10000, 10)
        if regs:
            data['SHI_HZ_Mode'] = regs[0]      # 10000
            data['SHI_HZ_Setpoint'] = regs[1] / 10 # 10001
            data['SHI_WW_Mode'] = regs[5]      # 10005
            data['SHI_WW_Setpoint'] = regs[6] / 10 # 10006
        else:
            # Fallback: Einzeln lesen falls Block-Read fehlschlägt
            r1 = self._send_request(3, 10000, 2)
            if r1: 
                data['SHI_HZ_Mode'] = r1[0]
                data['SHI_HZ_Setpoint'] = r1[1] / 10
            
            r2 = self._send_request(3, 10005, 2)
            if r2:
                data['SHI_WW_Mode'] = r2[0]
                data['SHI_WW_Setpoint'] = r2[1] / 10
        # Kompatibilitätsfelder für bestehende Diagnosewerkzeuge.
        if 'SHI_HZ_Mode' in data:
            data['HZ_Mode'] = data['SHI_HZ_Mode']
            data['HZ_Setpoint'] = data['SHI_HZ_Setpoint']
        if 'SHI_WW_Mode' in data:
            data['WW_Mode'] = data['SHI_WW_Mode']
            data['WW_Setpoint'] = data['SHI_WW_Setpoint']
        return data
    
    def write_ww_boost(self, mode, temp):
        """
        Schreibt Werte in das SHI für Warmwasser
        mode: 0=keine Beeinflussung, 1=Setpoint
        temp: Temperatur in °C (z.B. 55.0)
        """
        res1 = self._send_request(6, 10005, mode) # Register 10005: WW Modus
        if res1 is not True:
            return res1
        
        # Temperatur schicken, da das SHI bei 'Setpoint' (1) einen Vorgabewert in 10006 erwartet
        if mode == 1 and temp is not None:
            time.sleep(0.3)
            return self._send_request(6, 10006, int(temp * 10)) # 10006: WW Sollwert in 0.1 °C
        return res1
    
    def write_zirkulation(self, mode):
        """
        Schreibt Werte in das SHI fuer Zirkulationspumpe (Register 10070).
        mode: 0=Zwang Aus, 1=Automatik (interner Timer), 2=Zwang Ein
        """
        return self._send_request(6, 10070, mode) # Register 10070: Zirkulation Modus

    # Guten Morgen Boost zum Akku-leeren:
    def write_hz_boost(self, mode, setpoint=None):
        """
        Schreibt Werte für den Heizungs-Boost
        mode: 0=keine Beeinflussung, 1=Setpoint
        setpoint: Temperatur in °C (z.B. 35.0)
        """
        # Zuerst Modus umstellen!
        res1 = self._send_request(6, 10000, mode) # Register 10000: Heizung Modus
        if res1 is not True:
            return res1
        
        # Temperatur nur schicken, wenn Modus = 1. Sonst -> 1313 Fehler!
        if mode == 1 and setpoint is not None:
            time.sleep(0.3)
            return self._send_request(6, 10001, int(setpoint * 10)) # 10001: Sollwert
        return res1
