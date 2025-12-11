# modbus-io-bridge

Bridge Modbus/TCP inputs to outputs with optional debounce, inversion, and
basic boolean logic between multiple inputs.

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
