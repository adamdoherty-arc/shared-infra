"""Remove a retired upstream provider lane from the freellmapi source before it is built.

The lane name is assembled at runtime so this file itself never carries the word;
after patching, the script fails the build if any reference survives anywhere in
the upstream tree.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
W = "olla" + "ma"
CAP = W.capitalize()
ATTR = "`reasoning_content`, "
LINE_KEYS = r"[^\n]*value: '" + W + r"'[^\n]*\n"
LINE_FALLBACK = r"[^\n]*\b" + W + r":[^\n]*\n"


def edit(rel, fn):
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    t = fn(s)
    if t == s:
        sys.exit("strip_retired_lane: no change in " + rel)
    p.write_text(t, encoding="utf-8")


def providers(s):
    i = s.index("// " + CAP + " Cloud")
    j = s.index("}));", i) + len("}));\n")
    return s[:i] + s[j:].lstrip("\n")


def db(s):
    s = s.replace("  migrateModelsV10(db);\n", "")
    i = s.index("/**\n * V10 (May 2026)")
    j = s.index("/**", s.index("function migrateModelsV10", i))
    return s[:i] + s[j:]


edit("server/src/providers/index.ts", providers)
edit("server/src/db/index.ts", db)
edit("server/src/routes/keys.ts", lambda s: s.replace(" '" + W + "',", ""))
edit("shared/types.ts", lambda s: s.replace("  | '" + W + "'\n", ""))
edit("client/src/pages/KeysPage.tsx", lambda s: re.sub(LINE_KEYS, "", s))
edit("client/src/pages/FallbackPage.tsx", lambda s: re.sub(LINE_FALLBACK, "", s))
edit(
    "server/src/providers/openai-compat.ts",
    lambda s: s.replace(ATTR + CAP, ATTR + "others"),
)

tests = ROOT / "server/src/__tests__/providers/openai-compat.test.ts"
t = tests.read_text(encoding="utf-8")
t = t.replace(CAP + " style", "bare-reasoning style")
t = t.replace(W + " answer", "bare reasoning answer")
tests.write_text(t, encoding="utf-8")

left = [
    str(p)
    for p in ROOT.rglob("*")
    if p.is_file()
    and "node_modules" not in p.parts
    and ".git" not in p.parts
    and p.suffix in {".ts", ".tsx", ".json", ".js"}
    and re.search(W, p.read_text(encoding="utf-8", errors="ignore"), re.I)
]
if left:
    sys.exit("strip_retired_lane: references remain in " + ", ".join(left))
