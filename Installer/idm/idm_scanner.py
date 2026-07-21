#!/usr/bin/env python3
"""Eng begrenzte Read-only-Diagnose für den iDM Navigator 2.0.

Der Scanner liest ausschließlich das herstellerdokumentierte Input-Register
1006 (Smart-Grid-Status) mit genau einer FC04-Anfrage. Er führt weder einen
Register-Sweep noch Holding-Register-Lesungen oder Modbus-Schreibzugriffe aus.
"""

from __future__ import annotations

import argparse
import inspect
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:  # Die Modulprüfung bleibt ohne optionale Abhängigkeit wirkungslos.
    ModbusTcpClient = None


DEFAULT_CONFIG_PATH = Path("/var/www/html/data/e3dc_v4.json")
IDM_PORT = 502
IDM_SMART_GRID_REGISTER = 1006
IDM_SMART_GRID_COUNT = 1
IDM_ADDRESS_MODE = "manufacturer_io_address_direct_no_offset"
IDM_MANUFACTURER_DEFAULT_UNIT_ID = 1
IDM_NAVIGATOR_MODEL = "Navigatorregelung 2.0"
IDM_NAVIGATOR_GENERATION = "2.0"
IDM_MINIMUM_FIRMWARE = (20, 21, 101)
IDM_MINIMUM_FIRMWARE_TEXT = "20.21-101"
IDM_DOCUMENT = "Modbus TCP Navigator 2.0 DE, 812170 Rev.10, 20.04.2022"
IDM_DOCUMENT_URL = (
    "https://api.library.loxone.com/downloader/file/647/"
    "Modbus%2520TCP_Navigator%25202.0_DE.pdf"
)

# Herstellerbedeutungen für Register 1006, Dokumentseite 12/13.
IDM_SMART_GRID_VALUES = {
    0: "EVU-Sperre und kein günstiger Strom",
    1: "EVU-Bezug und kein günstiger Strom",
    2: "Kein EVU-Bezug und günstiger Strom",
    4: "EVU-Sperre und günstiger Strom",
}

EXIT_OK = 0
EXIT_RAW_ONLY = 2
EXIT_CONFIG = 3
EXIT_TRANSPORT = 4
EXIT_DEPENDENCY = 5


def _parse_navigator_version(value: Any) -> tuple[int, int, int] | None:
    """Parst ausschließlich das dokumentierte Schema ``major.minor-build``."""
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)\.(\d+)-(\d+)\s*", str(value))
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _parse_unit_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 247 else None


def _contract_gate(
    *,
    navigator_model: Any,
    navigator_generation: Any,
    navigator_version: Any,
    configured_unit_id: Any,
) -> dict[str, Any]:
    unit_was_configured = configured_unit_id is not None and str(configured_unit_id).strip() != ""
    parsed_unit_id = _parse_unit_id(configured_unit_id)
    effective_unit_id = (
        IDM_MANUFACTURER_DEFAULT_UNIT_ID if not unit_was_configured else parsed_unit_id
    )
    unit_source = "configured" if unit_was_configured else "manufacturer_default_unbound"

    reason = None
    if navigator_model is None or not str(navigator_model).strip():
        reason = "navigator_model_unbound"
    elif str(navigator_model).strip() != IDM_NAVIGATOR_MODEL:
        reason = "navigator_model_outside_contract"
    elif navigator_generation is None or not str(navigator_generation).strip():
        reason = "protocol_generation_unbound"
    elif str(navigator_generation).strip() != IDM_NAVIGATOR_GENERATION:
        reason = "protocol_generation_outside_contract"
    else:
        parsed_version = _parse_navigator_version(navigator_version)
        if parsed_version is None:
            reason = "firmware_version_unbound_or_invalid"
        elif parsed_version < IDM_MINIMUM_FIRMWARE:
            reason = "firmware_version_below_contract"
        elif not unit_was_configured:
            reason = "unit_id_default_unbound"
        elif parsed_unit_id != IDM_MANUFACTURER_DEFAULT_UNIT_ID:
            reason = "configured_unit_id_outside_contract"

    if unit_was_configured and parsed_unit_id != IDM_MANUFACTURER_DEFAULT_UNIT_ID:
        effective_unit_id = None

    return {
        "usable": reason is None,
        "reason": reason,
        "effective_unit_id": effective_unit_id,
        "unit_id_source": unit_source,
        "parsed_firmware": _parse_navigator_version(navigator_version),
    }


