# unit_based_voltage_sweep (v0.8.3)

`ExperimentPlan.unit` の最小例。`bindings` を明示する代わりに、
`unit: "unit001"` を指定し、`$psu` / `$dmm` という role 参照だけで
DSL を書く。

## 前提 `_system.yaml`

```yaml
experiment_units:
  unit001:
    psu: psu001     # alias
    dmm: dmm001
    temp: temp001
```

`unit001` に `psu` / `dmm` が含まれていれば、Plan で `$psu` / `$dmm`
が解決される。`temp` は使わなくてもよい (未参照なら scheduler が
lock しない)。

## 解決順序

1. `unit_bindings` (`unit001.psu = psu001`, `unit001.dmm = dmm001`)
2. `explicit_bindings` で同 role を override (今回は空)
3. alias → raw resource (`psu001` → `_system.yaml.instruments.psu001.resource`)

## dry-run の `summary.unit_resolution`

```json
{
  "unit": "unit001",
  "unit_bindings": {"psu": "psu001", "dmm": "dmm001", "temp": "temp001"},
  "explicit_bindings": {},
  "effective_bindings": {"psu": "psu001", "dmm": "dmm001", "temp": "temp001"},
  "overridden_roles": []
}
```

## 一部 role だけ差し替えたい時

```json
{
  "unit": "unit001",
  "bindings": { "dmm": "dmm_backup" },
  "steps": [...]
}
```

→ `overridden_roles: ["dmm"]` として記録され、`effective_bindings.dmm`
が `dmm_backup` になる。
