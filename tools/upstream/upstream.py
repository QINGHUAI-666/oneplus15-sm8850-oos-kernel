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


def fetch_susfs_ksu_patch(p: dict, ref: str | None = None, name: str = "susfs_ksu.patch"):
    """Download the SUSFS -> KernelSU patch at a given SUSFS ref (default: pin)."""
    s = p["susfs"]
    cache = ROOT / "tools" / "upstream" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / name
    url = (f"{GITLAB_API}/simonpunk%2Fsusfs4ksu/repository/files/"
           f"{urllib.parse.quote(s['ksu_patch'], safe='')}/raw?ref={ref or s['ref']}")
    try:
        out.write_bytes(http_bytes(url))
    except Exception as exc:  # noqa: BLE001 - network failures must be visible
        print(f"[susfs] cannot fetch {s['ksu_patch']}: {exc}", file=sys.stderr)
        return None
    return out


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


def export_kernel(tmp: Path, ref: str, dest: Path) -> None:
    """Extract <ref>:kernel from the upstream clone into dest (mirroring)."""
    dest.mkdir(parents=True, exist_ok=True)
    tar_path = tmp / f"kernel-{ref[:12]}.tar"
    with open(tar_path, "wb") as fh:
        subprocess.run(["git", "archive", "--format=tar", f"{ref}:kernel"],
                       cwd=tmp, stdout=fh, check=True)
    with tarfile.open(tar_path) as tar:
        def _safe(member, target):
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise SystemExit(f"refusing unsafe tar member: {member.name}")
            return member
        tar.extractall(dest, filter=_safe)


def pathname(p: Path) -> str:
    return p.name


def read_tree(root: Path) -> dict[str, bytes]:
    out = {}
    if not root.exists():
        return out
    for f in root.rglob("*"):
        if f.is_file() and not f.is_symlink():
            out[f.relative_to(root).as_posix()] = f.read_bytes()
    return out


def patch_paths(patch_file: Path, strip: str) -> set[str]:
    """Paths the patch touches, relative to the tree the patch is applied in."""
    out = set()
    for line in patch_file.read_text(errors="replace").splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            parts = path.split("/")
            if len(parts) > int(strip):
                out.add("/".join(parts[int(strip):]))
    return out


