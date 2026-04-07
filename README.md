# modbus-io-bridge

Bridge Modbus/TCP inputs to outputs with optional debounce, inversion, and
basic boolean logic between multiple inputs. The bridge can also expose the
evaluated logic results as discrete inputs on a built-in Modbus TCP or RTU
server so other devices can consume them.

Tested with `pymodbus==3.12.1`.

## Installation

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Multi-vehicle configuration

You can keep multiple vehicles in one config file under `vehicles:` and run a
single one by selecting it via `--vehicle`.

Vehicle identifiers:

- Prefer using `vehicles[].short_name` (matches `monitor_config.json` `shortName`)
- You can also use the exact `vehicles[].name`

Example:

```bash
python3 modbus_io_bridge.py --config config.yaml --vehicle mtz_82
```

### Generating vehicles/devices from `monitor_config.json`

To populate vehicle names, `short_name`, and controller IPs into a bridge config:

```bash
python3 tools/generate_bridge_config.py --monitor monitor_config.json --out config.generated.yaml --merge-in config.yaml
```

- `--merge-in` preserves any existing per-vehicle `mappings`, `settings`, and `server` blocks (keyed by `short_name`).
- The generator does **not** auto-generate `mappings` from monitor `points` (different semantics).

## Startup validation and indicators

On startup the bridge validates your configuration, checks connectivity to every
configured device, and logs the results. You can optionally blink all configured
outputs as a visual confirmation that initialization succeeded by enabling
`startup_blink` in the `settings` section of your configuration. Control the
blink timing with `startup_blink_on_ms`, `startup_blink_off_ms`, and
`startup_blink_cycles`.

## Logical expressions

Mappings can combine several inputs into a single output using a boolean
`logic` expression. Expressions support the operators `and`, `or`, `not`, and
XOR (`^`), as well as parentheses for grouping. Operator precedence follows
Python boolean rules (`not` → `and` → `or`/`^`).

## Fuzzy (rolling-window) logic

Optionally, a mapping can derive its output state from recent history using a
rolling time window.

- **rolling_window**: turn output ON when at least `min_on` ON-events occurred in
  the last `window_s` seconds.
- **modes**:
  - `rising_edge`: counts only False→True transitions (best match for “was on N times”)
  - `sample_true`: counts every poll sample that is True

Example: “if input was ON at least 5 times over the last minute, output is ON”:

```yaml
mappings:
  - name: horn_burst_filter
    input:
      device: mb_io_2
      address: 9
      source_type: discrete_input
    fuzzy:
      type: rolling_window
      window_s: 60
      min_on: 5
      mode: rising_edge
    output:
      device: mb_io_1
      address: 2
```

### Referencing inputs inside expressions

Each input may define an optional `name` that is used inside the expression. If
no name is given, the inputs are auto-labeled `in1`, `in2`, `in3`, … in the
order they appear in the configuration. Attempting to reference an unknown name
raises a configuration error.

### Interaction with other mapping options

- `invert` still applies after the logic result has been computed. Use this to
  flip the final output instead of adjusting individual input terms.
- `debounce_ms` controls how long a stable logical result is required before the
  output is updated, regardless of how many inputs changed.
- `on_error` continues to govern what happens if any of the inputs fail to read
  (e.g., `hold`, `force_off`, `force_on`).

### Example: named inputs

```yaml
mappings:
  - name: headlights_logic
    inputs:
      - name: left
        device: mb_io_2
        address: 5
      - name: right
        device: mb_io_2
        address: 8
    logic: "left ^ right"  # output is on only when exactly one switch is on
    output:
      device: mb_io_2
      address: 20
```

### Example: implicit input names

When `name` is omitted for an input, refer to it by its position (`in1`, `in2`,
etc.):

```yaml
mappings:
  - name: fan_enable_logic
    inputs:
      - device: mb_io_2
        address: 2
      - device: mb_io_3
        address: 7
    logic: "in1 and not in2"  # turn the fan on unless the interlock blocks it
    output:
      device: mb_io_1
      address: 12
```

## Exposing logic as a Modbus server

Enable the optional server to let downstream systems read the logic outputs as
discrete inputs. You can run the server over TCP or RTU by selecting a
`protocol` and providing either TCP host/port or serial line parameters. Each
entry in `publish_mappings` maps a mapping name to a discrete input address; if
`address` is omitted, addresses are assigned sequentially starting at
`start_address`.

```yaml
server:
  enabled: true
  protocol: tcp           # tcp | rtu
  host: 0.0.0.0
  port: 1502
  unit_id: 1
  start_address: 0
  publish_mappings:
    - name: handbrake     # published as discrete input 0
    - name: neutral       # published as discrete input 1

  # RTU-specific options (only used when protocol: rtu)
  serial_port: /dev/ttyUSB0
  baudrate: 9600
  bytesize: 8
  parity: N
  stopbits: 1
```


### Example: require two signals on and a third signal off

Use `and` to assert the two inputs that must be on, and `not` to enforce the
third input is off:

```yaml
mappings:
  - name: two_on_one_off
    inputs:
      - name: primary
        device: mb_io_1
        address: 4
      - name: secondary
        device: mb_io_1
        address: 5
      - name: inhibit
        device: mb_io_1
        address: 6
    logic: "primary and secondary and not inhibit"
    output:
      device: mb_io_2
      address: 11
```
