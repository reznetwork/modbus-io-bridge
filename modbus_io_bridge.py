#!/usr/bin/env python3
"""
modbus_io_bridge.py
-------------------
Mirror digital inputs from one (or many) Modbus/TCP devices to coils on another (or many) devices.
- Supports multiple input and output devices.
- Logic is strictly 1 input -> 1 output per mapping.
- Async, resilient reconnects, optional debounce, optional invert, and on-error behavior.

Requires: pymodbus>=3.6.4 (asyncio client)
    pip install "pymodbus>=3.6.4"

Usage:
    python modbus_io_bridge.py --config config.yaml

Config example is provided in 'config.yaml' in the same folder.

Author: reznetwork@github.com
License: MIT
"""
import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import Dict, Optional

import yaml
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

# ---------------------------
# Data Models
# ---------------------------

@dataclass(frozen=True)
class DeviceConfig:
    name: str
    host: str
    port: int = 502
    unit_id: int = 1
    timeout: float = 3.0

@dataclass(frozen=True)
class InputEndpoint:
    device: str            # device name (must exist in devices[])
    address: int           # bit address to read
    source_type: str = "discrete_input"  # "discrete_input" or "coil"

@dataclass(frozen=True)
class OutputEndpoint:
    device: str            # device name (must exist in devices[])
    address: int           # coil address to write

@dataclass(frozen=True)
class Mapping:
    name: str
    input: InputEndpoint
    output: OutputEndpoint
    invert: bool = False
    debounce_ms: int = 0           # require stable state for this many ms before writing
    on_error: str = "hold"         # "hold" (do nothing) or "force_off" or "force_on"

@dataclass(frozen=True)
class Settings:
    poll_interval_ms: int = 100    # how often to poll each input
    log_level: str = "INFO"        # DEBUG/INFO/WARNING/ERROR
    connect_retry_s: float = 3.0   # reconnect backoff

# ---------------------------
# Device Manager
# ---------------------------

class DeviceManager:
    def __init__(self, devices: Dict[str, DeviceConfig]):
        self.devices_cfg = devices
        self.clients: Dict[str, AsyncModbusTcpClient] = {}

    async def start(self):
        # Lazy connect on first use
        pass

    async def stop(self):
        for name, client in list(self.clients.items()):
            try:
                await client.close()
            except Exception:
                pass

    async def _get_client(self, name: str) -> AsyncModbusTcpClient:
        if name not in self.clients:
            cfg = self.devices_cfg[name]
            client = AsyncModbusTcpClient(
                host=cfg.host, port=cfg.port, timeout=cfg.timeout, retries=0
            )
            self.clients[name] = client
        client = self.clients[name]
        if not client.connected:  # ensure connection
            await client.connect()
        return client

    async def read_bit(self, device: str, address: int, source_type: str) -> Optional[bool]:
        """
        Returns True/False or None on error.
        """
        try:
            client = await self._get_client(device)
            slave_id = self.devices_cfg[device].unit_id
            if source_type == "discrete_input":
                rr = await client.read_discrete_inputs(address=address, count=1, slave=slave_id)
            elif source_type == "coil":
                rr = await client.read_coils(address=address, count=1, slave=slave_id)
            else:
                raise ValueError(f"Unsupported source_type: {source_type}")
            if rr.isError():
                raise ModbusException(str(rr))
            return bool(rr.bits[0])
        except Exception as e:
            logging.debug(f"read_bit error on {device}@{address} ({source_type}): {e}")
            return None

    async def write_coil(self, device: str, address: int, value: bool) -> bool:
        """
        Returns True on success, False on failure.
        """
        try:
            client = await self._get_client(device)
            slave_id = self.devices_cfg[device].unit_id
            rq = await client.write_coil(address=address, value=value, slave=slave_id)
            if rq.isError():
                raise ModbusException(str(rq))
            return True
        except Exception as e:
            logging.debug(f"write_coil error on {device}@{address}: {e}")
            return False

# ---------------------------
# Mapping Worker
# ---------------------------

