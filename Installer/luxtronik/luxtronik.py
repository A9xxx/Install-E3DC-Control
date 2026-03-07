import socket
import struct
import time
import json
import os

class LuxtronikModbus:
    def __init__(self, host=None, port=502):
        if host is None:
            # Standard-IP als Fallback, falls Config nicht lesbar
            host = '192.168.178.88'
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                config_file = os.path.join(script_dir, "config.lux.json")
                if os.path.exists(config_file):
                    with open(config_file, 'r') as f:
                        data = json.load(f)
                        host = data.get('luxtronik_ip', host)
            except:
                pass

        self.host = host
        self.port = port
        self.unit_id = 1
        self.socket = None

    def connect(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(3)
            self.socket.connect((self.host, self.port))
            return True
        except:
            return False

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = None

    def _recv_exact(self, n):
        data = b''
        while len(data) < n:
            chunk = self.socket.recv(n - len(data))
            if not chunk: break
            data += chunk
        return data

    def _send_request(self, func_code, addr, val_or_count):
        if not self.socket: return None
        # WICHTIG: Kurze Pause für die Hardware-Stabilität der Luxtronik
        time.sleep(0.2) 
        req = struct.pack('>HHHBBHH', 1, 0, 6, self.unit_id, func_code, addr, val_or_count)
        try:
            self.socket.sendall(req)
            header = self._recv_exact(9)
            if not header or len(header) < 9: return None
            
            if func_code == 6: # Write Single Register
                self._recv_exact(3) 
                return True
                
            byte_count = header[8]
            data = self._recv_exact(byte_count)
            if not data: return None
            return struct.unpack('>' + 'H' * (byte_count // 2), data)
        except:
            return None

    def read_all_sensors(self):
        data = {}
        def to_s(v): return struct.unpack('>h', struct.pack('>H', v))[0] / 10

        # 1. Wärmequellen-Status (10000)
        status_wp = self._send_request(4, 10000, 1)
        if status_wp:
            data['Verdichter_Ein'] = bool(status_wp[0] & 0x01)

        # 2. Betriebsart (10002)
        status_ba = self._send_request(4, 10002, 1)
        if status_ba:
            data['Betriebsart'] = status_ba[0]

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
        # 10300: Elektrische Leistungsaufnahme in Watt (Verdichter + System)
        # 10301: Thermische Heizleistung in Watt
        p_regs = self._send_request(4, 10300, 2)
        if p_regs:
            # Stromverbrauch in Watt (für das Dashboard "Akt. Verbrauch")
            data['Leistung_Heiz_kW'] = p_regs[0] /10
            
            # Heizleistung (Thermisch) umrechnen in kW
            # HINWEIS: Meistens wird der Wert direkt in Watt gesendet (/ 1000.0).
            # Falls im Dashboard später utopische Werte stehen (z.B. "6500 kW"), 
            # sendet deine Firmware den Wert in 100W-Schritten. 
            # Ändere es dann einfach auf: p_regs[1] / 10 
            data['Leistung_Verdichter_W'] = p_regs[1] *100

        # Energie Zähler
        e_elek_hw = self._send_request(4, 10310, 1)
        e_elek_lw = self._send_request(4, 10311, 1)
        if e_elek_hw and e_elek_lw:
            data['Energie_Elek_kWh'] = ((e_elek_hw[0] << 16) + e_elek_lw[0])
            
        e_waerme_hw = self._send_request(4, 10320, 1)
        e_waerme_lw = self._send_request(4, 10321, 1)
        if e_waerme_hw and e_waerme_lw:
            data['Energie_Waerme_kWh'] = ((e_waerme_hw[0] << 16) + e_waerme_lw[0])
        
        # 8. Inverter Frequenz (Register 10140=Soll, 10141=Ist)
        freq = self._send_request(4, 10140, 2)
        if freq:
            data['Verdichter_Frequenz_Soll'] = freq[0] / 10
            data['Verdichter_Frequenz_Ist'] = freq[1] / 10
            data['Verdichter_Frequenz'] = freq[1] / 10 # Hauptwert auf Ist setzen

        return data

    def read_shi_status(self):
        """Liest Holding Register für den SHI-Status"""
        data = {}
        # Holding Register ab 10000 (FC 03)
        regs = self._send_request(3, 10000, 10)
        if regs:
            data['HZ_Mode'] = regs[0]      # 10000
            data['HZ_Setpoint'] = regs[1] / 10 # 10001
            data['WW_Mode'] = regs[5]      # 10005
            data['WW_Setpoint'] = regs[6] / 10 # 10006
        else:
            # Fallback: Einzeln lesen falls Block-Read fehlschlägt
            r1 = self._send_request(3, 10000, 2)
            if r1: 
                data['HZ_Mode'] = r1[0]
                data['HZ_Setpoint'] = r1[1] / 10
            
            r2 = self._send_request(3, 10005, 2)
            if r2:
                data['WW_Mode'] = r2[0]
                data['WW_Setpoint'] = r2[1] / 10
        return data
    
    def write_ww_boost(self, mode, temp):
        """Schreibt Werte in das SHI für Warmwasser"""
        # Mode: 0=Aus, 1=Setpoint
        self._send_request(6, 10005, mode) # Register 10005
        # Temperatur mal 10
        self._send_request(6, 10006, int(temp * 10)) # Register 10006
    
    # Guten Morgen Boost zum Akku-leeren:
    def write_hz_boost(self, mode, setpoint=None):
        """
        Schreibt Werte für den Heizungs-Boost
        mode: 0=Auto/Aus, 1=Setpoint
        setpoint: Temperatur in °C (z.B. 35.0)
        """
        self._send_request(6, 10000, mode) # Register 10000: Heizung Modus
        if setpoint is not None:
            self._send_request(6, 10001, int(setpoint * 10)) # 10001: Sollwert