def _read_input_registers(
    client: Any,
    *,
    address: int,
    count: int,
    unit_id: int,
) -> Any:
    """Erzeugt genau eine FC04-Anfrage mit der lokalen pymodbus-ID-Signatur."""
    method = client.read_input_registers
    parameters = inspect.signature(method).parameters
    if "device_id" in parameters:
        unit_keyword = "device_id"
    elif "slave" in parameters:
        unit_keyword = "slave"
    elif "unit" in parameters:
        unit_keyword = "unit"
    elif any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        unit_keyword = "device_id"
    else:
        raise TypeError("pymodbus_unit_id_parameter_missing")
    return method(address=address, count=count, **{unit_keyword: unit_id})


def _base_result(gate: dict[str, Any]) -> dict[str, Any]:
    parsed_firmware = gate.get("parsed_firmware")
    return {
        "contract": "idm_navigator_2_0_smart_grid_input_v1",
        "document": IDM_DOCUMENT,
        "document_url": IDM_DOCUMENT_URL,
        "function_code": 4,
        "register": IDM_SMART_GRID_REGISTER,
        "manufacturer_io_address": IDM_SMART_GRID_REGISTER,
        "pymodbus_address": IDM_SMART_GRID_REGISTER,
        "address_mode": IDM_ADDRESS_MODE,
        "count": IDM_SMART_GRID_COUNT,
        "unit_id": gate.get("effective_unit_id"),
        "unit_id_source": gate.get("unit_id_source"),
        "minimum_firmware": IDM_MINIMUM_FIRMWARE_TEXT,
        "parsed_firmware": (
            ".".join((str(parsed_firmware[0]), str(parsed_firmware[1])))
            + f"-{parsed_firmware[2]}"
            if parsed_firmware is not None
            else None
        ),
        "transport_attempted": False,
        "transport_valid": False,
        "available": False,
        "valid": False,
        "raw_word": None,
        "value": None,
        "manufacturer_meaning": None,
        "reason": gate.get("reason"),
    }


def read_smart_grid_status(
    client: Any,
    *,
    navigator_model: Any,
    navigator_generation: Any,
    navigator_version: Any,
    configured_unit_id: Any,
) -> dict[str, Any]:
    """Liest Register 1006 einmalig; ungebundene Quellen bleiben raw-only."""
    gate = _contract_gate(
        navigator_model=navigator_model,
        navigator_generation=navigator_generation,
        navigator_version=navigator_version,
        configured_unit_id=configured_unit_id,
    )
    result = _base_result(gate)
    unit_id = gate["effective_unit_id"]
    if unit_id != IDM_MANUFACTURER_DEFAULT_UNIT_ID:
        result["reason"] = "configured_unit_id_outside_contract"
        return result

    result["transport_attempted"] = True
    try:
        response = _read_input_registers(
            client,
            address=IDM_SMART_GRID_REGISTER,
            count=IDM_SMART_GRID_COUNT,
            unit_id=unit_id,
        )
    except Exception as exc:
        result["reason"] = f"fc04_transport_error:{type(exc).__name__}"
        return result

    if response is None or bool(response.isError()):
        result["reason"] = "fc04_error_response"
        return result
    registers = getattr(response, "registers", None)
    if not isinstance(registers, (list, tuple)) or len(registers) != 1:
        result["reason"] = "fc04_register_count_invalid"
        return result
    try:
        raw_word = int(registers[0])
    except (TypeError, ValueError):
        result["reason"] = "fc04_register_type_invalid"
        return result
    if not 0 <= raw_word <= 0xFFFF:
        result["reason"] = "fc04_register_range_invalid"
        return result

    result["transport_valid"] = True
    result["raw_word"] = raw_word
    if raw_word == 0xFFFF:
        result["reason"] = "sentinel_0xffff_unavailable"
        return result
    if raw_word not in IDM_SMART_GRID_VALUES:
        result["reason"] = "enum_value_outside_manufacturer_contract"
        return result
    if not gate["usable"]:
        result["reason"] = gate["reason"]
        return result

    result.update(
        {
            "available": True,
            "valid": True,
            "value": raw_word,
            "manufacturer_meaning": IDM_SMART_GRID_VALUES[raw_word],
            "reason": "confirmed_from_fc04_input_read",
        }
    )
    return result


