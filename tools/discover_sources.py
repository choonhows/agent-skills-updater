#!/usr/bin/env python3
"""Bootstrap sources/tracked.json for an existing skills collection.

Clones each upstream repository you name, finds every SKILL.md in it, and maps
your local skills/<name>/ folders to the upstream folder that matches them best
(same folder or frontmatter name, fewest differing files). Skills claimed by more
than one upstream are reported so you can pick one; local skills no upstream
claims go in "untracked".

  tools/discover_sources.py OWNER/REPO [OWNER/REPO ...] [--root DIR] [--write]

Without --write it prints the result. Review it before the first update run:
set "mode": "report" on sources whose skills you adapted locally, and
"report_new_skills": true on bundles where you want to hear about new skills.
Standard library only; needs git on PATH.
"""
import argparse
import filecmp
import json
import os
import re
import subprocess
import sys
import tempfile


def fm_name(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return None
    m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", head, re.M)
    return m.group(1).strip() if m else None


def ndiff(a, b):
    c = filecmp.dircmp(a, b, ignore=[".git", "node_modules", ".DS_Store"])
    n = len(c.left_only) + len(c.right_only) + len(c.diff_files) + len(c.funny_files)
    for sub in c.common_dirs:
        n += ndiff(os.path.join(a, sub), os.path.join(b, sub))
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repos", nargs="+", help="upstream repositories as OWNER/REPO")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="skills repository root (default: parent of tools/)")
    ap.add_argument("--write", action="store_true", help="write sources/tracked.json")
    args = ap.parse_args()

    skills_dir = os.path.join(args.root, "skills")
    local = sorted(n for n in os.listdir(skills_dir) if os.path.isfile(os.path.join(skills_dir, n, "SKILL.md")))
    claims = {}
    sources = []

    with tempfile.TemporaryDirectory(prefix="skill-discover-") as work:
        for repo in args.repos:
            dest = os.path.join(work, repo.replace("/", "__"))
            r = subprocess.run(["git", "clone", "--quiet", "--depth", "1", f"https://github.com/{repo}.git", dest])
            if r.returncode:
                print(f"skip {repo}: clone failed", file=sys.stderr)
                continue
            head = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            branch = subprocess.run(["git", "-C", dest, "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
            cands = {}
            for dirpath, dirnames, filenames in os.walk(dest):
                dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
                if "SKILL.md" in filenames:
                    rel = os.path.relpath(dirpath, dest)
                    for n in {os.path.basename(dirpath), fm_name(os.path.join(dirpath, "SKILL.md"))}:
                        if n:
                            cands.setdefault(n, []).append(rel)
            mapping = {}
            for name in local:
                paths = cands.get(name)
                if not paths:
                    continue
                best = min(paths, key=lambda p: (ndiff(os.path.join(skills_dir, name), os.path.join(dest, p)), len(p)))
                mapping[name] = best
                claims.setdefault(name, []).append(repo)
            if mapping:
                sources.append({"repository": repo, "branch": branch, "pinned_commit": head, "mode": "auto",
                                "report_new_skills": False, "skills": dict(sorted(mapping.items()))})
            print(f"{repo}: {len(mapping)} local skills matched", file=sys.stderr)

    conflicts = {n: r for n, r in claims.items() if len(r) > 1}
    if conflicts:
        print("claimed by more than one upstream (keep one, delete the others):", file=sys.stderr)
        for n, r in sorted(conflicts.items()):
            print(f"  {n}: {', '.join(r)}", file=sys.stderr)
    out = {"sources": sources, "untracked": [n for n in local if n not in claims]}
    text = json.dumps(out, indent=2) + "\n"
    if args.write:
        path = os.path.join(args.root, "sources", "tracked.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        print(f"wrote {path}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
