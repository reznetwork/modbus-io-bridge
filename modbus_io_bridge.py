#!/usr/bin/env python3
"""
modbus_io_bridge.py
-------------------
Mirror digital inputs from one (or many) Modbus/TCP devices to coils on another (or many) devices.
- Supports multiple input and output devices.
- Logic supports combining multiple inputs (AND/OR/NOT/XOR) into one output.
- Async, resilient reconnects, optional debounce, optional invert, and on-error behavior.
- Optional Modbus TCP/RTU server exposes logic results as discrete inputs for other devices.

Requires: pymodbus==3.6.9 (asyncio client)
    pip install "pymodbus==3.6.9"

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
import ast
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

import yaml
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.exceptions import ModbusException
from pymodbus.server import StartAsyncSerialServer, StartAsyncTcpServer
from pymodbus.transaction import ModbusRtuFramer

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
    name: Optional[str] = None            # optional alias used in logic expressions

@dataclass(frozen=True)
class OutputEndpoint:
    device: str            # device name (must exist in devices[])
    address: int           # coil address to write

@dataclass(frozen=True)
class Mapping:
    name: str
    output: OutputEndpoint
    input: Optional[InputEndpoint] = None
    inputs: List[InputEndpoint] = field(default_factory=list)
    logic: Optional[str] = None           # boolean expression using input names
    invert: bool = False
    debounce_ms: int = 0           # require stable state for this many ms before writing
    on_error: str = "hold"         # "hold" (do nothing) or "force_off" or "force_on"

@dataclass(frozen=True)
class Settings:
    poll_interval_ms: int = 100    # how often to poll each input
    log_level: str = "INFO"        # DEBUG/INFO/WARNING/ERROR
    connect_retry_s: float = 3.0   # reconnect backoff
    startup_blink: bool = False    # blink outputs after successful startup
    startup_blink_on_ms: int = 200
    startup_blink_off_ms: int = 200
    startup_blink_cycles: int = 2


@dataclass(frozen=True)
class PublishedMapping:
    name: str
    address: int


@dataclass(frozen=True)
class ServerConfig:
    enabled: bool = False
    protocol: str = "tcp"  # "tcp" or "rtu"
    host: str = "0.0.0.0"
    port: int = 1502
    unit_id: int = 1
    serial_port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    start_address: int = 0
    published: List[PublishedMapping] = field(default_factory=list)

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

    async def check_all_devices(self) -> Dict[str, bool]:
        """Attempt to connect to every configured device and log the result."""

        results: Dict[str, bool] = {}
        for name, cfg in self.devices_cfg.items():
            ok = await self._check_device(name, cfg)
            results[name] = ok
            if ok:
                logging.info(
                    "Device '%s' available at %s:%s (unit %s)",
                    name,
                    cfg.host,
                    cfg.port,
                    cfg.unit_id,
                )
            else:
                logging.error(
                    "Device '%s' unreachable at %s:%s (unit %s)",
                    name,
                    cfg.host,
                    cfg.port,
                    cfg.unit_id,
                )
        return results

    async def _check_device(self, name: str, cfg: DeviceConfig) -> bool:
        try:
            client = await self._get_client(name)
            if client.connected:
                return True
            # Try explicit connect if the client reports disconnected
            return await client.connect()
        except Exception as exc:
            logging.debug("Device check for %s failed: %s", name, exc)
            return False

    async def read_bit(self, device: str, address: int, source_type: str) -> Optional[bool]:
        """
        Returns True/False or None on error.
        """
        try:
            client = await self._get_client(device)
            slave_id = self.devices_cfg[device].unit_id
            if source_type == "discrete_input":
                rr = await client.read_discrete_inputs(address=address, count=1, unit=slave_id)
            elif source_type == "coil":
                rr = await client.read_coils(address=address, count=1, unit=slave_id)
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
            rq = await client.write_coil(address=address, value=value, unit=slave_id)
            if rq.isError():
                raise ModbusException(str(rq))
            return True
        except Exception as e:
            logging.debug(f"write_coil error on {device}@{address}: {e}")
            return False


class LogicResultServer:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.mapping_addresses: Dict[str, int] = {
            pm.name: pm.address for pm in (cfg.published or [])
        }
        self._di_block: Optional[ModbusSequentialDataBlock] = None
        self._task: Optional[asyncio.Task] = None
        self._context: Optional[ModbusServerContext] = None

    async def start(self):
        if not self.cfg.enabled:
            return
        if not self.mapping_addresses:
            logging.warning("Server enabled but no published mappings configured; skipping start")
            return
        min_addr = min(self.mapping_addresses.values())
        if min_addr < self.cfg.start_address:
            raise ValueError("Published mapping addresses cannot be below start_address")
        max_addr = max(self.mapping_addresses.values())
        di_size = (max_addr - self.cfg.start_address) + 1
        self._di_block = ModbusSequentialDataBlock(self.cfg.start_address, [0] * di_size)
        empty_block = ModbusSequentialDataBlock(0, [0])
        slave = ModbusSlaveContext(
            di=self._di_block,
            co=empty_block,
            hr=empty_block,
            ir=empty_block,
            zero_mode=True,
        )
        self._context = ModbusServerContext(slaves={self.cfg.unit_id: slave}, single=False)

        async def _run_server():
            if self.cfg.protocol.lower() == "tcp":
                await StartAsyncTcpServer(
                    context=self._context, address=(self.cfg.host, self.cfg.port)
                )
            elif self.cfg.protocol.lower() == "rtu":
                await StartAsyncSerialServer(
                    context=self._context,
                    framer=ModbusRtuFramer,
                    port=self.cfg.serial_port,
                    baudrate=self.cfg.baudrate,
                    bytesize=self.cfg.bytesize,
                    parity=self.cfg.parity,
                    stopbits=self.cfg.stopbits,
                )
            else:
                raise ValueError(f"Unsupported server protocol: {self.cfg.protocol}")

        self._task = asyncio.create_task(_run_server(), name="logic-server")
        logging.info("Started logic Modbus %s server", self.cfg.protocol.upper())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def publish(self, mapping_name: str, value: bool):
        if not self._di_block or mapping_name not in self.mapping_addresses:
            return
        address = self.mapping_addresses[mapping_name]
        try:
            self._di_block.setValues(address, [int(bool(value))])
        except Exception as exc:
            logging.debug(
                "Failed to publish logic state for %s at %s: %s",
                mapping_name,
                address,
                exc,
            )

# ---------------------------
# Mapping Worker
# ---------------------------

class MappingWorker:
    def __init__(
        self,
        name: str,
        dm: DeviceManager,
        mp: Mapping,
        settings: Settings,
        server: Optional[LogicResultServer] = None,
    ):
        self.name = name
        self.dm = dm
        self.mp = mp
        self.settings = settings
        self._task: Optional[asyncio.Task] = None
        self._last_written: Optional[bool] = None
        self._last_published: Optional[bool] = None
        self._candidate_state: Optional[bool] = None
        self._candidate_since: Optional[float] = None
        self._inputs = self.mp.inputs if self.mp.inputs else ([self.mp.input] if self.mp.input else [])
        self.server = server

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
                values = await self._read_inputs()
                if values is None:
                    # Error behavior
                    if self.mp.on_error == "force_off":
                        await self._maybe_write(False)
                    elif self.mp.on_error == "force_on":
                        await self._maybe_write(True)
                    # hold => do nothing
                else:
                    desired = self._evaluate_logic(values)
                    if self.mp.invert:
                        desired = not desired
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
        self._publish(desired)
        if self._last_written is None or self._last_written != desired:
            ok = await self.dm.write_coil(self.mp.output.device, self.mp.output.address, desired)
            if ok:
                self._last_written = desired
                logging.info(f"[{self.name}] {self._format_io()} => wrote {int(desired)}")
            else:
                logging.warning(f"[{self.name}] write failed {self._format_io()} desired={int(desired)}")

    def _publish(self, desired: bool):
        if self.server and (self._last_published is None or self._last_published != desired):
            self.server.publish(self.name, desired)
            self._last_published = desired

    def _format_io(self) -> str:
        o = self.mp.output
        if self._inputs:
            ins = ", ".join(
                f"{inp.name or inp.device}:{inp.source_type}[{inp.address}]" for inp in self._inputs
            )
        else:
            ins = "<no inputs>"
        return f"IN {ins} -> OUT {o.device}/coil[{o.address}]"

    async def _read_inputs(self) -> Optional[Dict[str, bool]]:
        values: Dict[str, bool] = {}
        for idx, inp in enumerate(self._inputs):
            val = await self.dm.read_bit(inp.device, inp.address, inp.source_type)
            if val is None:
                return None
            key = inp.name or f"in{idx+1}"
            values[key] = bool(val)
        return values

    def _evaluate_logic(self, values: Dict[str, bool]) -> bool:
        if self.mp.logic:
            return _evaluate_expression(self.mp.logic, values)
        # Fallback to single input
        if len(values) != 1:
            raise ValueError(f"Mapping '{self.name}' requires logic for multiple inputs")
        return next(iter(values.values()))


def _evaluate_expression(expr: str, values: Dict[str, bool]) -> bool:
    """
    Evaluate a simple boolean expression using provided values.
    Supports AND/OR/NOT and XOR (using ^) operators.
    """

    def _eval(node: ast.AST) -> bool:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.BoolOp):
            vals = [_eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(vals)
            if isinstance(node.op, ast.Or):
                return any(vals)
            raise ValueError("Only AND/OR boolean operators are supported")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitXor):
            return _eval(node.left) ^ _eval(node.right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not _eval(node.operand)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"Unknown variable '{node.id}' in logic expression")
            return bool(values[node.id])
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return bool(node.value)
        raise ValueError("Unsupported expression; use AND, OR, NOT, XOR (^), and parentheses")

    try:
        parsed = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid logic expression '{expr}': {exc}" ) from exc
    return bool(_eval(parsed))


def _derive_mapping_name(raw_mapping: dict) -> str:
    if raw_mapping.get("name"):
        return str(raw_mapping["name"])
    if "input" in raw_mapping:
        inp = raw_mapping["input"]
        out = raw_mapping["output"]
        return f"map_{inp['device']}_{inp['address']}__{out['device']}_{out['address']}"
    if raw_mapping.get("inputs"):
        first = raw_mapping["inputs"][0]
        out = raw_mapping["output"]
        return f"map_{first['device']}_{first['address']}__{out['device']}_{out['address']}"
    raise ValueError("Mapping entry must include 'name', or at least inputs and output")

# ---------------------------
# Config Loader
# ---------------------------

def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    _validate_root_structure(raw)

    raw_devices = raw.get("devices", []) or []
    _validate_devices_raw(raw_devices)

    devices = {
        d["name"]: DeviceConfig(
            name=d["name"],
            host=d["host"],
            port=d.get("port", 502),
            unit_id=d.get("unit_id", 1),
            timeout=float(d.get("timeout", 3.0)),
        )
        for d in raw_devices
    }

    settings = Settings(
        poll_interval_ms=int(raw.get("settings", {}).get("poll_interval_ms", 100)),
        log_level=str(raw.get("settings", {}).get("log_level", "INFO")).upper(),
        connect_retry_s=float(raw.get("settings", {}).get("connect_retry_s", 3.0)),
        startup_blink=bool(raw.get("settings", {}).get("startup_blink", False)),
        startup_blink_on_ms=int(raw.get("settings", {}).get("startup_blink_on_ms", 200)),
        startup_blink_off_ms=int(raw.get("settings", {}).get("startup_blink_off_ms", 200)),
        startup_blink_cycles=int(raw.get("settings", {}).get("startup_blink_cycles", 2)),
    )

    server_raw = raw.get("server", {}) or {}
    start_address = int(server_raw.get("start_address", 0))
    published: List[PublishedMapping] = []
    for idx, pub in enumerate(server_raw.get("publish_mappings", [])):
        if isinstance(pub, str):
            name = pub
            addr = start_address + idx
        else:
            name = pub["name"]
            addr = int(pub.get("address", start_address + idx))
        published.append(PublishedMapping(name=name, address=addr))

    server_cfg = ServerConfig(
        enabled=bool(server_raw.get("enabled", False)),
        protocol=str(server_raw.get("protocol", "tcp")),
        host=str(server_raw.get("host", "0.0.0.0")),
        port=int(server_raw.get("port", 1502)),
        unit_id=int(server_raw.get("unit_id", 1)),
        serial_port=str(server_raw.get("serial_port", "/dev/ttyUSB0")),
        baudrate=int(server_raw.get("baudrate", 9600)),
        bytesize=int(server_raw.get("bytesize", 8)),
        parity=str(server_raw.get("parity", "N")),
        stopbits=int(server_raw.get("stopbits", 1)),
        start_address=start_address,
        published=published,
    )

    raw_mappings = raw.get("mappings", []) or []
    _validate_mapping_names(raw_mappings)

    mappings = {}
    for m in raw_mappings:
        name = _derive_mapping_name(m)
        inputs: List[InputEndpoint] = []
        logic_expr = m.get("logic")
        if "inputs" in m:
            if not m["inputs"]:
                raise ValueError(f"Mapping '{name}' must specify at least one input")
            for idx, inp_cfg in enumerate(m["inputs"]):
                alias = inp_cfg.get("name") or f"in{idx+1}"
                inputs.append(
                    InputEndpoint(
                        device=inp_cfg["device"],
                        address=int(inp_cfg["address"]),
                        source_type=inp_cfg.get("source_type", "discrete_input"),
                        name=alias,
                    )
                )
            if logic_expr is None:
                raise ValueError(f"Mapping '{name}' using multiple inputs requires 'logic'")
        elif "input" in m:
            inp = m["input"]
            inputs.append(
                InputEndpoint(
                    device=inp["device"],
                    address=int(inp["address"]),
                    source_type=inp.get("source_type", "discrete_input"),
                    name=inp.get("name"),
                )
            )
        else:
            raise ValueError(f"Mapping '{name}' is missing 'input' or 'inputs'")
        out = m["output"]
        mp = Mapping(
            name=name,
            input=inputs[0] if len(inputs) == 1 else None,
            inputs=inputs,
            logic=logic_expr,
            output=OutputEndpoint(
                device=out["device"],
                address=int(out["address"]),
            ),
            invert=bool(m.get("invert", False)),
            debounce_ms=int(m.get("debounce_ms", 0)),
            on_error=str(m.get("on_error", "hold")).lower(),
        )
        # Validate device names
        for inp in mp.inputs:
            if inp.device not in devices:
                raise ValueError(f"Mapping '{name}' references unknown input device '{inp.device}'")
        if mp.output.device not in devices:
            raise ValueError(f"Mapping '{name}' references unknown output device '{mp.output.device}'")
        mappings[name] = mp

    for pub in server_cfg.published:
        if pub.name not in mappings:
            raise ValueError(
                f"Server publish_mappings references unknown mapping '{pub.name}'"
            )

    _validate_devices(devices)
    _validate_mappings(mappings)

    return devices, settings, mappings, server_cfg


def _validate_root_structure(raw: dict):
    if raw is None:
        raise ValueError("Configuration file is empty")
    if not isinstance(raw, dict):
        raise ValueError("Configuration file must contain a mapping at the root")


def _validate_devices(devices: Dict[str, DeviceConfig]):
    if not devices:
        raise ValueError("Configuration must define at least one device")


def _validate_devices_raw(raw_devices: List[dict]):
    if not raw_devices:
        raise ValueError("Configuration must define at least one device")
    names_seen: Set[str] = set()
    for dev in raw_devices:
        name = dev.get("name")
        if not name:
            raise ValueError("Every device entry must include a 'name'")
        if name in names_seen:
            raise ValueError(f"Duplicate device name '{name}' in configuration")
        names_seen.add(name)


def _validate_mappings(mappings: Dict[str, Mapping]):
    if not mappings:
        raise ValueError("Configuration must define at least one mapping")
    outputs_seen: Set[tuple] = set()
    for mp in mappings.values():
        out_key = (mp.output.device, mp.output.address)
        if out_key in outputs_seen:
            raise ValueError(
                f"Multiple mappings attempt to drive the same output {mp.output.device}:{mp.output.address}"
            )
        outputs_seen.add(out_key)


def _validate_mapping_names(raw_mappings: List[dict]):
    if not raw_mappings:
        raise ValueError("Configuration must define at least one mapping")
    names: Set[str] = set()
    for raw in raw_mappings:
        name = raw.get("name")
        if not name:
            # fall back to derived name for duplicates check
            name = _derive_mapping_name(raw)
        if name in names:
            raise ValueError(f"Duplicate mapping name '{name}' in configuration")
        names.add(name)


async def _blink_outputs(mappings: Iterable[Mapping], dm: DeviceManager, settings: Settings):
    outputs = _collect_unique_outputs(mappings)
    if not outputs:
        logging.info("No outputs configured to blink on startup.")
        return
    on_s = settings.startup_blink_on_ms / 1000.0
    off_s = settings.startup_blink_off_ms / 1000.0
    cycles = max(1, settings.startup_blink_cycles)
    logging.info(
        "Blinking %s outputs for startup confirmation (%s cycles on %sms/off %sms)",
        len(outputs),
        cycles,
        settings.startup_blink_on_ms,
        settings.startup_blink_off_ms,
    )
    for idx in range(cycles):
        await _write_outputs(outputs, dm, True, phase=f"on-cycle-{idx+1}")
        await asyncio.sleep(on_s)
        await _write_outputs(outputs, dm, False, phase=f"off-cycle-{idx+1}")
        if idx < cycles - 1:
            await asyncio.sleep(off_s)
    logging.info("Startup blink completed.")


def _collect_unique_outputs(mappings: Iterable[Mapping]) -> List[OutputEndpoint]:
    seen: Set[tuple] = set()
    outputs: List[OutputEndpoint] = []
    for mp in mappings:
        key = (mp.output.device, mp.output.address)
        if key in seen:
            continue
        seen.add(key)
        outputs.append(mp.output)
    return outputs


async def _write_outputs(outputs: List[OutputEndpoint], dm: DeviceManager, value: bool, phase: str):
    results = await asyncio.gather(
        *(dm.write_coil(out.device, out.address, value) for out in outputs), return_exceptions=True
    )
    for out, res in zip(outputs, results):
        if res is True:
            continue
        if isinstance(res, Exception):
            logging.warning(
                "Startup blink %s failed for %s:%s with exception: %s",
                phase,
                out.device,
                out.address,
                res,
            )
        else:
            logging.warning(
                "Startup blink %s failed for %s:%s (write returned False)",
                phase,
                out.device,
                out.address,
            )

# ---------------------------
# Main
# ---------------------------

async def main_async(cfg_path: str):
    devices, settings, mappings, server_cfg = load_config(cfg_path)

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    logging.info(
        "Configuration validated: %s devices, %s mappings, server enabled=%s",
        len(devices),
        len(mappings),
        server_cfg.enabled,
    )

    dm = DeviceManager(devices)
    logic_server = LogicResultServer(server_cfg)
    await logic_server.start()

    device_status = await dm.check_all_devices()
    if device_status:
        unavailable = [name for name, ok in device_status.items() if not ok]
        if unavailable:
            logging.warning("Some devices are unreachable at startup: %s", ", ".join(unavailable))
        else:
            logging.info("All configured devices are reachable.")

    if settings.startup_blink:
        if device_status and all(device_status.values()):
            await _blink_outputs(mappings.values(), dm, settings)
        else:
            logging.info("Skipping startup blink because not all devices are reachable.")

    workers = [
        MappingWorker(name, dm, mp, settings, server=logic_server)
        for name, mp in mappings.items()
    ]
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
    await logic_server.stop()
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
    except Exception as exc:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.error("Fatal error during startup: %s", exc)
        raise

if __name__ == "__main__":
    main()
