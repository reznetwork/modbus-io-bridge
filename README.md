# modbus-io-bridge

Bridge Modbus/TCP **and RTU** inputs to outputs with optional debounce,
inversion, and basic boolean logic between multiple inputs. The bridge can also
expose computed logic results as **Modbus/TCP discrete inputs** so that external
controllers can query the state of each mapping.

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

## Modbus/RTU inputs and outputs

Define an RTU device by setting `protocol: rtu` and providing a serial
configuration. All other mapping options work the same way as TCP devices.

```yaml
devices:
  - name: mb_io_rtu
    protocol: rtu
    serial_port: /dev/ttyUSB0
    baudrate: 19200
    parity: N
    bytesize: 8
    stopbits: 1
    unit_id: 1
    timeout: 3.0
```

## Exposing mapping outputs as a Modbus server

When server support is enabled the bridge publishes each mapping's logic result
as a discrete input starting at `server_base_di_address`, allowing external
clients to read the logical state without wiring another output device.

```yaml
settings:
  server_enabled: true
  server_host: 0.0.0.0
  server_port: 1502
  server_base_di_address: 0
```

Addresses are assigned in mapping order; for example, the first mapping appears
at `server_base_di_address`, the second at `server_base_di_address + 1`, and so
on.
