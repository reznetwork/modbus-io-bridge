# modbus-io-bridge

Bridge Modbus/TCP inputs to outputs with optional debounce, inversion, and
basic boolean logic between multiple inputs. The bridge can also expose the
evaluated logic results as discrete inputs on a built-in Modbus TCP or RTU
server so other devices can consume them.

Tested with `pymodbus==3.6.9`. Use that exact version for best compatibility.

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
