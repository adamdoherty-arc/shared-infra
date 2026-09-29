"""Print the topic files whose working-tree diff is confined to the docs_refresh auto-generated section.

docs_refresh.py rewrites everything below the `<!-- auto -->` marker of .claude/memory/topics/*.md and never
commits. A file qualifies only when its HEAD version contains the marker and the text ABOVE the marker is
byte-identical in the working tree (anything else is somebody's hand edit and is left alone).
This helper would pass trivially if it accepted files with no marker in HEAD; it requires the marker there.
"""
import subprocess
import sys

MARKER = "<!-- auto -->"
ROOT = "C:/code/ADA"


def git(*a):
    return subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False).stdout


def norm(t):
    return t.replace("\r\n", "\n")


def main():
    for line in git("status", "--porcelain", "--", ".claude/memory/topics").split("\n"):
        if not line or line[:2].strip() != "M":
            continue
        path = line[3:].strip().strip('"')
        if not path.endswith(".md"):
            continue
        head = norm(git("show", f"HEAD:{path}"))
        marker_in_head = MARKER in head
        try:
            work = norm(open(f"{ROOT}/{path}", encoding="utf-8", errors="replace", newline="").read())
        except OSError:
            continue
        if MARKER not in work:
            continue
        above_head = head.split(MARKER, 1)[0]
        above_work = work.split(MARKER, 1)[0]
        if marker_in_head and above_head == above_work:
            print(path)
        elif not marker_in_head and above_work.rstrip() == head.rstrip():
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