def _validate_host(value: Any) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def load_probe_config(config_path: Path) -> dict[str, Any]:
    """Lädt nur die explizite JSON-Konfiguration; es gibt keinen Fallback-Scan."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "reason": f"config_unreadable:{type(exc).__name__}"}
    if not isinstance(payload, dict):
        return {"valid": False, "reason": "config_root_not_object"}
    host = _validate_host(payload.get("idm_ip"))
    if host is None:
        return {"valid": False, "reason": "idm_host_missing_or_invalid"}
    return {
        "valid": True,
        "host": host,
        "navigator_model": payload.get("idm_model"),
        "navigator_generation": payload.get("idm_navigator_generation"),
        "navigator_version": payload.get("idm_firmware"),
        "configured_unit_id": payload.get("idm_unit_id"),
    }


def run_probe(
    *,
    host: Any,
    navigator_model: Any,
    navigator_generation: Any,
    navigator_version: Any,
    configured_unit_id: Any,
    client_factory: Callable[..., Any] | None = ModbusTcpClient,
) -> dict[str, Any]:
    """Verbindet einmal und führt höchstens eine FC04-Anfrage aus."""
    gate = _contract_gate(
        navigator_model=navigator_model,
        navigator_generation=navigator_generation,
        navigator_version=navigator_version,
        configured_unit_id=configured_unit_id,
    )
    validated_host = _validate_host(host)
    if validated_host is None:
        result = _base_result(gate)
        result["reason"] = "idm_host_missing_or_invalid"
        return result
    if gate["effective_unit_id"] != IDM_MANUFACTURER_DEFAULT_UNIT_ID:
        result = _base_result(gate)
        result["reason"] = "configured_unit_id_outside_contract"
        return result
    if client_factory is None:
        result = _base_result(gate)
        result["reason"] = "pymodbus_dependency_missing"
        return result

    client = client_factory(validated_host, port=IDM_PORT, timeout=2)
    try:
        if not client.connect():
            result = _base_result(gate)
            result["reason"] = "modbus_connect_failed"
            return result
        return read_smart_grid_status(
            client,
            navigator_model=navigator_model,
            navigator_generation=navigator_generation,
            navigator_version=navigator_version,
            configured_unit_id=configured_unit_id,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def _prefer_override(override: Any, configured: Any) -> Any:
    return configured if override is None else override


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("valid"):
        return EXIT_OK
    reason = str(result.get("reason") or "")
    if reason == "pymodbus_dependency_missing":
        return EXIT_DEPENDENCY
    if reason.startswith("config_") or reason == "idm_host_missing_or_invalid":
        return EXIT_CONFIG
    if result.get("transport_valid"):
        return EXIT_RAW_ONLY
    return EXIT_TRANSPORT


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Liest genau einmal iDM Input-Register 1006 (FC04)."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--model")
    parser.add_argument("--generation")
    parser.add_argument("--firmware")
    parser.add_argument("--unit-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    configured = load_probe_config(arguments.config)
    if not configured.get("valid"):
        result = {
            "valid": False,
            "available": False,
            "transport_attempted": False,
            "transport_valid": False,
            "reason": configured.get("reason"),
        }
    else:
        result = run_probe(
            host=configured["host"],
            navigator_model=_prefer_override(arguments.model, configured["navigator_model"]),
            navigator_generation=_prefer_override(
                arguments.generation, configured["navigator_generation"]
            ),
            navigator_version=_prefer_override(arguments.firmware, configured["navigator_version"]),
            configured_unit_id=_prefer_override(
                arguments.unit_id, configured["configured_unit_id"]
            ),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
