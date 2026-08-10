# -*- coding: utf-8 -*-
"""Deterministische Identitäten für passive DV-Policyverträge."""

import hashlib
import json


PASSIVE_NORMAL_BINDING_SCHEMA = "direct_marketing_passive_normal_binding_v1"
PASSIVE_NORMAL_ACTION = "eco_plus_house_supply"
POLICY_SCHEMA = "direct_marketing_policy_v1"


def _contract_id(material):
    payload = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def passive_normal_identity(
    *,
    start_ts,
    end_ts,
    selected_start_ts,
    selected_end_ts,
    window_id=None,
):
    """Berechnet die Action- und Slot-ID aus dem vollständigen Semantikkern."""

    action_id = _contract_id({
        "schema": PASSIVE_NORMAL_BINDING_SCHEMA,
        "policy_schema": POLICY_SCHEMA,
        "mode": "eco_plus",
        "action": PASSIVE_NORMAL_ACTION,
    })
    slot_id = _contract_id({
        "schema": PASSIVE_NORMAL_BINDING_SCHEMA,
        "policy_action_id": action_id,
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "selected_start_ts": int(selected_start_ts),
        "selected_end_ts": int(selected_end_ts),
        "window_id": str(window_id or ""),
    })
    return {
        "schema": PASSIVE_NORMAL_BINDING_SCHEMA,
        "policy_action_id": action_id,
        "policy_slot_id": slot_id,
        "action": PASSIVE_NORMAL_ACTION,
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "selected_start_ts": int(selected_start_ts),
        "selected_end_ts": int(selected_end_ts),
        "window_id": str(window_id or "") or None,
    }