def merge_three_way(base: Path, ours: Path, theirs: Path,
                    force_theirs: set[str] | None = None) -> list[str]:
    force_theirs = force_theirs or set()
    """Apply (theirs - base) onto ours, file by file. Returns conflicted paths."""
    b, o, t = read_tree(base), read_tree(ours), read_tree(theirs)
    conflicts = []
    for rel in sorted(set(b) | set(o) | set(t)):
        bv, ov, tv = b.get(rel), o.get(rel), t.get(rel)
        if rel in force_theirs and tv is not None:
            target = ours / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tv)
            continue
        if tv == bv:                      # upstream untouched -> keep local
            continue
        target = ours / rel
        if ov == bv:                      # local untouched -> take upstream
            if tv is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tv)
            continue
        # both sides changed: 3-way file merge
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "base").write_bytes(bv or b"")
            (td / "ours").write_bytes(ov or b"")
            (td / "theirs").write_bytes(tv or b"")
            res = subprocess.run(["git", "merge-file", "-p",
                                  str(td / "ours"), str(td / "base"), str(td / "theirs")],
                                 capture_output=True)
            if res.returncode != 0:
                conflicts.append(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(res.stdout)
    return conflicts


def cmd_sync_kernelsu(args) -> int:
    """Vendor merge: base = pinned upstream, theirs = new upstream, ours = tree.

    Replacing the directory outright would drop local adaptations (SUSFS hooks,
    KABI fixes, the pinned version block), so the upstream delta is merged in.
    """
    p = pins()
    old_ref = p["kernelsu"]["ref"]
    new_ref = args.ref or old_ref
    target = ROOT / p["kernelsu"]["target"]
    if not target.is_dir():
        raise SystemExit(f"target missing: {target}")

    tmp = Path(tempfile.mkdtemp(prefix="ksu-"))
    print(f"[ksu] fetching {old_ref[:12]} -> {new_ref[:12]}")
    run(["git", "clone", "--filter=blob:none", "--no-checkout", "--quiet",
         "https://github.com/tiann/KernelSU", str(tmp / "src")])
    src = tmp / "src"
    run(["git", "fetch", "--quiet", "origin", old_ref, new_ref], cwd=src)
    old_sha = run(["git", "rev-parse", f"{old_ref}^{{commit}}"], cwd=src).stdout.strip()
    new_sha = run(["git", "rev-parse", f"{new_ref}^{{commit}}"], cwd=src).stdout.strip()
    count = int(run(["git", "rev-list", "--count", new_sha], cwd=src).stdout.strip())
    version = 30000 + count
    print(f"[ksu] {old_sha[:12]} -> {new_sha[:12]} ({count} commits, version {version})")

    if args.dry_run:
        print("[ksu] dry-run: nothing written")
        return 0

    base_src, new_src = tmp / "base", tmp / "new"
    export_kernel(src, old_sha, base_src)
    export_kernel(src, new_sha, new_src)

    # Both sides must be "SUSFS-shaped" before merging, otherwise the SUSFS
    # hooks in our tree look like local edits and collide with every upstream
    # touch of the same lines.
    old_patch = fetch_susfs_ksu_patch(p, name="susfs_ksu_pinned.patch")
    new_patch = fetch_susfs_ksu_patch(p, ref=(args.susfs_ref or p["susfs"]["branch"]),
                                      name="susfs_ksu_target.patch")
    if old_patch is None or new_patch is None:
        print("[ksu] cannot fetch the SUSFS KernelSU patch; refusing", file=sys.stderr)
        return 3
    for label, d, patch_file in (("base", base_src, old_patch),
                                 ("new", new_src, new_patch)):
        strip = "2" if b"+++ b/kernel/" in patch_file.read_bytes() else "1"
        rc = run(["patch", f"-p{strip}", "--forward", "--batch",
                  "--no-backup-if-mismatch", "-i", str(patch_file)], cwd=d, check=False)
        if rc.returncode != 0:
            print(f"[ksu] warning: SUSFS patch did not apply cleanly to {label} tree "
                  f"({pathname(patch_file)})", file=sys.stderr)
        for rej in d.rglob("*.rej"):
            rej.unlink()

    # files owned by the SUSFS patch have no local content worth merging
    strip_new = "2" if b"+++ b/kernel/" in new_patch.read_bytes() else "1"
    owned = patch_paths(new_patch, strip_new)
    conflicts = merge_three_way(base_src, target, new_src, force_theirs=owned)
    if conflicts:
        report = ROOT / "tools" / "upstream" / "report.md"
        report.write_text("# KernelSU vendor merge: conflicts\n\n" +
                          "\n".join(f"- `{c}`" for c in conflicts) + "\n")
        print(f"[ksu] {len(conflicts)} conflicted file(s):", file=sys.stderr)
        for c in conflicts:
            print(f"  CONFLICT {c}", file=sys.stderr)
        return 2

    # pinned version marker (upstream derives it from git, we cannot)
    kbuild = ROOT / p["kernelsu"]["version_file"]
    text = kbuild.read_text()
    marker = (f"# Vendored upstream {new_sha} has {count}\n"
              f"# ancestors including itself. Preserve its real upstream 30000 + count version\n"
              f"# without depending on a nested .git directory or accessing the network in make.\n"
              f"KSU_VERSION := {version}")
    if re.search(r"^# Vendored upstream", text, re.M):
        text = re.sub(r"^# Vendored upstream(?:.*\n)*?^KSU_VERSION\s*:=\s*\d+\s*$",
                      marker, text, count=1, flags=re.M)
    elif re.search(r"^KSU_VERSION\s*:=\s*\d+\s*$", text, re.M):
        text = re.sub(r"^KSU_VERSION\s*:=\s*\d+\s*$", marker, text, count=1, flags=re.M)
    else:
        text = marker + "\n" + text
    kbuild.write_text(text)

    if "KSU_SUSFS" not in (target / "Kconfig").read_text():
        print("[ksu] Kconfig lost the SUSFS options; aborting", file=sys.stderr)
        return 3

    p["kernelsu"]["ref"] = new_sha
    p["kernelsu"]["version"] = version
    save_pins(p)
    print(f"[ksu] pinned {new_sha[:12]} (version {version})")
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


def cmd_compat(args) -> int:
    """Can the SUSFS KernelSU patch be applied to a given KernelSU revision?

    SUSFS ships 10_enable_susfs_for_ksu.patch written against one particular
    KernelSU tree; the two upstreams must be updated as a pair. This command
    answers that question instead of guessing.
    """
    p = pins()
    s = p["susfs"]
    cache = ROOT / "tools" / "upstream" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    patch_path = cache / "susfs_ksu.patch"
    url = (f"{GITLAB_API}/simonpunk%2Fsusfs4ksu/repository/files/"
           f"{urllib.parse.quote(s['ksu_patch'], safe='')}/raw?ref={args.susfs_ref or s['branch']}")
    patch_path.write_bytes(http_bytes(url))
    strip = "2" if b"+++ b/kernel/" in patch_path.read_bytes() else "1"

    ksu_sha_head = http_json(f"{GITHUB_API}/repos/tiann/KernelSU/commits?per_page=1")[0]["sha"]
    refs = {"pinned": p["kernelsu"]["ref"], "upstream-head": ksu_sha_head}
    if args.ksu_ref:
        refs = {"requested": args.ksu_ref, **refs}

    tmp = Path(tempfile.mkdtemp(prefix="compat-"))
    run(["git", "clone", "--filter=blob:none", "--no-checkout", "--quiet",
         "https://github.com/tiann/KernelSU", str(tmp / "src")])
    src = tmp / "src"
    print(f"{'kernelsu ref':>16} {'sha':>12}  susfs patch result")
    bad = 0
    for label, ref in refs.items():
        run(["git", "fetch", "--quiet", "origin", ref], cwd=src)
        sha = run(["git", "rev-parse", "FETCH_HEAD"], cwd=src).stdout.strip()
        dest = tmp / label.replace("/", "_")
        export_kernel(src, sha, dest)
        rc = run(["patch", f"-p{strip}", "--dry-run", "--batch",
                  "-i", str(patch_path)], cwd=dest, check=False)
        failed = rc.stdout.count("FAILED")
        status = "clean" if rc.returncode == 0 else f"{failed} hunk(s) FAILED"
        bad += rc.returncode != 0
        print(f"{label:>16} {sha[:12]:>12}  {status}")
    print()
    print("verdict:", "pair is compatible" if bad == 0 else
          "upstream pair NOT aligned - do not sync single features independently")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="report drift against pins.json")
    c.add_argument("--json", help="write a machine-readable report here")
    c.set_defaults(func=cmd_check)

    k = sub.add_parser("sync-kernelsu", help="vendor a new KernelSU snapshot")
    k.add_argument("--ref", help="commit/tag (default: pinned ref)")
    k.add_argument("--susfs-ref", help="SUSFS ref whose KernelSU patch is applied (default: upstream branch head)")
    k.add_argument("--dry-run", action="store_true")
    k.set_defaults(func=cmd_sync_kernelsu)

    c2 = sub.add_parser("compat", help="check the SUSFS/KernelSU upstream pair")
    c2.add_argument("--susfs-ref", help="SUSFS ref to test (default: pinned branch)")
    c2.add_argument("--ksu-ref", help="extra KernelSU ref to test")
    c2.set_defaults(func=cmd_compat)

    s = sub.add_parser("sync-susfs", help="adopt a new SUSFS revision")
    s.add_argument("--ref", help="branch/commit (default: pinned branch)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_sync_susfs)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
