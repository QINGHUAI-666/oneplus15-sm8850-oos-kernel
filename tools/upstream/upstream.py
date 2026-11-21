#!/usr/bin/env python3
"""Upstream sync helper for the two third-party features vendored in this tree.

    upstream.py check                        # report drift against pins.json
    upstream.py sync-kernelsu [--ref REF]    # vendor a new KernelSU snapshot
    upstream.py sync-susfs    [--ref REF]    # adopt a new SUSFS revision

Both sync commands are additive-on-top-of-current-tree: they never rewrite
unrelated files, and they stop with a non-zero exit status (and .rej files to
inspect) instead of guessing when a hunk does not apply.

Stdlib only; designed to run inside GitHub Actions on a stock runner.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PINS_PATH = Path(__file__).with_name("pins.json")
GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4/projects"


def pins() -> dict:
    return json.loads(PINS_PATH.read_text())


def save_pins(data: dict) -> None:
    PINS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "op15-upstream-sync"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(GITHUB_API):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "op15-upstream-sync"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"$ {' '.join(map(str, cmd))}\n{p.stdout}\n{p.stderr}")
    return p


# --------------------------------------------------------------------------- check
def ksu_head() -> tuple[str, str]:
    head = http_json(f"{GITHUB_API}/repos/tiann/KernelSU/commits?per_page=1")[0]
    return head["sha"], head["commit"]["committer"]["date"]


def susfs_head(branch: str) -> tuple[str, str]:
    proj = "simonpunk%2Fsusfs4ksu"
    c = http_json(f"{GITLAB_API}/{proj}/repository/commits?ref_name={branch}&per_page=1")[0]
    return c["id"], c["committed_date"]


def cmd_check(args) -> int:
    p = pins()
    drift = False
    rows = []
    sha, when = ksu_head()
    same = sha == p["kernelsu"]["ref"]
    drift |= not same
    rows.append(("kernelsu", p["kernelsu"]["ref"][:12], sha[:12], when[:19], "OK" if same else "DRIFT"))
    sha, when = susfs_head(p["susfs"]["branch"])
    same = sha == p["susfs"]["ref"]
    drift |= not same
    rows.append(("susfs", p["susfs"]["ref"][:12], sha[:12], when[:19], "OK" if same else "DRIFT"))

    print(f"{'feature':10} {'pinned':14} {'upstream':14} {'upstream date':21} status")
    for r in rows:
        print(f"{r[0]:10} {r[1]:14} {r[2]:14} {r[3]:21} {r[4]}")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"drift": drift, "rows": [
                {"feature": r[0], "pinned": r[1], "upstream": r[2], "date": r[3], "status": r[4]}
                for r in rows]}, indent=2))
    return 1 if drift else 0


# ----------------------------------------------------------------- kernelsu sync
KSU_VERSION_RE = re.compile(r"^KSU_VERSION\s*:=\s*(\d+)\s*$", re.M)
KSU_COMMENT_RE = re.compile(r"^# Vendored upstream ([0-9a-f]{40}) has (\d+)$", re.M)


def cmd_sync_kernelsu(args) -> int:
    p = pins()
    ref = args.ref or p["kernelsu"]["ref"]
    target = ROOT / p["kernelsu"]["target"]
    if not target.is_dir():
        raise SystemExit(f"target missing: {target}")

    tmp = Path(tempfile.mkdtemp(prefix="ksu-"))
    print(f"[ksu] fetching {ref} into {tmp}")
    run(["git", "clone", "--filter=blob:none", "--no-checkout", "--quiet",
         "https://github.com/tiann/KernelSU", str(tmp)])
    run(["git", "fetch", "--quiet", "origin", ref], cwd=tmp)
    sha = run(["git", "rev-parse", "FETCH_HEAD"], cwd=tmp).stdout.strip()
    count = int(run(["git", "rev-list", "--count", sha], cwd=tmp).stdout.strip())
    version = 30000 + count
    print(f"[ksu] {sha} -> {count} commits -> KSU_VERSION {version}")

    if args.dry_run:
        print("[ksu] dry-run: not touching the tree")
        return 0

    # 1. replace the vendored tree
    for child in target.iterdir():
        if child.name in {".git"}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    tar_path = tmp / "kernel.tar"
    with open(tar_path, "wb") as fh:
        subprocess.run(["git", "archive", "--format=tar", f"{sha}:kernel"],
                       cwd=tmp, stdout=fh, check=True)
    with tarfile.open(tar_path) as tar:
        tar.extractall(target, filter="data")

    # 2. pinned version marker (upstream derives it from git, we cannot)
    kbuild = ROOT / p["kernelsu"]["version_file"]
    text = kbuild.read_text()
    text = KSU_COMMENT_RE.sub(
        f"# Vendored upstream {sha} has {count}", text, count=1)
    if not KSU_COMMENT_RE.search(text):
        text = (f"# Vendored upstream {sha} has {count}\n"
                f"# ancestors including itself. Preserve its real upstream 30000 + count version\n"
                f"# without depending on a nested .git directory or accessing the network in make.\n"
                + text)
    text = KSU_VERSION_RE.sub(f"KSU_VERSION := {version}", text, count=1)
    kbuild.write_text(text)

    # 3. re-apply the SUSFS -> KernelSU integration patch
    ksu_patch = (ROOT / "tools" / "upstream" / "cache" / "susfs_ksu.patch")
    if ksu_patch.exists():
        rc = run(["patch", "-p1", "--forward", "--batch", "--no-backup-if-mismatch",
                  "-i", str(ksu_patch)], cwd=target, check=False)
        print(rc.stdout[-2000:])
        if rc.returncode != 0:
            print("[ksu] SUSFS/KernelSU integration patch did not apply cleanly", file=sys.stderr)
            return 2

    p["kernelsu"]["ref"] = sha
    p["kernelsu"]["version"] = version
    save_pins(p)
    print(f"[ksu] pinned {sha} (version {version})")
    return 0


# -------------------------------------------------------------------- susfs sync
def cmd_sync_susfs(args) -> int:
    p = pins()
    s = p["susfs"]
    ref = args.ref or s["branch"]
    proj = "simonpunk%2Fsusfs4ksu"
    base = f"{GITLAB_API}/{proj}/repository/files"
    raw = "{base}/{path}/raw?ref={ref}"

    def fetch(path: str) -> bytes:
        return http_bytes(raw.format(base=base, path=urllib.parse.quote(path, safe=""), ref=ref))

    cache = ROOT / "tools" / "upstream" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    kpatch = cache / "susfs_kernel.patch"
    kpatch.write_bytes(fetch(s["kernel_patch"]))
    ksucache = cache / "susfs_ksu.patch"
    ksucache.write_bytes(fetch(s["ksu_patch"]))
    print(f"[susfs] kernel patch {len(kpatch.read_bytes())} B, ksu patch {len(ksucache.read_bytes())} B")

    if args.dry_run:
        print("[susfs] dry-run: patches cached, tree untouched")
        return 0

    # 1. SUSFS-owned files are copied verbatim
    for mapping in s["owned_files"]:
        src, dst = mapping.split("->")
        (ROOT / s["kernel_root"] / dst).write_bytes(fetch(src))
        print(f"[susfs] copied {src} -> {dst}")

    # 2. hook hunks: --forward skips the hunks that are already applied
    rc = run(["patch", "-p1", "--forward", "--batch", "--no-backup-if-mismatch",
              "-i", str(kpatch)], cwd=ROOT / s["kernel_root"], check=False)
    print(rc.stdout[-4000:])
    real = classify_rejects(ROOT / s["kernel_root"], rc.stdout)
    if real:
        write_conflict_report(real, ROOT, "susfs kernel patch")
        return 2

    # 3. KernelSU-side integration
    rc = run(["patch", "-p1", "--forward", "--batch", "--no-backup-if-mismatch",
              "-i", str(ksucache)], cwd=ROOT / s["ksu_root"], check=False)
    print(rc.stdout[-4000:])
    if rc.returncode != 0:
        print("[susfs] KernelSU integration patch did not apply cleanly", file=sys.stderr)
        return 2

    # 4. pins
    head_sha, _ = susfs_head(s["branch"])
    s["ref"] = head_sha
    m = re.search(r'^#define\s+SUSFS_VERSION\s+"([^"]+)"',
                  (ROOT / s["version_file"]).read_text(), re.M)
    if m:
        s["version"] = m.group(1)
    save_pins(p)
    print(f"[susfs] pinned {head_sha} ({s['version']})")
    return 0


def classify_rejects(tree: Path, stdout: str) -> list[Path]:
    """Drop .rej files that only record already-applied hunks; return the real ones.

    `patch --forward` reports "Reversed (or previously applied) patch detected!"
    for hunks that are already in the tree, but still writes a .rej file. Those
    are benign: the file content is exactly the patch post-image.
    """
    real = []
    for rej in tree.rglob("*.rej"):
        target = Path(str(rej)[:-4])
        if target.exists():
            content = target.read_text(errors="replace")
            added = [l[1:].strip() for l in rej.read_text(errors="replace").splitlines()
                     if l.startswith("+") and not l.startswith("+++")]
            added = [l for l in added if l]
            # every post-image line already present => hunk was already applied
            if added and all(line in content for line in added):
                rej.unlink()
                continue
        real.append(rej)
    return real


def write_conflict_report(rejects: list[Path], root: Path, what: str) -> None:
    out = root / "tools" / "upstream" / "report.md"
    lines = [f"# {what}: conflicts", "",
             f"{len(rejects)} hunk file(s) did not apply cleanly.", ""]
    for rej in rejects:
        lines.append(f"## `{rej.relative_to(root)}`")
        lines.append("```diff")
        lines += rej.read_text(errors="replace").splitlines()[:120]
        lines.append("```")
        lines.append("")
    out.write_text("\n".join(lines))
    print(f"[sync] conflicts written to {out.relative_to(root)}", file=sys.stderr)
    for rej in rejects:
        print(f"  CONFLICT {rej.relative_to(root)}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="report drift against pins.json")
    c.add_argument("--json", help="write a machine-readable report here")
    c.set_defaults(func=cmd_check)

    k = sub.add_parser("sync-kernelsu", help="vendor a new KernelSU snapshot")
    k.add_argument("--ref", help="commit/tag (default: pinned ref)")
    k.add_argument("--dry-run", action="store_true")
    k.set_defaults(func=cmd_sync_kernelsu)

    s = sub.add_parser("sync-susfs", help="adopt a new SUSFS revision")
    s.add_argument("--ref", help="branch/commit (default: pinned branch)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sync_susfs)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
