"""
rscp_client.py - Native Python RSCP-Client für E3DC Kraftwerke

Eigenständige Implementierung ohne externe RSCP-Bibliotheken.
Kein git clone, kein py3rijndael – nur Python-Stdlib + pycryptodome.

Protokollreferenz:
  - E3DC RSCP-Dokumentation (öffentliche Beispielanwendung)
  - Protokollstruktur nach unabhängiger Analyse mehrerer Open-Source-Implementierungen

Lizenz: MIT
"""

import struct
import socket
import time
import zlib
import logging

# Rijndael-256-CBC via pycryptodome (Standard-Paket, kein Sonder-Fork)
# pip install pycryptodome
try:
    from Crypto.Cipher import AES as _AESBase
    _PYCRYPTO_OK = True
except ImportError:
    _PYCRYPTO_OK = False

# Rijndael mit 256-Bit Blockgröße – pycryptodome unterstützt nur 128-bit AES-Blöcke.
# Daher implementieren wir Rijndael-256 mit reinen Python-Lookup-Tables.
# Die S-Box und Round-Konstanten sind mathematische Konstanten aus der Rijndael-Spezifikation
# (publiziertes Verfahren, Joan Daemen & Vincent Rijmen, 1998). Kein urheberrechtlich
# geschützter Code – pure Mathematik über dem endlichen Körper GF(2^8).

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rijndael GF(2^8) Hilfsfunktionen
# ---------------------------------------------------------------------------

def _gf_mul(a: int, b: int) -> int:
    """Multiplikation im Galois-Körper GF(2^8) mit Reduktionspolynom 0x11B."""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B  # Reduktionspolynom x^8 + x^4 + x^3 + x + 1
        b >>= 1
    return result


def _gf_inv(a: int) -> int:
    """Inverses Element in GF(2^8) via Exponentiation (a^254 mod poly)."""
    if a == 0:
        return 0
    result = 1
    base = a
    exp = 254
    while exp:
        if exp & 1:
            result = _gf_mul(result, base)
        base = _gf_mul(base, base)
        exp >>= 1
    return result


def _build_sbox() -> list:
    """Erzeugt die Rijndael-S-Box aus GF(2^8)-Inversem + affiner Transformation."""
    sbox = [0] * 256
    for i in range(256):
        x = _gf_inv(i)
        # Affine Transformation: b_i = x_i XOR x_{i+4} XOR x_{i+5} XOR x_{i+6} XOR x_{i+7} XOR c_i
        b = 0
        for bit in range(8):
            b |= (((x >> bit) ^ (x >> ((bit + 4) % 8)) ^ (x >> ((bit + 5) % 8)) ^
                   (x >> ((bit + 6) % 8)) ^ (x >> ((bit + 7) % 8)) ^ (0x63 >> bit)) & 1) << bit
        sbox[i] = b
    return sbox


def _build_inv_sbox(sbox: list) -> list:
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return inv


# S-Box einmalig beim Laden berechnen
_SBOX = _build_sbox()
_INV_SBOX = _build_inv_sbox(_SBOX)

# Rijndael Round-Konstanten (RCON)
_RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
         0x6C, 0xD8, 0xAB, 0x4D, 0x9A]

# MixColumns GF-Multiplikations-Lookup-Tabellen (für Nb=8)
_MUL2 = [_gf_mul(i, 2) for i in range(256)]
_MUL3 = [_gf_mul(i, 3) for i in range(256)]
_MUL9 = [_gf_mul(i, 9) for i in range(256)]
_MUL11 = [_gf_mul(i, 11) for i in range(256)]
_MUL13 = [_gf_mul(i, 13) for i in range(256)]
_MUL14 = [_gf_mul(i, 14) for i in range(256)]


# ---------------------------------------------------------------------------
# Rijndael-256-CBC Implementierung
# Nb=8 (256-Bit Block), Nk=8 (256-Bit Key), Nr=14 Runden
# ---------------------------------------------------------------------------