class MappingWorker:
    def __init__(self, name: str, dm: DeviceManager, mp: Mapping, settings: Settings):
        self.name = name
        self.dm = dm
        self.mp = mp
        self.settings = settings
        self._task: Optional[asyncio.Task] = None
        self._last_written: Optional[bool] = None
        self._candidate_state: Optional[bool] = None
        self._candidate_since: Optional[float] = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"map:{self.name}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        pi = self.settings.poll_interval_ms / 1000.0
        debounce_s = self.mp.debounce_ms / 1000.0
        while True:
            try:
                val = await self.dm.read_bit(self.mp.input.device, self.mp.input.address, self.mp.input.source_type)
                if val is None:
                    # Error behavior
                    if self.mp.on_error == "force_off":
                        await self._maybe_write(False)
                    elif self.mp.on_error == "force_on":
                        await self._maybe_write(True)
                    # hold => do nothing
                else:
                    desired = not val if self.mp.invert else val
                    if debounce_s <= 0:
                        await self._maybe_write(desired)
                    else:
                        now = asyncio.get_event_loop().time()
                        if self._candidate_state is None or self._candidate_state != desired:
                            self._candidate_state = desired
                            self._candidate_since = now
                        else:
                            if now - (self._candidate_since or 0) >= debounce_s:
                                await self._maybe_write(desired)
                await asyncio.sleep(pi)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.debug(f"Worker {self.name} loop error: {e}")
                await asyncio.sleep(self.settings.connect_retry_s)

    async def _maybe_write(self, desired: bool):
        if self._last_written is None or self._last_written != desired:
            ok = await self.dm.write_coil(self.mp.output.device, self.mp.output.address, desired)
            if ok:
                self._last_written = desired
                logging.info(f"[{self.name}] {self._format_io()} => wrote {int(desired)}")
            else:
                logging.warning(f"[{self.name}] write failed {self._format_io()} desired={int(desired)}")

    def _format_io(self) -> str:
        i = self.mp.input
        o = self.mp.output
        return f"IN {i.device}/{i.source_type}[{i.address}] -> OUT {o.device}/coil[{o.address}]"

# ---------------------------
# Config Loader
# ---------------------------

def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    devices = {
        d["name"]: DeviceConfig(
            name=d["name"],
            host=d["host"],
            port=d.get("port", 502),
            unit_id=d.get("unit_id", 1),
            timeout=float(d.get("timeout", 3.0)),
        )
        for d in raw.get("devices", [])
    }

    settings = Settings(
        poll_interval_ms=int(raw.get("settings", {}).get("poll_interval_ms", 100)),
        log_level=str(raw.get("settings", {}).get("log_level", "INFO")).upper(),
        connect_retry_s=float(raw.get("settings", {}).get("connect_retry_s", 3.0)),
    )

    mappings = {}
    for m in raw.get("mappings", []):
        name = m.get("name") or f"map_{m['input']['device']}_{m['input']['address']}__{m['output']['device']}_{m['output']['address']}"
        inp = m["input"]
        out = m["output"]
        mp = Mapping(
            name=name,
            input=InputEndpoint(
                device=inp["device"],
                address=int(inp["address"]),
                source_type=inp.get("source_type", "discrete_input"),
            ),
            output=OutputEndpoint(
                device=out["device"],
                address=int(out["address"]),
            ),
            invert=bool(m.get("invert", False)),
            debounce_ms=int(m.get("debounce_ms", 0)),
            on_error=str(m.get("on_error", "hold")).lower(),
        )
        # Validate device names
        if mp.input.device not in devices:
            raise ValueError(f"Mapping '{name}' references unknown input device '{mp.input.device}'")
        if mp.output.device not in devices:
            raise ValueError(f"Mapping '{name}' references unknown output device '{mp.output.device}'")
        mappings[name] = mp

    return devices, settings, mappings

# ---------------------------
# Main
# ---------------------------

async def main_async(cfg_path: str):
    devices, settings, mappings = load_config(cfg_path)

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    dm = DeviceManager(devices)

    workers = [MappingWorker(name, dm, mp, settings) for name, mp in mappings.items()]
    for w in workers:
        w.start()

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _stop)
        except NotImplementedError:
            # e.g., on Windows
            pass

    logging.info("Started Modbus IO bridge. Press Ctrl+C to stop.")
    await stop_event.wait()

    logging.info("Stopping workers...")
    await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)
    await dm.stop()
    logging.info("Stopped.")

def parse_args():
    p = argparse.ArgumentParser(description="Mirror Modbus digital inputs to coils across devices.")
    p.add_argument("--config", "-c", required=True, help="Path to YAML config")
    return p.parse_args()

def main():
    args = parse_args()
    try:
        asyncio.run(main_async(args.config))
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
