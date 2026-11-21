# PLK110 zero-build contract tools

These tools verify a kernel `Image` against the module contract of the phone's **own stock
firmware**, without `repo sync`, without a compiler and without building anything.

They were used to qualify `plk110-17.0.1.201-base`; they are the regression gate for every future
build.

## Inputs

* A kernel `Image` (raw ARM64 Image, e.g. extracted from `boot.img` at page offset 4096).
* `stock_module_oracle.json` — the contract of `PLK110_17.0.1.201`: 1,069 stock modules →
  6,681 `MODVERSIONS` records → 4,267 symbols that must be provided by vmlinux.
* `stock_identity.json` — firmware identity baseline (release strings, AVB properties, sizes,
  GKI module-signing certificate fingerprint).

## Recipe

```sh
# 1. (once) unpack the stock module partitions from the OTA
python3 payload_tool.py payload.bin extract boot vendor_boot vendor_dlkm system_dlkm
python3 erofs_extract.py parts/system_dlkm.img system_dlkm_root      # -> 103 modules

# 2. verdict for any candidate Image
python3 verify_image.py Image stock_module_oracle.json
```

`verify_image.py` prints exported-symbol count, matched / missing / CRC-mismatched counts for the
4,267 required symbols and a final `PASS` / `FAIL`.

## How the CRC table is recovered from a stripped Image

`include/asm-generic/vmlinux.lds.h` emits both tables with `SORT()`:

```text
__ksymtab      (N1 x 12-byte PREL32)   __ksymtab_gpl (N2 x 12-byte PREL32)
__kcrctab      (N1 x u32)              __kcrctab_gpl (N2 x u32)
__ksymtab_strings
```

Because both are sorted by symbol name, the scan finds the dense 12-byte PREL32 array, splits it
where the sort order breaks (normal vs GPL), locates the CRC arrays by matching adjacent
`(crc_i, crc_i+1)` pairs against the expected values, and then pairs by index. The result is
self-validating: on the stock Image all 4,267 required symbols match with zero mismatches.

## Reference results

| Image | exported | matched | missing | CRC mismatch | verdict |
|---|---|---|---|---|---|
| stock `6.12.69-android16-6-g5fd39602326e-ab16372235-4k` | 9,933 | 4,267 | 0 | 0 | PASS |
| upstream `6.12.117-android16-6-o-g9218fe3f104b-4k` | 10,172 | 4,267 | 0 | 0 | PASS |

## Limits

`MODVERSIONS` only covers exported symbol CRCs. Private struct layout / inline ABI is not covered;
for that use the upstream `tools/audit-retained-stock-module-contract.py` with a real
`Module.symvers` from a completed build.
