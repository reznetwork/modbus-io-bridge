#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


def _normalize_device_name(name: str) -> str:
    return str(name).strip().lower()


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_yaml_if_exists(path: Optional[Path]) -> Optional[dict]:
    if not path:
        return None
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _index_merge_vehicles(merge_raw: Optional[dict]) -> Dict[str, dict]:
    """
    Return vehicle blocks keyed by short_name (preferred) or name.
    """
    if not merge_raw or not isinstance(merge_raw, dict):
        return {}

    vehicles = merge_raw.get("vehicles")
    if vehicles is None:
        # treat as single-vehicle legacy config
        name = str(merge_raw.get("name") or merge_raw.get("vehicle") or "default")
        short_name = merge_raw.get("short_name")
        if short_name is not None:
            short_name = str(short_name)
        key = short_name or name
        return {key: merge_raw}

    if not isinstance(vehicles, list):
        return {}

    out: Dict[str, dict] = {}
    for v in vehicles:
        if not isinstance(v, dict):
            continue
        name = v.get("name")
        short_name = v.get("short_name")
        if short_name:
            out[str(short_name)] = v
        if name:
            out.setdefault(str(name), v)
    return out


def _monitor_defaults(monitor_raw: dict) -> dict:
    pm = monitor_raw.get("pymodbus", {}) or {}
    unit_candidates = pm.get("unitCandidates") or []
    unit_id = 1
    if isinstance(unit_candidates, list) and unit_candidates:
        try:
            unit_id = int(unit_candidates[0])
        except Exception:
            unit_id = 1
    return {
        "device": {
            "port": int(pm.get("port", 502)),
            "timeout": float(pm.get("timeout", 3.0)),
            "unit_id": unit_id,
        }
    }


def _monitor_vehicles(monitor_raw: dict) -> List[dict]:
    vehicles = monitor_raw.get("vehicles") or []
    if not isinstance(vehicles, list):
        raise ValueError("'vehicles' must be a list in monitor config")

    out: List[dict] = []
    for v in vehicles:
        if not isinstance(v, dict):
            continue
        name = v.get("name")
        short_name = v.get("shortName")
        controllers = v.get("controllers") or []
        if not name or not short_name:
            continue
        if not isinstance(controllers, list):
            controllers = []

        devices: List[dict] = []
        for c in controllers:
            if not isinstance(c, dict):
                continue
            c_name = c.get("name")
            host = c.get("host")
            if not c_name or not host:
                continue
            devices.append({"name": _normalize_device_name(c_name), "host": str(host)})

        out.append(
            {
                "name": str(name),
                "short_name": str(short_name),
                "devices": devices,
            }
        )
    return out


def _merge_vehicle_blocks(base_vehicle: dict, merge_vehicle: dict) -> dict:
    """
    Preserve user-authored blocks (mappings/settings/server) from merge input.
    Devices are always regenerated from monitor config.
    """
    out = dict(base_vehicle)
    for key in ("mappings", "settings", "server"):
        if key in merge_vehicle:
            out[key] = merge_vehicle[key]
    return out


def _write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def main():
    ap = argparse.ArgumentParser(description="Generate modbus-io-bridge multi-vehicle config from monitor_config.json")
    ap.add_argument("--monitor", required=True, help="Path to monitor_config.json")
    ap.add_argument("--out", required=True, help="Output YAML path")
    ap.add_argument("--merge-in", help="Existing bridge YAML to preserve mappings/settings/server by short_name")
    args = ap.parse_args()

    monitor_path = Path(args.monitor)
    out_path = Path(args.out)
    merge_path = Path(args.merge_in) if args.merge_in else None

    monitor_raw = _load_json(monitor_path)
    merge_raw = _load_yaml_if_exists(merge_path)
    merge_index = _index_merge_vehicles(merge_raw)

    defaults = _monitor_defaults(monitor_raw)
    vehicles = _monitor_vehicles(monitor_raw)

    merged_vehicles: List[dict] = []
    for v in vehicles:
        key = v.get("short_name") or v.get("name")
        mv = merge_index.get(str(key)) if key else None
        if mv and isinstance(mv, dict):
            merged_vehicles.append(_merge_vehicle_blocks(v, mv))
        else:
            merged_vehicles.append(v)

    out = {"defaults": defaults, "vehicles": merged_vehicles}
    _write_yaml(out_path, out)


if __name__ == "__main__":
    main()

