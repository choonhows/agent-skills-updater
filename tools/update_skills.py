#!/usr/bin/env python3
"""Refresh vendored skills from their upstream repositories.

Reads sources/tracked.json and sources/removed-skills.json, fetches each upstream
at the head of its branch, and for every tracked skill compares the upstream folder
with skills/<name>/.

  mode "auto"    differing skills are replaced with the upstream copy
  mode "report"  differences are only listed in the summary

Skills listed in removed-skills.json are never refreshed or re-added. New upstream
skills are reported, never added. Symlinks in upstream folders are not copied.

Usage:
  tools/update_skills.py [--summary FILE] [--only OWNER/REPO ...] [--dry-run]

Writes a Markdown summary (for a pull request body) and prints `changed=true|false`.
Standard library only; needs git on PATH.
"""
import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")
TRACKED = os.path.join(ROOT, "sources", "tracked.json")
REMOVED = os.path.join(ROOT, "sources", "removed-skills.json")

SCRIPT_EXT = {".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts", ".mts", ".py", ".rb",
              ".pl", ".ps1", ".cmd", ".bat", ".exe", ".bin", ".so", ".dylib", ".wasm"}
SKIP_NAMES = {".git", "node_modules", ".DS_Store"}


def run(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def list_files(root, exclude=()):
    """Relative paths of regular files under root, minus excluded paths and symlinks."""
    files, links = set(), []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            rel = os.path.normpath(os.path.join(rel_dir, d))
            if d in SKIP_NAMES or rel in exclude:
                continue
            if os.path.islink(full):
                links.append(rel)
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.normpath(os.path.join(rel_dir, f))
            if f in SKIP_NAMES or rel in exclude:
                continue
            if os.path.islink(full):
                links.append(rel)
                continue
            files.add(rel)
    return files, links


def compare(local, upstream, exclude):
    lf = list_files(local)[0] if os.path.isdir(local) else set()
    uf, links = list_files(upstream, exclude)
    added = sorted(uf - lf)
    removed = sorted(lf - uf)
    modified = sorted(f for f in lf & uf
                      if not filecmp.cmp(os.path.join(local, f), os.path.join(upstream, f), shallow=False))
    return added, removed, modified, links


def is_script(path, base):
    ext = os.path.splitext(path)[1].lower()
    full = os.path.join(base, path)
    if ext in SCRIPT_EXT:
        return True
    try:
        if os.access(full, os.X_OK) and not os.path.isdir(full):
            return True
        with open(full, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def replace_skill(local, upstream, exclude):
    """Replace local with a copy of upstream (regular files only)."""
    files, _ = list_files(upstream, exclude)
    tmp = local + ".updating"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    for rel in sorted(files):
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(upstream, rel), dst)
    if os.path.exists(local):
        shutil.rmtree(local)
    os.rename(tmp, local)


def skill_dirs(repo_dir):
    found = {}
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        if "SKILL.md" in filenames:
            rel = os.path.relpath(dirpath, repo_dir)
            found.setdefault(os.path.basename(dirpath) if rel != "." else os.path.basename(repo_dir), rel)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", help="write a Markdown summary to this file")
    ap.add_argument("--only", nargs="*", help="limit to these OWNER/REPO sources")
    ap.add_argument("--dry-run", action="store_true", help="compare only; change nothing")
    args = ap.parse_args()

    tracked = json.load(open(TRACKED))
    removed = ({s["name"] for s in json.load(open(REMOVED)).get("skills", [])}
               if os.path.exists(REMOVED) else set())
    all_tracked = {n for s in tracked["sources"] for n in s["skills"]}
    local_names = set(os.listdir(SKILLS))

    sections, flagged, new_upstream, problems = [], [], [], []
    changed_any = False

    with tempfile.TemporaryDirectory(prefix="skill-update-") as work:
        for src in tracked["sources"]:
            repo = src["repository"]
            if args.only and repo not in args.only:
                continue
            dest = os.path.join(work, repo.replace("/", "__"))
            try:
                run("git", "clone", "--quiet", "--depth", "1", "--branch", src["branch"],
                    f"https://github.com/{repo}.git", dest)
                head = run("git", "rev-parse", "HEAD", cwd=dest)
            except subprocess.CalledProcessError as e:
                problems.append(f"`{repo}`: fetch failed ({(e.stderr or '').strip()[:200]})")
                continue

            exclude = {os.path.normpath(p) for p in src.get("exclude", [])}
            rows, all_current = [], True
            for name, upath in src["skills"].items():
                if name in removed:
                    continue
                up = os.path.normpath(os.path.join(dest, upath))
                if not os.path.isfile(os.path.join(up, "SKILL.md")):
                    problems.append(f"`{name}`: `{upath}` no longer exists in `{repo}`")
                    all_current = False
                    continue
                local = os.path.join(SKILLS, name)
                added, gone, modified, links = compare(local, up, exclude)
                if links:
                    problems.append(f"`{name}`: skipped {len(links)} symlink(s) from upstream: "
                                    + ", ".join(f"`{l}`" for l in links[:5]))
                if not (added or gone or modified):
                    continue
                scripts = [f for f in added + modified if is_script(f, up)]
                if src["mode"] == "auto" and not args.dry_run:
                    replace_skill(local, up, exclude)
                    changed_any = True
                    action = "updated"
                else:
                    action = "needs review" if src["mode"] == "report" else "would update"
                    all_current = False
                rows.append(f"| `{name}` | {action} | {len(added)} | {len(modified)} | {len(gone)} |")
                for f in scripts:
                    flagged.append(f"`{name}/{f}`" + (" (new)" if f in added else ""))

            if src.get("report_new_skills"):
                mapped = {os.path.normpath(p) for p in src["skills"].values()}
                for n, rel in sorted(skill_dirs(dest).items()):
                    if os.path.normpath(rel) in mapped or rel.split(os.sep)[0] in ("docs", "tests", "examples"):
                        continue
                    if n not in all_tracked and n not in removed and n not in local_names:
                        new_upstream.append(f"`{n}` in `{repo}` (`{rel}`)")

            old = src.get("pinned_commit")
            if rows or (old != head and all_current):
                link = (f"https://github.com/{repo}/compare/{old}...{head}" if old
                        else f"https://github.com/{repo}/tree/{head}")
                note = f"\n\n> {src['note']}" if src.get("note") and src["mode"] == "report" else ""
                body = (f"### [{repo}]({link})\n\n`{(old or 'unpinned')[:10]}` → `{head[:10]}` "
                        f"({src['mode']}){note}\n")
                if rows:
                    body += ("\n| Skill | Action | Added | Modified | Removed |\n| --- | --- | --- | --- | --- |\n"
                             + "\n".join(rows) + "\n")
                sections.append(body)
            if all_current and not args.dry_run and src["mode"] == "auto" and old != head:
                src["pinned_commit"] = head
                changed_any = True

    if changed_any and not args.dry_run:
        with open(TRACKED, "w") as fh:
            json.dump(tracked, fh, indent=2)
            fh.write("\n")

    out = ["## Skill updates", ""]
    if not sections and not new_upstream and not problems:
        out.append("Every tracked skill matches its upstream.")
    if flagged:
        out += ["### Review first: new or changed scripts", "",
                "These files can run code on your machine. Read them before merging.", ""]
        out += [f"- {f}" for f in flagged] + [""]
    out += sections
    if new_upstream:
        out += ["### New upstream skills (not added)", "",
                "Add one by mapping it in `sources/tracked.json` and copying it in.", ""]
        out += [f"- {n}" for n in new_upstream] + [""]
    if problems:
        out += ["### Problems", ""] + [f"- {p}" for p in problems] + [""]
    text = "\n".join(out).rstrip() + "\n"
    if args.summary:
        with open(args.summary, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    print(f"changed={'true' if changed_any else 'false'}")


if __name__ == "__main__":
    main()
