#!/usr/bin/env python3
"""Copy the shared UI kit into an app, or check that the app's copy has not drifted.

    python sync.py <app-frontend-root>          # write <root>/src/kit/**, scripts/kit-check.mjs, kit.lock.json
    python sync.py <app-frontend-root> --check  # exit 1, listing drifted / missing / extra files

The kit is canonical here (shared-infra/ui-kit). Each app holds a generated copy, stamped with a header
that says so, and kit.lock.json with a sha256 per file; the app's `npm run kit:check` (tools/kit-check.mjs,
synced to <root>/scripts/) verifies that lock with Node alone, so it runs in CI and Docker too.

Hashes are taken over LF-normalised text, so a CRLF checkout on Windows is not drift. Stdlib only.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
SOURCE = "shared-infra/ui-kit"
KIT_DIR = "src/kit"
# What is synced, from the kit root: everything under these, plus cn.ts. README, sync.py and tests of the
# sync itself stay here.
TREES = ("css", "theme", "components")
SINGLE = {"cn.ts": f"{KIT_DIR}/cn.ts", "tools/kit-check.mjs": "scripts/kit-check.mjs"}
NOTE = f"GENERATED from {SOURCE} -- edit there, then run sync.py"
HEADERS = {
    ".ts": f"// {NOTE}\n",
    ".tsx": f"// {NOTE}\n",
    ".mjs": f"// {NOTE}\n",
    ".js": f"// {NOTE}\n",
    ".css": f"/* {NOTE} */\n",
}


def lf(text):
    return text.replace("\r\n", "\n")


def sha(text):
    return hashlib.sha256(lf(text).encode("utf-8")).hexdigest()


def planned():
    """{app-relative POSIX path: generated text} for every synced file."""
    out = {}
    pairs = [(KIT / src, dest) for src, dest in SINGLE.items()]
    for tree in TREES:
        for p in sorted((KIT / tree).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                pairs.append((p, f"{KIT_DIR}/{p.relative_to(KIT).as_posix()}"))
    for src, dest in pairs:
        header = HEADERS.get(src.suffix)
        if header is None:
            raise SystemExit(f"sync.py: no header syntax for {src.name}; add its extension to HEADERS")
        body = lf(src.read_text(encoding="utf-8"))
        if body.startswith("#!"):  # keep a shebang first
            first, _, rest = body.partition("\n")
            text = f"{first}\n{header}{rest}"
        else:
            text = header + body
        out[dest] = text
    return out


def lock_text(files):
    return json.dumps({"source": SOURCE, "kit_dir": KIT_DIR, "files": {k: sha(v) for k, v in sorted(files.items())}},
                      indent=2) + "\n"


def existing(root):
    kit = root / KIT_DIR
    return {p.relative_to(root).as_posix() for p in kit.rglob("*") if p.is_file()} if kit.is_dir() else set()


def check(root, files):
    problems = []
    for dest, text in sorted(files.items()):
        p = root / dest
        if not p.is_file():
            problems.append(f"missing: {dest}")
        elif lf(p.read_text(encoding="utf-8")) != text:
            problems.append(f"drifted: {dest}")
    problems += [f"extra:   {x}" for x in sorted(existing(root) - set(files))]
    lock = root / "kit.lock.json"
    if not lock.is_file() or lf(lock.read_text(encoding="utf-8")) != lock_text(files):
        problems.append("stale:   kit.lock.json")
    return problems


def write(root, files):
    for stale in sorted(existing(root) - set(files)):
        (root / stale).unlink()
    for dest, text in files.items():
        p = root / dest
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(root / "kit.lock.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(lock_text(files))
    # Remove folders a removed kit file left empty.
    for d in sorted((root / KIT_DIR).rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", help="the app's frontend root (the folder holding package.json)")
    ap.add_argument("--check", action="store_true", help="report drift instead of writing; exit 1 on any")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "package.json").is_file():
        print(f"sync.py: {root} has no package.json; pass the app's frontend root", file=sys.stderr)
        return 2
    files = planned()
    if args.check:
        problems = check(root, files)
        if problems:
            print(f"ui-kit drift in {root} ({len(problems)}):", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            print(f"Edit the kit in {KIT}, then run: python {Path(__file__).name} {args.root}", file=sys.stderr)
            return 1
        print(f"ui-kit: {len(files)} files in {root} match {SOURCE}.")
        return 0
    write(root, files)
    print(f"ui-kit: synced {len(files)} files into {root} (kit.lock.json updated).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
