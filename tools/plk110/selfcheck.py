#!/usr/bin/env python3
"""Static self-check for the PLK110 adaptation and the vendored feature pins.

Fast, dependency-free, runs on any GitHub-hosted runner. Covers everything the
build workflow cannot cheaply re-verify on every push.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAILS: list[str] = []


def check(cond: bool, what: str, detail: str = "") -> None:
    if cond:
        print(f"  ok   {what}")
    else:
        FAILS.append(what)
        print(f"  FAIL {what}" + (f"  ({detail})" if detail else ""))


def der_sha256_of_pem(pem: str) -> str:
    p = subprocess.run(["openssl", "x509", "-outform", "DER"], input=pem.encode(),
                       capture_output=True)
    if p.returncode != 0:
        return "openssl-failed"
    return hashlib.sha256(p.stdout).hexdigest()


def main() -> int:
    ident = json.loads((Path(__file__).with_name("stock_identity.json")).read_text())
    oracle = json.loads((Path(__file__).with_name("stock_module_oracle.json")).read_text())
    pins = json.loads((ROOT / "tools/upstream/pins.json").read_text())

    print("[1] firmware identity anchors")
    bzl = (ROOT / "kernel_platform/soc-repo/kleaf-scripts/android_build.bzl").read_text()
    check(f'fingerprint:{ident["avb_boot_fingerprint"]}' in bzl,
          "android_build.bzl carries the PLK110 AVB fingerprint",
          ident["avb_boot_fingerprint"])
    check(f'security_patch:{ident["avb_boot_security_patch"]}' in bzl,
          "android_build.bzl carries 2026-11-01 security patch")
    check("com.android.build.boot.os_version:16" in bzl,
          "boot os_version stays 16")

    print("[2] GKI module-signing trust anchor")
    cfg = (ROOT / "kernel_platform/common/arch/arm64/configs/gki_defconfig").read_text()
    m = re.search(r'^CONFIG_SYSTEM_TRUSTED_KEYS="([^"]+)"', cfg, re.M)
    check(bool(m), "CONFIG_SYSTEM_TRUSTED_KEYS is set")
    if m:
        cert = ROOT / "kernel_platform/common" / m.group(1)
        check(cert.is_file(), "certificate file exists", str(cert))
        if cert.is_file() and len(sys.argv) > 1 and sys.argv[1] == "--with-openssl":
            got = der_sha256_of_pem(cert.read_text())
            check(got == ident["stock_gki_module_signing_cert_sha256_der"],
                  "certificate DER matches the stock Image", got[:16])

    print("[3] stock module oracle")
    check(len(oracle["vmlinux_required"]) == 4267,
          "oracle holds 4,267 required symbols", str(len(oracle["vmlinux_required"])))
    check(oracle["counts"] == {"system_dlkm": 103, "vendor_dlkm": 461, "vendor_boot": 505},
          "oracle covers 103/461/505 modules", str(oracle["counts"]))

    print("[4] vendored feature pins")
    kbuild = (ROOT / pins["kernelsu"]["version_file"]).read_text()
    m = re.search(r"^KSU_VERSION\s*:=\s*(\d+)\s*$", kbuild, re.M)
    check(bool(m) and int(m.group(1)) == pins["kernelsu"]["version"],
          "KernelSU Kbuild version matches pins.json",
          f"{m.group(1) if m else 'absent'} vs {pins['kernelsu']['version']}")
    check(pins["kernelsu"]["ref"] in kbuild,
          "KernelSU vendored commit is recorded in Kbuild")
    susfs = (ROOT / pins["susfs"]["version_file"]).read_text()
    m = re.search(r'^#define\s+SUSFS_VERSION\s+"([^"]+)"', susfs, re.M)
    check(bool(m) and m.group(1) == pins["susfs"]["version"],
          "SUSFS version matches pins.json",
          f"{m.group(1) if m else 'absent'} vs {pins['susfs']['version']}")

    print("[5] tooling syntax")
    for py in sorted((ROOT / "tools").rglob("*.py")):
        try:
            compile(py.read_text(), str(py), "exec")
        except SyntaxError as exc:
            check(False, f"{py.relative_to(ROOT)} compiles", str(exc))
    check(not any(f.startswith("tooling") for f in FAILS), "all python tooling compiles")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s)")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