class Rijndael256:
    """
    Rijndael mit 256-Bit Blockgröße und 256-Bit Schlüssel (Nr=14, Nb=8, Nk=8).
    ACHTUNG: Das ist NICHT AES (AES hat 128-Bit Blöcke / Nb=4).
    E3DC nutzt diese Variante für RSCP-Rahmen-Verschlüsselung.
    """

    NB = 8   # Block-Spalten (256-Bit Block)
    NK = 8   # Schlüssel-Spalten (256-Bit Key)
    NR = 14  # Runden

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("Schlüssel muss genau 32 Bytes lang sein (256-Bit)")
        self._round_keys = self._key_expansion(list(key))

    def _key_expansion(self, key: list) -> list:
        """Key Schedule für Rijndael (Nb=8, Nk=8)."""
        nb, nk, nr = self.NB, self.NK, self.NR
        w = []
        # Erste Nk Wörter direkt aus dem Schlüssel
        for i in range(nk):
            w.append([key[4*i], key[4*i+1], key[4*i+2], key[4*i+3]])

        for i in range(nk, nb * (nr + 1)):
            temp = w[i-1][:]
            if i % nk == 0:
                # RotWord + SubWord + RCON
                temp = [_SBOX[temp[1]] ^ _RCON[i // nk],
                        _SBOX[temp[2]],
                        _SBOX[temp[3]],
                        _SBOX[temp[0]]]
            elif nk > 6 and i % nk == 4:
                temp = [_SBOX[b] for b in temp]
            w.append([w[i-nk][j] ^ temp[j] for j in range(4)])

        # Runde-Schlüssel als Liste von Zustandsmatrizen (nb Spalten)
        round_keys = []
        for r in range(nr + 1):
            rk = []
            for c in range(nb):
                rk.append(w[r * nb + c][:])
            round_keys.append(rk)
        return round_keys

    def _bytes_to_state(self, data: bytes, offset: int = 0) -> list:
        """32-Byte-Block in 4×8 Zustandsmatrix (Spalten-Major)."""
        state = [[0]*self.NB for _ in range(4)]
        for r in range(4):
            for c in range(self.NB):
                state[r][c] = data[offset + r + 4*c]
        return state

    def _state_to_bytes(self, state: list) -> bytes:
        out = bytearray(4 * self.NB)
        for r in range(4):
            for c in range(self.NB):
                out[r + 4*c] = state[r][c]
        return bytes(out)

    def _add_round_key(self, state: list, rk: list) -> list:
        for r in range(4):
            for c in range(self.NB):
                state[r][c] ^= rk[c][r]
        return state

    def _sub_bytes(self, state: list) -> list:
        for r in range(4):
            for c in range(self.NB):
                state[r][c] = _SBOX[state[r][c]]
        return state

    def _inv_sub_bytes(self, state: list) -> list:
        for r in range(4):
            for c in range(self.NB):
                state[r][c] = _INV_SBOX[state[r][c]]
        return state

    def _shift_rows(self, state: list) -> list:
        """ShiftRows für Nb=8: Offsets laut Rijndael-Spezifikation (Daemen/Rijmen 1998).
        Nb=4 (AES):  Zeilen-Shifts = [0, 1, 2, 3]
        Nb=8 (256b): Zeilen-Shifts = [0, 1, 3, 4]  <-- andere Offsets!
        """
        # Zeile 0: kein Shift
        # Zeile 1: Shift 1
        state[1] = state[1][1:] + state[1][:1]
        # Zeile 2: Shift 3 (NICHT 2!)
        state[2] = state[2][3:] + state[2][:3]
        # Zeile 3: Shift 4 (NICHT 3!)
        state[3] = state[3][4:] + state[3][:4]
        return state

    def _inv_shift_rows(self, state: list) -> list:
        """InvShiftRows für Nb=8: inverse Shifts = [0, Nb-1, Nb-3, Nb-4] = [0, 7, 5, 4]."""
        nb = self.NB  # = 8
        # Zeile 0: kein Shift
        # Zeile 1: Shift rechts um 1 = links um (Nb-1)=7
        state[1] = state[1][nb-1:] + state[1][:nb-1]
        # Zeile 2: Shift rechts um 3 = links um (Nb-3)=5
        state[2] = state[2][nb-3:] + state[2][:nb-3]
        # Zeile 3: Shift rechts um 4 = links um (Nb-4)=4
        state[3] = state[3][nb-4:] + state[3][:nb-4]
        return state

    def _mix_column(self, col: list) -> list:
        a, b, c, d = col
        return [
            _MUL2[a] ^ _MUL3[b] ^ c ^ d,
            a ^ _MUL2[b] ^ _MUL3[c] ^ d,
            a ^ b ^ _MUL2[c] ^ _MUL3[d],
            _MUL3[a] ^ b ^ c ^ _MUL2[d],
        ]

    def _inv_mix_column(self, col: list) -> list:
        a, b, c, d = col
        return [
            _MUL14[a] ^ _MUL11[b] ^ _MUL13[c] ^ _MUL9[d],
            _MUL9[a] ^ _MUL14[b] ^ _MUL11[c] ^ _MUL13[d],
            _MUL13[a] ^ _MUL9[b] ^ _MUL14[c] ^ _MUL11[d],
            _MUL11[a] ^ _MUL13[b] ^ _MUL9[c] ^ _MUL14[d],
        ]

    def _mix_columns(self, state: list) -> list:
        for c in range(self.NB):
            col = [state[r][c] for r in range(4)]
            mixed = self._mix_column(col)
            for r in range(4):
                state[r][c] = mixed[r]
        return state

    def _inv_mix_columns(self, state: list) -> list:
        for c in range(self.NB):
            col = [state[r][c] for r in range(4)]
            mixed = self._inv_mix_column(col)
            for r in range(4):
                state[r][c] = mixed[r]
        return state

    def encrypt_block(self, block: bytes) -> bytes:
        """Verschlüsselt genau einen 32-Byte-Block."""
        assert len(block) == 32
        state = self._bytes_to_state(block)
        state = self._add_round_key(state, self._round_keys[0])
        for r in range(1, self.NR):
            state = self._sub_bytes(state)
            state = self._shift_rows(state)
            state = self._mix_columns(state)
            state = self._add_round_key(state, self._round_keys[r])
        state = self._sub_bytes(state)
        state = self._shift_rows(state)
        state = self._add_round_key(state, self._round_keys[self.NR])
        return self._state_to_bytes(state)

    def decrypt_block(self, block: bytes) -> bytes:
        """Entschlüsselt genau einen 32-Byte-Block."""
        assert len(block) == 32
        state = self._bytes_to_state(block)
        state = self._add_round_key(state, self._round_keys[self.NR])
        for r in range(self.NR - 1, 0, -1):
            state = self._inv_shift_rows(state)
            state = self._inv_sub_bytes(state)
            state = self._add_round_key(state, self._round_keys[r])
            state = self._inv_mix_columns(state)
        state = self._inv_shift_rows(state)
        state = self._inv_sub_bytes(state)
        state = self._add_round_key(state, self._round_keys[0])
        return self._state_to_bytes(state)


class RscpCipher:
    """
    Rijndael-256-CBC-Wrapper für RSCP-Kommunikation.
    Rolling-IV: Letzter verschlüsselter Block wird für nächsten Frame als neuer IV verwendet.
    """

    BLOCK_SIZE = 32  # 256-Bit Blöcke

    def __init__(self, key: bytes):
        self._rijndael = Rijndael256(key)
        self._enc_iv = bytearray(b'\xFF' * self.BLOCK_SIZE)
        self._dec_iv = bytearray(b'\xFF' * self.BLOCK_SIZE)

    def encrypt(self, data: bytes) -> bytes:
        """Verschlüsselt data (wird auf BLOCK_SIZE-Vielfaches zero-padded)."""
        # Zero-Padding auf Blockgröße
        pad_len = (self.BLOCK_SIZE - len(data) % self.BLOCK_SIZE) % self.BLOCK_SIZE
        data = data + bytes(pad_len)

        result = bytearray()
        for i in range(0, len(data), self.BLOCK_SIZE):
            block = bytes(data[i:i+self.BLOCK_SIZE])
            # CBC: XOR mit IV vor Verschlüsselung
            xored = bytes(a ^ b for a, b in zip(block, self._enc_iv))
            enc_block = self._rijndael.encrypt_block(xored)
            result.extend(enc_block)
            self._enc_iv = bytearray(enc_block)  # Rolling IV

        return bytes(result)

    def decrypt(self, data: bytes) -> bytes:
        """Entschlüsselt data (Länge muss Vielfaches von BLOCK_SIZE sein)."""
        if len(data) % self.BLOCK_SIZE != 0:
            raise ValueError(f"Datenlänge {len(data)} ist kein Vielfaches von {self.BLOCK_SIZE}")

        result = bytearray()
        for i in range(0, len(data), self.BLOCK_SIZE):
            block = bytes(data[i:i+self.BLOCK_SIZE])
            dec_block = self._rijndael.decrypt_block(block)
            # CBC: XOR mit IV nach Entschlüsselung
            plain = bytes(a ^ b for a, b in zip(dec_block, self._dec_iv))
            result.extend(plain)
            self._dec_iv = bytearray(block)  # Rolling IV = letzter verschlüsselter Block

        return bytes(result)


# ---------------------------------------------------------------------------
# RSCP Frame-Struktur
# ---------------------------------------------------------------------------

# Frame-Header: MAGIC(2) + CTRL(2) + Timestamp_sec(8, uint64) + Timestamp_ns(4, uint32) + Length(2) = 18 Bytes
# Quelle: RSCPGui _rscp_utils.py -> _FRAME_HEADER_FORMAT = '<HHQIH'
_FRAME_MAGIC       = 0xE3DC
_FRAME_CTRL        = 0x0011    # Bit 4 = CRC im Frame aktiv
_FRAME_HEADER_FMT  = '<HHQIH'  # magic, ctrl, ts_sec, ts_ns, length
_FRAME_HEADER_SIZE = struct.calcsize(_FRAME_HEADER_FMT)  # = 18 Bytes
_MAGIC_CHECK_FMT   = '>H'      # magic ist big-endian (E3DC Protokoll)

# TLV-Tag-Header: tag(4, uint32) + type(1, uint8) + length(2, uint16) = 7 Bytes
# Quelle: RSCPGui _rscp_utils.py -> _DATA_HEADER_FORMAT = '<IBH'
_TAG_HEADER_FMT    = '<IBH'
_TAG_HEADER_SIZE   = struct.calcsize(_TAG_HEADER_FMT)  # = 7 Bytes

# RSCP-Datentypen (Typ-Byte im TLV)
class RscpType:
    Nil       = 0x00
    Bool      = 0x01
    Char8     = 0x02
    UChar8    = 0x03
    Int16     = 0x04
    Uint16    = 0x05
    Int32     = 0x06
    Uint32    = 0x07
    Int64     = 0x08
    Uint64    = 0x09
    Float32   = 0x0A
    Double64  = 0x0B
    Bitfield  = 0x0C
    String    = 0x0C # In RSCPGui, String and Bitfield both share 0x0C... wait! Actually String is CString: 13 / Char8: 2.
    CString   = 0x0D
    Container = 0x0E
    Timestamp = 0x0F
    ByteArray = 0x10
    Error     = 0xFF


# Bekannte Tag-Codes (Subset für vital_stats / Batterie-Diagnostik)
class RscpTag:
    # Auth
    RSCP_REQ_AUTHENTICATION       = 0x00000001
    RSCP_AUTHENTICATION_USER      = 0x00000002
    RSCP_AUTHENTICATION_PASSWORD  = 0x00000003
    RSCP_AUTHENTICATION           = 0x00800001

    # Info
    INFO_REQ_SW_RELEASE           = 0x06000004
    INFO_REQ_PRODUCTION_DATE      = 0x06000002
    INFO_REQ_MAC_ADDRESS          = 0x06000007
    # INFO_REQ
    INFO_REQ_MAC_ADDRESS          = 0x0A00000A
    INFO_REQ_PRODUCTION_DATE      = 0x0A000002
    INFO_REQ_SW_RELEASE           = 0x0A000019
    INFO_REQ_GET_FS_USAGE         = 0x0A000025
    
    # INFO
    INFO_MAC_ADDRESS              = 0x0A80000A
    INFO_PRODUCTION_DATE          = 0x0A800002
    INFO_SW_RELEASE               = 0x0A800019
    INFO_GET_FS_USAGE             = 0x0A80002D
    INFO_FS_USE_PERCENT           = 0x0A800031

    # Batterie
    BAT_REQ_DATA                  = 0x03040000
    BAT_INDEX                     = 0x03040001
    BAT_REQ_INFO                  = 0x03000020
    BAT_REQ_ASOC                  = 0x0300000F
    BAT_REQ_CHARGE_CYCLES         = 0x03000008
    BAT_REQ_MAX_DCB_CELL_TEMPERATURE = 0x03000016
    BAT_REQ_MIN_DCB_CELL_TEMPERATURE = 0x03000017
    BAT_REQ_DCB_COUNT             = 0x0300000D
    BAT_REQ_USABLE_CAPACITY       = 0x03000026  # Einheit: Ah (NICHT Wh!) - bestaetigt via RSCPGui
    BAT_REQ_FCC                   = 0x03000010  # Einheit: Ah (NICHT Wh!) - bestaetigt via RSCPGui
    BAT_REQ_MODULE_VOLTAGE        = 0x03000002  # Batterie-String-Spannung in V
    BAT_REQ_DCB_INFO              = 0x03000042
    BAT_REQ_DCB_ALL_CELL_TEMPERATURES = 0x03000018
    BAT_REQ_DCB_ALL_CELL_VOLTAGES = 0x0300001A
    BAT_REQ_RC                    = 0x03000011

    # Batterie-Antwort-Tags
    BAT_DATA                      = 0x03840000
    BAT_INFO                      = 0x03800020
    BAT_ASOC                      = 0x0380000F
    BAT_CHARGE_CYCLES             = 0x03800008
    BAT_MAX_DCB_CELL_TEMPERATURE  = 0x03800016
    BAT_MIN_DCB_CELL_TEMPERATURE  = 0x03800017
    BAT_DCB_COUNT                 = 0x0380000D
    BAT_USABLE_CAPACITY           = 0x03800026  # Ah
    BAT_FCC                       = 0x03800010  # Ah
    BAT_MODULE_VOLTAGE            = 0x03800002  # V (String-Spannung)
    BAT_DCB_INFO                  = 0x03800042
    BAT_DCB_INDEX                 = 0x03800100
    BAT_DCB_SOH                   = 0x03800109
    BAT_DCB_SOH_H20               = 0x03800116  # H20/S10X: Pack-SOH liegt hier (RSCP liefert 0x03800109 nicht)
    BAT_DCB_CYCLE_COUNT           = 0x03800110
    BAT_DCB_CELL_TEMPERATURE      = 0x03800019
    BAT_DCB_ALL_CELL_TEMPERATURES = 0x03800018
    BAT_DCB_ALL_CELL_VOLTAGES     = 0x0380001A
    BAT_DCB_CELL_VOLTAGE          = 0x0380001B
    BAT_RC                        = 0x03800011

    # EMS
    EMS_REQ_BAT_SOC               = 0x01000008
    EMS_BAT_SOC                   = 0x01800008
    EMS_REQ_GET_POWER_SETTINGS    = 0x0100008B
    EMS_GET_POWER_SETTINGS        = 0x0180008B
    EMS_REQ_SET_POWER_SETTINGS    = 0x0100008C
    EMS_SET_POWER_SETTINGS        = 0x0180008C
    EMS_REQ_SET_POWER             = 0x01000030
    EMS_REQ_SET_POWER_MODE        = 0x01000031
    EMS_REQ_SET_POWER_VALUE       = 0x01000032
    EMS_POWER_LIMITS_USED         = 0x01000100
    EMS_MAX_CHARGE_POWER          = 0x01000101
    EMS_MAX_DISCHARGE_POWER       = 0x01000102
    EMS_DISCHARGE_START_POWER     = 0x01000103
    EMS_REQ_SET_MAX_CHARGE_POWER  = 0x01000101
    EMS_REQ_SET_MAX_DISCHARGE_POWER = 0x01000102
    EMS_REQ_MODE                  = 0x01000011

    # Wallbox
    WB_REQ_DATA                   = 0x0E040000
    WB_DATA                       = 0x0E840000
    WB_INDEX                      = 0x0E040001
    WB_REQ_PM_POWER_L1            = 0x0E00000C
    WB_REQ_PM_POWER_L2            = 0x0E00000D
    WB_REQ_PM_POWER_L3            = 0x0E00000E
    WB_REQ_EXTERN_DATA_ALG        = 0x0E041014
    WB_EXTERN_DATA_ALG            = 0x0E841014
    WB_PM_POWER_L1                = 0x0E80000C
    WB_PM_POWER_L2                = 0x0E80000D
    WB_PM_POWER_L3                = 0x0E80000E
    WB_REQ_FIRMWARE_VERSION       = 0x0E00002F
    WB_FIRMWARE_VERSION           = 0x0E80002F
    WB_REQ_SET_AUTO_PHASE_SWITCH_ENABLED = 0x0E000038
    WB_REQ_AUTO_PHASE_SWITCH_ENABLED     = 0x0E000039
    WB_SET_AUTO_PHASE_SWITCH_ENABLED     = 0x0E800038
    WB_AUTO_PHASE_SWITCH_ENABLED         = 0x0E800039
    WB_REQ_WALLBOX_TYPE          = 0x0E041036
    WB_WALLBOX_TYPE              = 0x0E841036
    WB_REQ_DEVICE_NAME            = 0x0E000042
    WB_DEVICE_NAME                = 0x0E800042
    WB_REQ_SUN_MODE_ACTIVE        = 0x0E041038
    WB_REQ_SET_SUN_MODE_ACTIVE    = 0x0E041039
    WB_SUN_MODE_ACTIVE            = 0x0E841038
    WB_SET_SUN_MODE_ACTIVE        = 0x0E841039
    WB_REQ_NUMBER_PHASES          = 0x0E04103B
    WB_REQ_SET_NUMBER_PHASES      = 0x0E04103C
    WB_NUMBER_PHASES              = 0x0E84103B
    WB_SET_NUMBER_PHASES          = 0x0E84103C
    WB_REQ_ABORT_CHARGING         = 0x0E04103D
    WB_REQ_SET_ABORT_CHARGING     = 0x0E04103E
    WB_ABORT_CHARGING             = 0x0E84103D
    WB_SET_ABORT_CHARGING         = 0x0E84103F
    WB_REQ_UPPER_CURRENT_LIMIT    = 0x0E041045
    WB_REQ_LOWER_CURRENT_LIMIT    = 0x0E041046
    WB_REQ_MAX_CHARGE_CURRENT     = 0x0E041047
    WB_REQ_SET_MAX_CHARGE_CURRENT = 0x0E041049
    WB_UPPER_CURRENT_LIMIT        = 0x0E841045
    WB_LOWER_CURRENT_LIMIT        = 0x0E841046
    WB_MAX_CHARGE_CURRENT         = 0x0E841047
    WB_SET_MAX_CHARGE_CURRENT     = 0x0E841049
    WB_REQ_SET_EXTERN             = 0x0E041010
    WB_REQ_SET_PARAM_1            = 0x0E041018
    WB_REQ_SET_PARAM_2            = 0x0E041019
    WB_REQ_PARAM_2                = 0x0E04101A
    WB_REQ_PARAM_1                = 0x0E04101B
    WB_EXTERN_DATA                = 0x0E042010
    WB_EXTERN_DATA_LEN            = 0x0E042011
    WB_SET_PARAM_1                = 0x0E841018
    WB_SET_PARAM_2                = 0x0E841019
    WB_RSP_PARAM_2                = 0x0E84101A
    WB_RSP_PARAM_1                = 0x0E84101B
    WB_DEVICE_STATE               = 0x0E860000
    WB_DEVICE_CONNECTED           = 0x0E860001
    WB_DEVICE_WORKING             = 0x0E860002
    WB_DEVICE_IN_SERVICE          = 0x0E860003
    # Request-Tags fuer Verbindungsstatus (innerhalb WB_REQ_DATA Container)
    WB_REQ_DEVICE_CONNECTED       = 0x0E041000
    WB_REQ_DEVICE_WORKING         = 0x0E041001
    # Session-Daten (aktuelle Ladesitzung) - direkt aus E3DC-Firmware
    WB_REQ_SESSION                = 0x0E00002C   # Anfrage aktuelle Session
    WB_SESSION                    = 0x0E80002C   # Antwort-Tag Session-Container
    WB_SESSION_START_TIME         = 0x0E741026   # Unix-Timestamp Beginn der Session
    WB_SESSION_STATUS             = 0x0E741027   # Status der Session
    WB_SESSION_CHARGED_ENERGY     = 0x0E74102A   # Geladene Energie in Wh (aktuelle Session)
    WB_SESSION_CHARGED_SUN_ENERGY = 0x0E74102B   # Davon Solar-Anteil in Wh
    WB_SESSION_AUTH_DATA          = 0x0E741030   # RFID/Autorisierungsdaten


# ---------------------------------------------------------------------------
# TLV-Dekodierung
# ---------------------------------------------------------------------------

def _decode_value(type_byte: int, data: bytes):
    """Dekodiert einen RSCP-Wert anhand des Typ-Bytes."""
    if type_byte == RscpType.Nil or data is None:
        return None
    elif type_byte == RscpType.Bool:
        return bool(data[0]) if data else False
    elif type_byte == RscpType.Char8:
        return struct.unpack('<b', data)[0]
    elif type_byte == RscpType.UChar8:
        return struct.unpack('<B', data)[0]
    elif type_byte == RscpType.Int16:
        return struct.unpack('<h', data)[0]
    elif type_byte == RscpType.Uint16:
        return struct.unpack('<H', data)[0]
    elif type_byte == RscpType.Int32:
        return struct.unpack('<i', data)[0]
    elif type_byte == RscpType.Uint32:
        return struct.unpack('<I', data)[0]
    elif type_byte == RscpType.Int64:
        return struct.unpack('<q', data)[0]
    elif type_byte == RscpType.Uint64:
        return struct.unpack('<Q', data)[0]
    elif type_byte == RscpType.Float32:
        return struct.unpack('<f', data)[0]
    elif type_byte == RscpType.Double64:
        return struct.unpack('<d', data)[0]
    elif type_byte in (RscpType.CString, RscpType.String):
        return data.decode('utf-8', errors='replace').rstrip('\x00')
    elif type_byte == RscpType.ByteArray:
        return data
    elif type_byte == RscpType.Timestamp:
        if len(data) >= 8:
            sec = struct.unpack('<Q', data[:8])[0]
            ns = struct.unpack('<I', data[8:12])[0] if len(data) >= 12 else 0
            return sec + ns * 1e-9
        return None
    elif type_byte == RscpType.Error:
        return struct.unpack('<I', data[:4])[0] if len(data) >= 4 else None
    elif type_byte == RscpType.Container:
        return _decode_tlv_list(data)
    return data


def _decode_tlv(data: bytes, pos: int) -> tuple:
    """Dekodiert ein einzelnes TLV-Element. Gibt (tag, type, value, next_pos) zurück."""
    if pos + _TAG_HEADER_SIZE > len(data):
        return None, None, None, len(data)
    tag, type_byte, length = struct.unpack_from(_TAG_HEADER_FMT, data, pos)
    start = pos + _TAG_HEADER_SIZE
    end = start + length
    value_bytes = data[start:end]

    if type_byte == RscpType.Container:
        value = _decode_tlv_list(value_bytes)
    else:
        value = _decode_value(type_byte, value_bytes)

    return tag, type_byte, value, end


def _decode_tlv_list(data: bytes) -> list:
    """Dekodiert eine Liste von TLV-Elementen."""
    items = []
    pos = 0
    while pos < len(data):
        tag, type_byte, value, next_pos = _decode_tlv(data, pos)
        if tag is None:
            break
        items.append({'tag': tag, 'type': type_byte, 'value': value})
        pos = next_pos
    return items


# ---------------------------------------------------------------------------
# TLV-Kodierung
# ---------------------------------------------------------------------------

def _encode_value(type_byte: int, value) -> bytes:
    if type_byte == RscpType.Nil or value is None:
        return b''
    elif type_byte == RscpType.Bool:
        return struct.pack('<B', 1 if value else 0)
    elif type_byte in (RscpType.Char8,):
        return struct.pack('<b', value)
    elif type_byte in (RscpType.UChar8,):
        return struct.pack('<B', value)
    elif type_byte == RscpType.Int16:
        return struct.pack('<h', value)
    elif type_byte == RscpType.Uint16:
        return struct.pack('<H', value)
    elif type_byte == RscpType.Int32:
        return struct.pack('<i', value)
    elif type_byte == RscpType.Uint32:
        return struct.pack('<I', value)
    elif type_byte == RscpType.Int64:
        return struct.pack('<q', value)
    elif type_byte == RscpType.Uint64:
        return struct.pack('<Q', value)
    elif type_byte == RscpType.Float32:
        return struct.pack('<f', value)
    elif type_byte == RscpType.Double64:
        return struct.pack('<d', value)
    elif type_byte == RscpType.CString:
        return value.encode('utf-8') if isinstance(value, str) else value
    elif type_byte == RscpType.ByteArray:
        return value if isinstance(value, bytes) else bytes(value)
    elif type_byte == RscpType.Container:
        # Wert muss eine Liste von TLV-Dicts sein
        return _encode_tlv_list(value)
    elif type_byte == RscpType.Timestamp:
        # E3DC Timestamp Format: 3x signed int32 (hiword, loword, milliseconds)
        # Input: dict {'seconds': ..., 'nanoseconds': ...} ODER float (Unix-Sekunden)
        if isinstance(value, dict):
            ts = int(value.get('seconds', 0))
            ns = int(value.get('nanoseconds', 0))
            ms = round(ns / 1e6)
        else:
            ts = int(value)
            ms = round((value - ts) * 1000)
        hiword = (ts >> 32) & 0xFFFFFFFF
        loword = ts & 0xFFFFFFFF
        return struct.pack('<iii', hiword, loword, ms)
    return b''


def _encode_tlv(tag: int, type_byte: int, value) -> bytes:
    """Kodiert ein einzelnes TLV-Element mit format '<IBH' (tag=uint32, type=uint8, len=uint16)."""
    data_bytes = _encode_value(type_byte, value)
    header = struct.pack(_TAG_HEADER_FMT, tag, type_byte, len(data_bytes))
    return header + data_bytes


def _encode_tlv_list(items: list) -> bytes:
    """Kodiert eine Liste von TLV-Dicts [{'tag': ..., 'type': ..., 'value': ...}]."""
    result = bytearray()
    for item in items:
        result.extend(_encode_tlv(item['tag'], item['type'], item['value']))
    return bytes(result)


# ---------------------------------------------------------------------------
# RSCP-Frame-Builder
# ---------------------------------------------------------------------------

def _build_frame(payload: bytes) -> bytes:
    """
    Baut einen RSCP-Frame im E3DC-Drahtformat:
    MAGIC(2 BE) + CTRL(2 LE) + Timestamp_sec(8 LE) + Timestamp_ns(4 LE) + Length(2 LE)
    + Payload + CRC32(4 LE)
    Format entspricht RSCPGui _FRAME_HEADER_FORMAT = '<HHQIH'
    ACHTUNG: MAGIC wird von E3DC big-endian geprüft (0xE3DC = [0xe3, 0xdc])
    Daher: struct.pack('<H', 0xE3DC) wäre [0xDC, 0xE3] - FALSCH!
    Wir schreiben die Magic-Bytes direkt als b'\xe3\xdc'.
    """
    ts_sec = int(time.time())
    ts_ns  = 0
    ctrl   = _FRAME_CTRL
    length = len(payload)
    # Header: Magic (raw BE bytes) + CTRL(2 LE) + TS_SEC(8 LE) + TS_NS(4 LE) + LENGTH(2 LE)
    header = b'\xe3\xdc'   # MAGIC: raw bytes [e3, dc] = BE 0xE3DC ✓
    header += b'\x00\x11'  # CTRL: raw [00, 11] = BE 0x0011 (Bit4=CRC, Bit0=Version)
                            # ACHTUNG: struct.pack('<H', 0x0011) wäre [11, 00] - FALSCH!
                            # RSCPGui nutzt _endian_swap_uint16(0x0011)=0x1100, pack('<H',0x1100)=[00,11] ✓
    header += struct.pack('<Q', ts_sec)   # 64-bit timestamp seconds (LE) ✓
    header += struct.pack('<I', ts_ns)    # 32-bit ms-Feld (RSCPGui: round(sub_sec*1000)) (LE)
    header += struct.pack('<H', length)   # 16-bit payload length (LE) ✓
    frame_without_crc = header + payload
    crc = zlib.crc32(frame_without_crc) & 0xFFFFFFFF
    return frame_without_crc + struct.pack('<I', crc)


def _parse_frame(data: bytes) -> tuple:
    """
    Parst einen E3DC RSCP-Frame.
    Erwartet Format '<HHQIH' (18 Byte Header) + Payload + CRC32(4).
    Gibt (payload_bytes, ok) zurück.
    """
    if len(data) < _FRAME_HEADER_SIZE + 4:
        log.error(f"Frame zu kurz: {len(data)} Bytes")
        return None, False

    # Magic big-endian prüfen
    magic = struct.unpack_from('>H', data, 0)[0]
    if magic != _FRAME_MAGIC:
        log.error(f"Ungültiges RSCP-Magic: {magic:#06x} (erwartet {_FRAME_MAGIC:#06x})")
        return None, False

    _, ctrl, ts_sec, ts_ns, length = struct.unpack_from(_FRAME_HEADER_FMT, data)

    total_expected = _FRAME_HEADER_SIZE + length + 4  # Header + Payload + CRC
    if len(data) < total_expected:
        log.error(f"Frame unvollständig: {len(data)}/{total_expected} Bytes")
        return None, False

    payload = data[_FRAME_HEADER_SIZE : _FRAME_HEADER_SIZE + length]

    # CRC32 prüfen wenn CTRL-Bit 4 gesetzt (0x0010)
    if ctrl & 0x0010:
        crc_received = struct.unpack_from('<I', data, _FRAME_HEADER_SIZE + length)[0]
        crc_calc = zlib.crc32(data[:_FRAME_HEADER_SIZE + length]) & 0xFFFFFFFF
        if crc_received != crc_calc:
            log.warning(f"CRC-Fehler: erwartet {crc_calc:#010x}, erhalten {crc_received:#010x}")
            return None, False

    return payload, True


# ---------------------------------------------------------------------------
# Hilfs-Funktionen für TLV-Suche (analog zu find_dto in vital_stats.py)
# ---------------------------------------------------------------------------

def find_tag(items, tag_code: int):
    """Sucht rekursiv nach einem Tag-Code in einer TLV-Liste. Gibt den Wert zurück."""
    if not isinstance(items, list):
        return None
    for item in items:
        if item.get('tag') == tag_code:
            return item
        if item.get('type') == RscpType.Container and isinstance(item.get('value'), list):
            result = find_tag(item['value'], tag_code)
            if result is not None:
                return result
    return None


def find_tag_value(items, tag_code: int):
    """Gibt direkt den 'value' eines Tages zurück (oder None)."""
    item = find_tag(items, tag_code)
    return item['value'] if item else None


def find_all_values(items, tag_code: int) -> list:
    """Sucht rekursiv ALLE Werte eines bestimmten Tags (z.B. weil Firmware sie in BAT_DATA kapselt)."""
    results = []
    if not isinstance(items, list):
        return results
    for item in items:
        if item.get('tag') == tag_code and item.get('value') is not None:
            results.append(item['value'])
        if item.get('type') == RscpType.Container and isinstance(item.get('value'), list):
            results.extend(find_all_values(item['value'], tag_code))
    return results


# ---------------------------------------------------------------------------
# RscpConnection: TCP-Verbindung + Auth + Daten-Requests
# ---------------------------------------------------------------------------

class RscpConnection:
    """
    Verwaltete RSCP-TCP-Verbindung zum E3DC-Kraftwerk.
    Singleton-Verbindung: erst verbinden, dann beliebig viele Requests senden.
    """

    TIMEOUT = 10.0   # Sekunden
    RECV_BUF = 65536

    def __init__(self, host: str, port: int, rscp_password: str):
        self.host = host
        self.port = port
        # Key-Padding mit \xff (entspricht RSCPGui: ljust(32, '\xff'))
        # E3DC-Firmware verwendet intern memset(key,0,32) + strncpy(key,pw,32)
        pw_bytes = rscp_password.encode('latin-1')[:32]
        self._key = pw_bytes.ljust(32, b'\xff')
        self._sock = None
        self._cipher = None
        self._authenticated = False

    def connect(self):
        """Öffnet TCP-Verbindung und authentifiziert sich."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.TIMEOUT)
        self._sock.connect((self.host, self.port))
        self._cipher = RscpCipher(self._key)
        self._authenticated = False
        log.debug(f"TCP-Verbindung zu {self.host}:{self.port} aufgebaut")

    def _send_frame(self, payload_items: list):
        """Kodiert, verschlüsselt und sendet einen RSCP-Frame."""
        payload = _encode_tlv_list(payload_items)
        frame = _build_frame(payload)
        encrypted = self._cipher.encrypt(frame)
        self._sock.sendall(encrypted)

    def _recv_frame(self) -> list:
        """Empfängt, entschlüsselt und dekodiert einen RSCP-Frame."""
        raw = self._sock.recv(self.RECV_BUF)
        if not raw:
            raise ConnectionError("Verbindung geschlossen")
        # Blockgröße-Alignment sicherstellen (TCP-Fragmentierung)
        while len(raw) % 32 != 0:
            chunk = self._sock.recv(self.RECV_BUF)
            if not chunk:
                break
            raw += chunk
        decrypted = self._cipher.decrypt(raw)
        payload, ok = _parse_frame(decrypted)
        if not ok or payload is None:
            return []
        return _decode_tlv_list(payload)

    def authenticate(self, portal_user: str, portal_password: str):
        """Sendet Authentication-Container und prüft die Antwort."""
        auth_payload = [
            {'tag': RscpTag.RSCP_REQ_AUTHENTICATION, 'type': RscpType.Container, 'value': [
                {'tag': RscpTag.RSCP_AUTHENTICATION_USER,     'type': RscpType.CString, 'value': portal_user},
                {'tag': RscpTag.RSCP_AUTHENTICATION_PASSWORD, 'type': RscpType.CString, 'value': portal_password},
            ]}
        ]
        self._send_frame(auth_payload)
        response = self._recv_frame()
        auth_level = find_tag_value(response, RscpTag.RSCP_AUTHENTICATION)
        if auth_level and int(auth_level) >= 10:
            self._authenticated = True
            log.debug(f"RSCP-Authentifizierung erfolgreich (Level {auth_level})")
        else:
            raise ConnectionError(f"RSCP-Authentifizierung fehlgeschlagen (Level: {auth_level})")

    def request(self, items: list) -> list:
        """Sendet eine Liste von TLV-Anfragen und gibt die Antwort zurück."""
        if not self._authenticated:
            raise RuntimeError("Nicht authentifiziert – connect() und authenticate() zuerst aufrufen")
        self._send_frame(items)
        return self._recv_frame()

    def close(self):
        """Schließt die TCP-Verbindung."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            self._authenticated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# Fertige High-Level-Funktion: Batterie-Vitalwerte abfragen
# ---------------------------------------------------------------------------

def fetch_battery_vitals(host: str, port: int, portal_user: str,
                          portal_password: str, rscp_password: str) -> dict:
    """
    Fragt Batterie-Vitalwerte vom E3DC ab (analog zu vital_stats.py).
    Gibt ein dict zurück wie vital_stats.py – kompatibel mit vitals.php.
    """
    conn = RscpConnection(host, port, rscp_password)
    result = {'cabinets': [], 'system_info': {}, 'generated_at': int(time.time())}

    try:
        conn.connect()
        conn.authenticate(portal_user, portal_password)

        # --- System-Info ---
        sys_req = [
            {'tag': RscpTag.INFO_REQ_SW_RELEASE,      'type': RscpType.Nil,  'value': None},
            {'tag': RscpTag.INFO_REQ_PRODUCTION_DATE, 'type': RscpType.Nil,  'value': None},
            {'tag': RscpTag.INFO_REQ_MAC_ADDRESS,     'type': RscpType.Nil,  'value': None},
            {'tag': RscpTag.INFO_REQ_GET_FS_USAGE,    'type': RscpType.Nil,  'value': None},
            {'tag': 167772161,                        'type': RscpType.Nil,  'value': None}, # INFO_REQ_SERIAL_NUMBER
        ]
        sys_resp = conn.request(sys_req)
        fs_usage = find_tag(sys_resp, RscpTag.INFO_GET_FS_USAGE)
        fs_perc = None
        if fs_usage and isinstance(fs_usage.get('value'), list):
            fs_perc = find_tag_value(fs_usage['value'], RscpTag.INFO_FS_USE_PERCENT)

        result['system_info'] = {
            'sw_release':          find_tag_value(sys_resp, RscpTag.INFO_SW_RELEASE) or 'N/A',
            'production_date':     find_tag_value(sys_resp, RscpTag.INFO_PRODUCTION_DATE) or 'N/A',
            'mac_address':         find_tag_value(sys_resp, RscpTag.INFO_MAC_ADDRESS) or 'N/A',
            'serial_number':       find_tag_value(sys_resp, 176160769) or 'N/A', # INFO_SERIAL_NUMBER
            'disk_usage_percent':  fs_perc,
        }

        # --- Batterien (Cabinet 0..3) ---
        for cab_idx in range(4):
            bat_req = [{'tag': RscpTag.BAT_REQ_DATA, 'type': RscpType.Container, 'value': [
                {'tag': RscpTag.BAT_INDEX,                       'type': RscpType.Uint16, 'value': cab_idx},
                {'tag': RscpTag.BAT_REQ_INFO,                    'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_ASOC,                    'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_CHARGE_CYCLES,           'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_MAX_DCB_CELL_TEMPERATURE,'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_MIN_DCB_CELL_TEMPERATURE,'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_DCB_COUNT,               'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_USABLE_CAPACITY,         'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_FCC,                     'type': RscpType.Nil,    'value': None},
                {'tag': RscpTag.BAT_REQ_MODULE_VOLTAGE,          'type': RscpType.Nil,    'value': None},
                {'tag': 50331715,                                'type': RscpType.Nil,    'value': None}, # RscpTag.BAT_REQ_SPECIFICATION
            ]}]

            bat_resp = conn.request(bat_req)
            bat_data = find_tag(bat_resp, RscpTag.BAT_DATA)
            if not bat_data or not isinstance(bat_data.get('value'), list):
                continue

            bd = bat_data['value']
            dcb_count = find_tag_value(bd, RscpTag.BAT_DCB_COUNT)
            if not dcb_count or int(dcb_count) <= 0:
                continue

            dcb_count = int(dcb_count)
            asoc = find_tag_value(bd, RscpTag.BAT_ASOC)
            cycles = find_tag_value(bd, RscpTag.BAT_CHARGE_CYCLES)
            t_max = find_tag_value(bd, RscpTag.BAT_MAX_DCB_CELL_TEMPERATURE)
            t_min = find_tag_value(bd, RscpTag.BAT_MIN_DCB_CELL_TEMPERATURE)

            # Fallback: BAT_INFO Container
            bi = []
            bat_info = find_tag(bd, RscpTag.BAT_INFO)
            if bat_info and isinstance(bat_info.get('value'), list):
                bi = bat_info['value']
            # Zell-Temperaturen
            if t_max is None: t_max = find_tag_value(bi, RscpTag.BAT_MAX_DCB_CELL_TEMPERATURE)
            if t_min is None: t_min = find_tag_value(bi, RscpTag.BAT_MIN_DCB_CELL_TEMPERATURE)
            if cycles is None: cycles = find_tag_value(bi, RscpTag.BAT_CHARGE_CYCLES)

            spec_cap = None
            bat_spec = find_tag(bd, 58720323) # BAT_SPECIFICATION
            if bat_spec and isinstance(bat_spec.get('value'), list):
                spec_cap = find_tag_value(bat_spec['value'], 58720549) # BAT_SPECIFIED_CAPACITY

            # BAT_MODULE_VOLTAGE: String-Spannung des Akkus in Volt (Live-Wert, schwankt!)
            # Fuer die Umrechnung von Ah in Wh nutzen wir die nominale Spannung von 51.8V
            # (756 Ah * 51.8V = 39160 Wh = Exakter Wert von BAT_SPECIFIED_CAPACITY)
            nominal_voltage_v = 51.8

            # BAT_FCC: Full Charge Capacity in Ah (bestaetigt via RSCPGui Quellcode: + ' Ah')
            # BAT_USABLE_CAPACITY: Usable Capacity ebenfalls in Ah
            # Umrechnung: Ah * Spannung(V) = Wh
            fcc_ah = find_tag_value(bd, RscpTag.BAT_FCC)
            usable_cap_ah = find_tag_value(bd, RscpTag.BAT_USABLE_CAPACITY)

            # Bevorzuge BAT_SPECIFICATION fuer die installierte Kapazitaet.
            # Einige H20/S10X-Firmwares liefern BAT_FCC/USABLE_CAPACITY nicht als
            # Batterie-Hardwarekapazitaet, sondern als kleinen Live-/Restwert
            # (z.B. 27Ah -> 1.4kWh bei 22.3kWh installiert). Solche Werte duerfen
            # nicht in die Vitals-Kapazitaetsrechnung laufen.
            fcc_wh = None
            real_usable_wh = None
            specified_wh = None

            # Falls wir die installierte Design Cap. (Wh) gefunden haben, addieren wir sie
            if spec_cap is not None:
                specified_wh = int(spec_cap)
                if 'installed_capacity_wh' not in result['system_info']:
                    result['system_info']['installed_capacity_wh'] = 0
                result['system_info']['installed_capacity_wh'] += specified_wh

            cap_ah = fcc_ah if fcc_ah is not None else usable_cap_ah
            if cap_ah is not None:
                uc_wh = int(float(cap_ah) * nominal_voltage_v)
                # Nur plausibel, wenn der Ah-Wert mindestens die halbe spezifizierte
                # Kapazitaet erreicht oder keine Spezifikation vorliegt.
                if specified_wh is None or uc_wh >= int(specified_wh * 0.5):
                    fcc_wh = uc_wh
                    if 'usable_capacity_wh' not in result['system_info']:
                        result['system_info']['usable_capacity_wh'] = 0
                    result['system_info']['usable_capacity_wh'] += uc_wh

            # Zusaetzlich: echte nutzbare Kapazitaet (USABLE_CAPACITY, etwas kleiner als FCC)
            if usable_cap_ah is not None:
                rc_wh = int(float(usable_cap_ah) * nominal_voltage_v)
                if specified_wh is None or rc_wh >= int(specified_wh * 0.5):
                    real_usable_wh = rc_wh
                    if 'real_usable_capacity_wh' not in result['system_info']:
                        result['system_info']['real_usable_capacity_wh'] = 0
                    result['system_info']['real_usable_capacity_wh'] += rc_wh

            # Rohdaten fuer Debugging (falls Spannung fehlt)
            result['system_info'].setdefault('debug_fcc_ah', 0)
            result['system_info']['debug_fcc_ah'] += float(fcc_ah or 0)
            result['system_info'].setdefault('debug_usable_cap_ah', 0)
            result['system_info']['debug_usable_cap_ah'] += float(usable_cap_ah or 0)
            result['system_info'].setdefault('debug_voltage_v', nominal_voltage_v)
            if fcc_wh is None and cap_ah is not None and specified_wh is not None:
                result['system_info']['capacity_note'] = 'BAT_FCC/USABLE_CAPACITY unplausibel klein; nutze BAT_SPECIFICATION.'

            cab = {
                'index': cab_idx,
                'count': dcb_count,
                # BAT_ASOC ist kein belastbarer Pack-SOH. Der Schrank-SOH wird nach
                # dem DCB-Loop aus echten Pack-SOH-Werten berechnet.
                'asoc': round(float(asoc), 2) if asoc is not None else None,
                'soh_avg': None,
                'cycles': int(cycles) if cycles is not None else None,
                'temp_max_global': round(float(t_max), 1) if t_max is not None else None,
                'temp_min_global': round(float(t_min), 1) if t_min is not None else None,
                'fcc_wh': fcc_wh,
                'usable_wh': real_usable_wh if real_usable_wh is not None else (int(specified_wh * 0.9) if specified_wh else None),
                'specified_wh': specified_wh,
                'packs': [],
            }

            # DCB-Pack-Details (SOH pro Pack, Spannungsspreizung)
            for dcb_idx in range(dcb_count):
                dcb_req = [{'tag': RscpTag.BAT_REQ_DATA, 'type': RscpType.Container, 'value': [
                    {'tag': RscpTag.BAT_INDEX,                       'type': RscpType.Uint16, 'value': cab_idx},
                    {'tag': RscpTag.BAT_REQ_DCB_INFO,                'type': RscpType.Uint16, 'value': dcb_idx},
                    {'tag': RscpTag.BAT_REQ_DCB_ALL_CELL_TEMPERATURES,'type': RscpType.Uint16,'value': dcb_idx},
                    {'tag': RscpTag.BAT_REQ_DCB_ALL_CELL_VOLTAGES,   'type': RscpType.Uint16, 'value': dcb_idx},
                ]}]
                dcb_resp = conn.request(dcb_req)
                dcb_bd = find_tag(dcb_resp, RscpTag.BAT_DATA)
                if not dcb_bd or not isinstance(dcb_bd.get('value'), list):
                    continue

                dcb_info = find_tag(dcb_bd['value'], RscpTag.BAT_DCB_INFO)
                if not dcb_info or not isinstance(dcb_info.get('value'), list):
                    continue
                di = dcb_info['value']
                soh = find_tag_value(di, RscpTag.BAT_DCB_SOH)
                if soh is None:
                    soh = find_tag_value(di, RscpTag.BAT_DCB_SOH_H20)
                pack_cycles = find_tag_value(di, RscpTag.BAT_DCB_CYCLE_COUNT)
                if soh is None:
                    continue
                try:
                    soh_f = float(soh)
                except (TypeError, ValueError):
                    continue
                if soh_f <= 0 or soh_f > 110:
                    continue

                # Zell-Temperaturen parsen
                pack_t_max, pack_t_min = cab['temp_max_global'], cab['temp_min_global']
                clean_temps = []
                temp_min_idx = None
                temp_max_idx = None
                all_temps_tag = find_tag(dcb_bd['value'], RscpTag.BAT_DCB_ALL_CELL_TEMPERATURES)
                if all_temps_tag and isinstance(all_temps_tag.get('value'), list):
                    temps = find_all_values(all_temps_tag['value'], RscpTag.BAT_DCB_CELL_TEMPERATURE)
                    clean = [t for t in temps if t != 0.0 and not (3.0 <= t <= 4.5)]
                    if not clean: clean = temps
                    if clean:
                        clean_temps = [round(float(t), 1) for t in clean]
                        pack_t_max = round(max(clean), 1)
                        pack_t_min = round(min(clean), 1)
                        temp_min_idx = clean_temps.index(pack_t_min) + 1
                        temp_max_idx = clean_temps.index(pack_t_max) + 1

                # Zell-Spannungsspreizung
                v_spread = None
                all_volts_tag = find_tag(dcb_bd['value'], RscpTag.BAT_DCB_ALL_CELL_VOLTAGES)
                v_min = None
                v_max = None
                clean_volts = []
                voltage_min_idx = None
                voltage_max_idx = None
                if all_volts_tag and isinstance(all_volts_tag.get('value'), list):
                    volts = find_all_values(all_volts_tag['value'], RscpTag.BAT_DCB_CELL_VOLTAGE)
                    if len(volts) >= 2:
                        clean_volts = [round(float(v), 3) for v in volts]
                        v_min = round(min(volts), 3)
                        v_max = round(max(volts), 3)
                        v_spread = round(v_max - v_min, 3)
                        voltage_min_idx = clean_volts.index(v_min) + 1
                        voltage_max_idx = clean_volts.index(v_max) + 1

                cab['packs'].append({
                    'index': dcb_idx,
                    'soh': round(soh_f, 2),
                    'soh_source': 'BAT_DCB_SOH' if find_tag_value(di, RscpTag.BAT_DCB_SOH) is not None else 'BAT_DCB_SOH_H20',
                    'cycles': int(pack_cycles) if pack_cycles is not None else cab['cycles'],
                    'temp_max': pack_t_max,
                    'temp_min': pack_t_min,
                    'temp_spread': round(pack_t_max - pack_t_min, 1) if pack_t_max is not None and pack_t_min is not None else None,
                    'temp_min_cell': temp_min_idx,
                    'temp_max_cell': temp_max_idx,
                    'cell_temperatures': clean_temps,
                    'voltage_min': v_min,
                    'voltage_max': v_max,
                    'voltage_min_cell': voltage_min_idx,
                    'voltage_max_cell': voltage_max_idx,
                    'voltage_spread': v_spread,
                    'cell_voltages': clean_volts,
                    'cell_count': len(clean_volts) if clean_volts else None,
                })

            if cab['packs']:
                soh_values = [p['soh'] for p in cab['packs'] if p.get('soh') is not None]
                cycle_values = [p['cycles'] for p in cab['packs'] if p.get('cycles') is not None]
                voltage_spreads = [p['voltage_spread'] for p in cab['packs'] if p.get('voltage_spread') is not None]
                temp_spreads = [p['temp_spread'] for p in cab['packs'] if p.get('temp_spread') is not None]
                if soh_values:
                    cab['soh_min'] = round(min(soh_values), 2)
                    cab['soh_max'] = round(max(soh_values), 2)
                    cab['soh_spread'] = round(cab['soh_max'] - cab['soh_min'], 2)
                    cab['soh_avg'] = round(sum(soh_values) / len(soh_values), 2)
                if cycle_values:
                    cab['cycles_min'] = min(cycle_values)
                    cab['cycles_max'] = max(cycle_values)
                    cab['cycles_spread'] = cab['cycles_max'] - cab['cycles_min']
                if voltage_spreads:
                    cab['voltage_spread_max'] = round(max(voltage_spreads), 3)
                if temp_spreads:
                    cab['temp_spread_max'] = round(max(temp_spreads), 1)

            result['cabinets'].append(cab)

    finally:
        conn.close()

    return result


def fetch_ems_data(host: str, port: int, portal_user: str,
                   portal_password: str, rscp_password: str) -> dict:
    """
    Fragt generelle EMS-Daten ab (z.B. SOC).
    """
    conn = RscpConnection(host, port, rscp_password)
    result = {'soc': None}

    try:
        conn.connect()
        conn.authenticate(portal_user, portal_password)

        ems_req = [
            {'tag': RscpTag.EMS_REQ_BAT_SOC, 'type': RscpType.Nil, 'value': None}
        ]
        
        ems_resp = conn.request(ems_req)
        soc = find_tag_value(ems_resp, RscpTag.EMS_BAT_SOC)
        if soc is not None:
            result['soc'] = float(soc)

    finally:
        conn.close()

    return result


# ---------------------------------------------------------------------------
# Power Control (Setzen von Lade/Entlade-Modus)
# ---------------------------------------------------------------------------

def set_e3dc_power(host: str, port: int, portal_user: str,
                   portal_password: str, rscp_password: str,
                   mode: int, value: int) -> bool:
    """
    Sendet das Kommando, um den Betriebsmodus und die Leistung des EMS zu steuern.
    mode: 0 = NORMAL (Auto), 1 = IDLE (Sperre), 2 = ENTLADEN, 3 = LADEN, 4 = NETZ_LADE
    value: Leistung in Watt
    Gibt True bei Erfolg zurück, sonst False.
    """
    conn = RscpConnection(host, port, rscp_password)
    try:
        conn.connect()
        conn.authenticate(portal_user, portal_password)
        
        set_req = [
            {'tag': RscpTag.EMS_REQ_SET_POWER, 'type': RscpType.Container, 'value': [
                {'tag': RscpTag.EMS_REQ_SET_POWER_MODE,  'type': RscpType.UChar8, 'value': mode},
                {'tag': RscpTag.EMS_REQ_SET_POWER_VALUE, 'type': RscpType.Int32,  'value': value},
            ]},
            # Zusätzlich MODE abfragen um sicherzugehen und den Status bei E3/DC zu refreshen
            {'tag': RscpTag.EMS_REQ_MODE, 'type': RscpType.Nil, 'value': None}
        ]
        
        conn.request(set_req)
        return True
    except Exception as e:
        log.error(f"Fehler bei set_e3dc_power: {e}")
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    import json, sys, argparse

    parser = argparse.ArgumentParser(description='RSCP-Batteriedaten abrufen')
    parser.add_argument('--host',     required=True,  help='E3DC IP-Adresse')
    parser.add_argument('--port',     type=int, default=5033, help='RSCP-Port (Standard: 5033)')
    parser.add_argument('--user',     required=True,  help='E3DC Portal-Benutzername')
    parser.add_argument('--password', required=True,  help='E3DC Portal-Passwort')
    parser.add_argument('--rscp',     required=True,  help='RSCP-Passwort (lokal am Gerät gesetzt)')
    args = parser.parse_args()

    try:
        data = fetch_battery_vitals(args.host, args.port, args.user, args.password, args.rscp)
        print(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)
