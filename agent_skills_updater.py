#!/usr/bin/env python3
"""agent-skills-updater: keep the agent skills you have installed up to date.

Finds every skill folder (a folder with SKILL.md) in the usual install locations,
works out which GitHub repository each one came from, and updates them from there.

  agent-skills-updater scan                 list installed skills and where they come from
  agent-skills-updater check                show which skills have upstream updates
  agent-skills-updater update [NAME ...]    apply updates (asks first; --yes to skip)
  agent-skills-updater pin NAME OWNER/REPO[:PATH]   set a skill's source by hand
  agent-skills-updater ignore NAME          never update this skill (unignore to undo)

Sources are detected, in order, from: a git checkout the skill lives in (left to
`git pull`), the `npx skills` lock file, GitHub links in SKILL.md frontmatter, the
other skills of a repository already identified, and GitHub code search confirmed
by comparing file contents. Detection results are saved, so it runs once per skill.

Safety: every update is backed up first; a skill you edited since the last update
is never overwritten without --force; updates that add or change scripts are
listed separately and skipped by --safe; symlinks in upstream folders are never
copied. Standard library only. Needs git; code search uses `gh` or $GITHUB_TOKEN.
"""
import argparse
import datetime as _dt
import difflib
import filecmp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

VERSION = "0.2.0"
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "agent-skills-updater")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "agent-skills-updater")
DEFAULT_STATE = os.path.join(CONFIG_DIR, "state.json")
DEFAULT_ROOTS = [
    "~/.claude/skills", "~/.agents/skills", "~/.config/opencode/skills", "~/.opencode/skills",
    "~/.codex/skills", "~/.cursor/skills", "~/.gemini/skills", "~/.copilot/skills",
]
LOCK_FILES = [
    os.path.join(os.environ.get("XDG_STATE_HOME", ""), "skills", ".skill-lock.json") if os.environ.get("XDG_STATE_HOME") else None,
    os.path.join(HOME, ".agents", ".skill-lock.json"),
]
SCRIPT_EXT = {".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts", ".mts", ".py", ".rb", ".pl",
              ".ps1", ".cmd", ".bat", ".exe", ".bin", ".so", ".dylib", ".wasm"}
SKIP = {".git", "node_modules", ".DS_Store", "__pycache__"}
GH_RE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/(?:tree|blob)/[^/\s]+/([^\s'\"#?)]+))?(?:[\s'\"#?)]|$)")
MATCH_THRESHOLD = 0.6
os.environ["GIT_TERMINAL_PROMPT"] = "0"  # never hang on a password prompt for a missing repo


# ---------------------------------------------------------------- utilities

def log(*a):
    print(*a, file=sys.stderr)


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def list_files(root):
    """Regular files under root (relative), and symlinks found."""
    files, links = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if d in SKIP:
                continue
            if os.path.islink(full):
                links.append(os.path.relpath(full, root))
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            full = os.path.join(dirpath, f)
            if f in SKIP:
                continue
            if os.path.islink(full):
                links.append(os.path.relpath(full, root))
            else:
                files.append(os.path.relpath(full, root))
    return sorted(files), links


def tree_hash(root):
    h = hashlib.sha256()
    for rel in list_files(root)[0]:
        h.update(rel.encode())
        with open(os.path.join(root, rel), "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


def frontmatter(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(20000)
    except OSError:
        return "", ""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    return (m.group(1) if m else ""), text


def fm_name(path):
    fm, _ = frontmatter(path)
    m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", fm, re.M)
    return m.group(1).strip() if m else None


def similarity(local, upstream):
    """0..1: share of identical files, or SKILL.md text similarity if higher."""
    lf, _ = list_files(local)
    uf, _ = list_files(upstream)
    if not lf or not uf:
        return 0.0
    common = set(lf) & set(uf)
    same = sum(1 for f in common if filecmp.cmp(os.path.join(local, f), os.path.join(upstream, f), shallow=False))
    file_score = same / max(len(lf), len(uf))
    try:
        a = open(os.path.join(local, "SKILL.md"), encoding="utf-8", errors="replace").read()
        b = open(os.path.join(upstream, "SKILL.md"), encoding="utf-8", errors="replace").read()
        text_score = difflib.SequenceMatcher(None, a, b, autojunk=False).quick_ratio()
        if text_score > 0.5:
            text_score = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False).ratio()
    except OSError:
        text_score = 0.0
    return max(file_score, 0.9 * text_score)


def diff_dirs(local, upstream):
    lf = set(list_files(local)[0]) if os.path.isdir(local) else set()
    uf, links = list_files(upstream)
    uf = set(uf)
    added, removed = sorted(uf - lf), sorted(lf - uf)
    modified = sorted(f for f in lf & uf
                      if not filecmp.cmp(os.path.join(local, f), os.path.join(upstream, f), shallow=False))
    return added, removed, modified, links


def is_script(rel, base):
    if os.path.splitext(rel)[1].lower() in SCRIPT_EXT:
        return True
    full = os.path.join(base, rel)
    try:
        if os.access(full, os.X_OK):
            return True
        with open(full, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


# ---------------------------------------------------------------- state

class State:
    def __init__(self, path):
        self.path = path
        self.data = {"version": 1, "skills": {}, "repos_without_match": {}}
        if os.path.exists(path):
            with open(path) as fh:
                self.data.update(json.load(fh))

    def get(self, key):
        return self.data["skills"].get(key)

    def set(self, key, entry):
        self.data["skills"][key] = entry

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- discovery of installed skills

def find_skills(roots):
    """{realpath: {"name", "paths"}} for every skill folder under the roots, deduplicated."""
    found = {}
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            p = os.path.join(root, entry)
            if not os.path.isdir(p):
                continue
            candidates = [p]
            if not os.path.isfile(os.path.join(p, "SKILL.md")):
                # a bundle folder holding several skills (e.g. a cloned skills repo)
                candidates = []
                for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                    dirnames[:] = [d for d in dirnames if d not in SKIP]
                    if "SKILL.md" in filenames:
                        candidates.append(dirpath)
                        dirnames[:] = []
            for c in candidates:
                real = os.path.realpath(c)
                item = found.setdefault(real, {"name": os.path.basename(real), "paths": [],
                                               "root": os.path.realpath(root)})
                if c not in item["paths"]:
                    item["paths"].append(c)
    return found


def git_toplevel(path):
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def read_locks():
    out = {}
    for lf in LOCK_FILES:
        if lf and os.path.exists(lf):
            try:
                data = json.load(open(lf))
            except (OSError, ValueError):
                continue
            for name, e in (data.get("skills") or {}).items():
                if e.get("sourceType", "github") == "github" and "/" in e.get("source", ""):
                    path = e.get("skillPath") or ""
                    if path.endswith("SKILL.md"):
                        path = os.path.dirname(path)
                    out[name] = {"repo": e["source"], "path": path or None, "commit": e.get("commit")}
    return out


# ---------------------------------------------------------------- upstream repositories

class Repos:
    def __init__(self):
        self.dir = os.path.join(CACHE_DIR, "repos")
        self.fetched = {}
        self.index_cache = {}
        self.meta = {}

    def checkout(self, repo):
        """Fresh shallow checkout of the default branch; returns (path, commit) or (None, error)."""
        if repo in self.fetched:
            return self.fetched[repo]
        dest = os.path.join(self.dir, repo.replace("/", "__"))
        url = f"https://github.com/{repo}.git"
        try:
            if os.path.isdir(os.path.join(dest, ".git")):
                run(["git", "-C", dest, "fetch", "--quiet", "--depth", "1", "origin", "HEAD"])
                run(["git", "-C", dest, "reset", "--quiet", "--hard", "FETCH_HEAD"])
            else:
                os.makedirs(self.dir, exist_ok=True)
                run(["git", "clone", "--quiet", "--depth", "1", url, dest])
            res = (dest, run(["git", "-C", dest, "rev-parse", "HEAD"]))
        except RuntimeError as e:
            res = (None, str(e))
        self.fetched[repo] = res
        return res

    def index(self, repo):
        """{skill name: [relative folder, ...]} for every SKILL.md in the repo."""
        if repo in self.index_cache:
            return self.index_cache[repo]
        path, _ = self.checkout(repo)
        idx = {}
        if path:
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if d not in SKIP]
                if "SKILL.md" in filenames:
                    rel = os.path.relpath(dirpath, path)
                    names = {os.path.basename(dirpath) if rel != "." else repo.split("/")[1],
                             fm_name(os.path.join(dirpath, "SKILL.md"))}
                    for n in names:
                        if n:
                            idx.setdefault(n, []).append(rel)
        self.index_cache[repo] = idx
        return idx

    def best_match(self, repo, name, local_dir, hint_path=None):
        """(path, score) of the folder in repo that best matches the local skill."""
        path, _ = self.checkout(repo)
        if not path:
            return None, 0.0
        cands = list(self.index(repo).get(name, []))
        if hint_path and os.path.isfile(os.path.join(path, hint_path, "SKILL.md")) and hint_path not in cands:
            cands.insert(0, hint_path)
        best, score = None, 0.0
        for rel in cands:
            s = similarity(local_dir, os.path.join(path, rel))
            if s > score or (s == score and best and len(rel) < len(best)):
                best, score = rel, s
        return best, score

    def repo_info(self, repo):
        if repo not in self.meta:
            data = github_api(f"repos/{repo}") or {}
            self.meta[repo] = {"stars": data.get("stargazers_count", 0), "fork": data.get("fork", False),
                               "parent": (data.get("parent") or {}).get("full_name")}
        return self.meta[repo]


# ---------------------------------------------------------------- GitHub API

_token = None
_last_search = [0.0]


def gh_token():
    global _token
    if _token is None:
        _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if not _token and shutil.which("gh"):
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            _token = r.stdout.strip() if r.returncode == 0 else ""
    return _token


def github_api(path, params=None):
    url = "https://api.github.com/" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"agent-skills-updater/{VERSION}"})
    tok = gh_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except Exception as e:  # network, rate limit, 404
        log(f"  github api {path}: {e}")
        return None


def description_phrase(skill_md_text, words=8):
    """A run of plain words from the description, usable as an exact-phrase search."""
    m = re.search(r"^description:\s*[>|]?-?\s*(.+?)(?:\n\S|\Z)", skill_md_text, re.M | re.S)
    if not m:
        return None
    text = re.sub(r"\s+", " ", m.group(1)).strip().strip("'\"")
    text = re.split(r"[,.;:()—–\"']", text)[0]  # code search phrases can't span punctuation
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", text) if t]
    return " ".join(tokens[:words]) if len(tokens) >= 4 else None


def search_candidates(name, phrase=None):
    """[(repo, folder, is_fork)] holding a SKILL.md for this skill name, via GitHub code search."""
    if not gh_token():
        return []
    wait = 6.5 - (time.time() - _last_search[0])  # code search allows ~10 requests a minute
    if wait > 0:
        time.sleep(wait)
    _last_search[0] = time.time()
    q = f'filename:SKILL.md "name: {name}"' + (f' "{phrase}"' if phrase else "")
    data = github_api("search/code", {"q": q, "per_page": 100})
    out, seen = [], set()
    for item in (data or {}).get("items", []):
        repo = item["repository"]["full_name"]
        folder = os.path.dirname(item["path"]) or "."
        if (repo, folder) not in seen:
            seen.add((repo, folder))
            out.append((repo, folder, item["repository"].get("fork", False)))
    return out


def stars_for(repos_list):
    """{repo: stars} for many repositories in one GraphQL request."""
    tok = gh_token()
    out = {}
    if not tok or not repos_list:
        return out
    parts = []
    for i, r in enumerate(repos_list[:100]):
        owner, name = r.split("/", 1)
        parts.append(f'r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                     f'{{ nameWithOwner stargazerCount isFork }}')
    body = json.dumps({"query": "query { " + " ".join(parts) + " }"}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                                          "User-Agent": f"agent-skills-updater/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp).get("data") or {}
    except Exception as e:
        log(f"  github graphql: {e}")
        return out
    for i, r in enumerate(repos_list[:100]):
        node = data.get(f"r{i}")
        if node:
            out[r] = node["stargazerCount"] - (10 ** 9 if node["isFork"] else 0)
    return out


def skills_sh_candidates(name):
    """[repo] that publish a skill with exactly this name on the skills.sh directory, most installed first."""
    base = os.environ.get("SKILLS_API_URL", "https://skills.sh")
    url = f"{base}/api/search?" + urllib.parse.urlencode({"q": name})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": f"agent-skills-updater/{VERSION}"}),
                                    timeout=20) as resp:
            data = json.load(resp)
    except Exception as e:
        log(f"  skills.sh search: {e}")
        return []
    hits = [s for s in data.get("skills", []) if s.get("name") == name and re.fullmatch(r"[\w.-]+/[\w.-]+", s.get("source", ""))]
    hits.sort(key=lambda s: s.get("installs", 0), reverse=True)
    out = []
    for s in hits:
        if s["source"].lower() not in (o.lower() for o in out):
            out.append(s["source"])
    return out


def raw_file(repo, path):
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{urllib.parse.quote(path)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": f"agent-skills-updater/{VERSION}"}),
                                    timeout=20) as resp:
            return resp.read(400000).decode("utf-8", "replace")
    except Exception:
        return None


def text_similarity(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.quick_ratio() < 0.5:
        return 0.0
    return difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False).ratio()


# ---------------------------------------------------------------- source resolution

def hints_from_frontmatter(skill_md):
    fm, _ = frontmatter(skill_md)
    hints = []
    for m in GH_RE.finditer(fm):
        owner, repo, sub = m.group(1), m.group(2), m.group(3)
        if owner.lower() in ("orgs", "users", "settings"):
            continue
        sub = sub[:-len("SKILL.md")].rstrip("/") if sub and sub.endswith("SKILL.md") else sub
        hints.append((f"{owner}/{repo}", sub))
    return hints


def resolve_all(skills, state, repos, use_search=True, refresh=False):
    """Fill state with a source for every skill that does not have one yet."""
    locks = read_locks()
    pending = []
    for real, info in sorted(skills.items(), key=lambda kv: kv[1]["name"]):
        e = state.get(real)
        if e and not refresh and e.get("how") in ("pinned", "git", "lock", "frontmatter", "bundle", "directory", "search", "none"):
            if e.get("how") != "none" or not use_search or e.get("searched"):
                continue
        pending.append(real)

    known_repos = []
    for e in state.data["skills"].values():
        if e.get("repo") and e["repo"] not in known_repos:
            known_repos.append(e["repo"])

    def accept(real, repo, path, score, how):
        entry = state.get(real) or {}
        entry.update({"name": skills[real]["name"], "repo": repo, "path": path, "how": how,
                      "confidence": round(score, 3)})
        entry.setdefault("installed_hash", None)
        state.set(real, entry)
        if repo not in known_repos:
            known_repos.append(repo)
        log(f"  {skills[real]['name']}: {repo}/{path} ({how}, {score:.0%} match)")

    unresolved = []
    for real in pending:
        name = skills[real]["name"]
        prev = state.get(real) or {}
        if prev.get("how") == "pinned" and not refresh:
            continue
        top = git_toplevel(real)
        root = skills[real]["root"]
        # a checkout the skill lives in, not a dotfiles repo that contains the whole skills folder
        if top and not (root == top or root.startswith(top + os.sep)):
            remote = subprocess.run(["git", "-C", top, "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
            state.set(real, {**prev, "name": name, "how": "git", "checkout": top, "remote": remote, "repo": None})
            log(f"  {name}: git checkout at {top} (update it with git pull)")
            continue
        lock = locks.get(name)
        if lock:
            path, score = repos.best_match(lock["repo"], name, real, lock["path"])
            if path:
                accept(real, lock["repo"], path, score, "lock")
                continue
        done = False
        for repo, sub in hints_from_frontmatter(os.path.join(real, "SKILL.md")):
            path, score = repos.best_match(repo, name, real, sub)
            if path and score >= MATCH_THRESHOLD:
                accept(real, repo, path, score, "frontmatter")
                done = True
                break
        if not done:
            unresolved.append(real)

    # skills often come in bundles: try repositories we already know first
    still = []
    for real in unresolved:
        name = skills[real]["name"]
        hit = None
        for repo in known_repos:
            if name in repos.index(repo):
                path, score = repos.best_match(repo, name, real)
                if path and score >= MATCH_THRESHOLD and (not hit or score > hit[2]):
                    hit = (repo, path, score)
        if hit:
            accept(real, hit[0], hit[1], hit[2], "bundle")
        else:
            still.append(real)

    if not use_search:
        for real in still:
            state.set(real, {**(state.get(real) or {}), "name": skills[real]["name"], "how": "none", "repo": None})
        return

    if still and not gh_token():
        log("  GitHub code search needs `gh auth login` or GITHUB_TOKEN; using the skills.sh directory only")

    def bundle_sweep(repo, rest):
        """the same repository often holds other skills we have not placed yet"""
        for other in rest:
            oname = skills[other]["name"]
            if (state.get(other) or {}).get("repo") or oname not in repos.index(repo):
                continue
            p, s = repos.best_match(repo, oname, other)
            if p and s >= MATCH_THRESHOLD:
                accept(other, repo, p, s, "bundle")

    for i, real in enumerate(still):
        name = skills[real]["name"]
        e = state.get(real)
        if e and e.get("repo"):  # found through a bundle hit while searching for another skill
            continue
        log(f"  looking up {name} ({i + 1}/{len(still)})")
        # 1. the skills.sh directory ranks originals by installs, so copies rarely win
        best = None
        for repo in skills_sh_candidates(name)[:4]:
            path, score = repos.best_match(repo, name, real)
            if path and score >= MATCH_THRESHOLD and (best is None or score > best[2] + 0.05):
                best = (repo, path, score)
        if best:
            accept(real, best[0], best[1], best[2], "directory")
            bundle_sweep(best[0], still[i + 1:])
            continue
        # 2. repositories named after the skill, most-starred first (e.g. "<name>-skill")
        data = github_api("search/repositories", {"q": f"{name} in:name", "sort": "stars", "per_page": 5})
        for item in (data or {}).get("items", []):
            repo = item["full_name"]
            if name not in repos.index(repo) and not os.path.isfile(os.path.join(repos.checkout(repo)[0] or "", "SKILL.md")):
                continue
            path, score = repos.best_match(repo, name, real)
            if path and score >= MATCH_THRESHOLD:
                best = (repo, path, score)
                break
        if best:
            accept(real, best[0], best[1], best[2], "search")
            bundle_sweep(best[0], still[i + 1:])
            continue
        if not gh_token():
            state.set(real, {**(e or {}), "name": name, "how": "none", "repo": None, "searched": True})
            log(f"  {name}: no source found (set one with: pin {name} OWNER/REPO[:PATH])")
            continue
        # 3. GitHub code search
        try:
            local_md = open(os.path.join(real, "SKILL.md"), encoding="utf-8", errors="replace").read()
        except OSError:
            local_md = ""
        # popular skills have many copies: look at the most-starred repositories first,
        # and compare only SKILL.md text before cloning anything
        # a phrase from the description narrows thousands of same-named skills to real copies
        cands = []
        phrase = description_phrase(local_md)
        if phrase:
            cands = search_candidates(name, phrase)
        if not cands:
            cands = search_candidates(name)
        stars = stars_for(sorted({c[0] for c in cands}))
        cands.sort(key=lambda c: stars.get(c[0], -1), reverse=True)
        prelim = []
        for repo, folder, fork in cands[:10]:
            md = raw_file(repo, f"{folder}/SKILL.md" if folder != "." else "SKILL.md")
            s = text_similarity(local_md, md) if md else 0.0
            if s >= MATCH_THRESHOLD:
                prelim.append((s, repo, folder, fork))
        if not prelim:
            state.set(real, {**(e or {}), "name": name, "how": "none", "repo": None, "searched": True})
            log(f"  {name}: no source found (set one with: pin {name} OWNER/REPO[:PATH])")
            continue
        chosen = None
        for s, repo, folder, _ in prelim[:3]:  # already in star order; confirm with the full folder
            path, score = repos.best_match(repo, name, real, folder)
            if path and score >= MATCH_THRESHOLD:
                chosen = (repo, path, score)
                break
        if not chosen:
            state.set(real, {**(e or {}), "name": name, "how": "none", "repo": None, "searched": True})
            log(f"  {name}: no confirmed source")
            continue
        repo, path, score = chosen
        accept(real, repo, path, score, "search")
        bundle_sweep(repo, still[i + 1:])


# ---------------------------------------------------------------- check and update

def assess(skills, state, repos, names=None):
    """List of dicts describing each skill's update status."""
    results = []
    for real, info in sorted(skills.items(), key=lambda kv: kv[1]["name"]):
        name = info["name"]
        if names and name not in names:
            continue
        e = state.get(real) or {}
        r = {"real": real, "name": name, "entry": e}
        if e.get("ignored"):
            r["status"] = "ignored"
        elif e.get("how") == "git":
            r["status"] = "git"
        elif not e.get("repo"):
            r["status"] = "unknown"
        else:
            path, commit = repos.checkout(e["repo"])
            up = os.path.join(path, e["path"]) if path else None
            if not up or not os.path.isfile(os.path.join(up, "SKILL.md")):
                r["status"] = "missing-upstream"
                r["detail"] = commit if not path else f"{e['path']} not in {e['repo']}"
            else:
                added, removed, modified, links = diff_dirs(real, up)
                r.update(upstream=up, commit=commit, added=added, removed=removed, modified=modified, links=links)
                r["scripts"] = [f for f in added + modified if is_script(f, up)]
                current = tree_hash(real)
                r["hash"] = current
                if not (added or removed or modified):
                    r["status"] = "current"
                elif e.get("installed_hash") and e["installed_hash"] != current:
                    r["status"] = "edited"
                elif e.get("installed_hash"):
                    r["status"] = "update"
                else:
                    r["status"] = "differs"  # no baseline yet: outdated or edited, can't tell
        results.append(r)
    return results


def apply_update(r, backup_root):
    real, up = r["real"], r["upstream"]
    os.makedirs(backup_root, exist_ok=True)
    backup = os.path.join(backup_root, r["name"])
    n = 1
    while os.path.exists(backup):
        n += 1
        backup = os.path.join(backup_root, f"{r['name']}.{n}")
    shutil.copytree(real, backup, symlinks=True)
    tmp = real + ".asu-new"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    for rel in list_files(up)[0]:
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(up, rel), dst)
    old = real + ".asu-old"
    os.rename(real, old)
    os.rename(tmp, real)
    shutil.rmtree(old)
    return backup


LABEL = {"current": "up to date", "update": "update available", "differs": "differs from upstream",
         "edited": "edited locally", "unknown": "source unknown", "git": "git checkout",
         "ignored": "ignored", "missing-upstream": "gone upstream"}


def summarize(results, markdown=False):
    lines = []
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines.append(", ".join(f"{v} {LABEL[k]}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    actionable = [r for r in results if r["status"] in ("update", "differs", "edited")]
    if actionable:
        lines.append("")
        if markdown:
            lines += ["| Skill | Status | Source | Added | Modified | Removed |", "| --- | --- | --- | --- | --- | --- |"]
        for r in actionable:
            e = r["entry"]
            src = f"{e['repo']}/{e['path']}".rstrip("/.")
            if markdown:
                lines.append(f"| `{r['name']}` | {LABEL[r['status']]} | [`{src}`](https://github.com/{e['repo']}/tree/{r['commit']}/{e['path']}) "
                             f"| {len(r['added'])} | {len(r['modified'])} | {len(r['removed'])} |")
            else:
                lines.append(f"  {r['name']:<34} {LABEL[r['status']]:<22} +{len(r['added'])} ~{len(r['modified'])} -{len(r['removed'])}  {src}")
    scripts = [(r["name"], f) for r in actionable for f in r["scripts"]]
    if scripts:
        lines += ["", "Scripts that would be added or changed (read before updating):" if not markdown
                  else "### Review first: new or changed scripts", ""]
        lines += [f"  {n}/{f}" if not markdown else f"- `{n}/{f}`" for n, f in scripts]
    unknown = [r["name"] for r in results if r["status"] == "unknown"]
    if unknown:
        lines += ["", f"Source unknown ({len(unknown)}): " + ", ".join(unknown)]
    gone = [f"{r['name']} ({r.get('detail')})" for r in results if r["status"] == "missing-upstream"]
    if gone:
        lines += ["", "Gone upstream: " + ", ".join(gone)]
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def cmd_scan(args, skills, state, repos):
    resolve_all(skills, state, repos, use_search=not args.no_search, refresh=args.refresh)
    state.save()
    for real, info in sorted(skills.items(), key=lambda kv: kv[1]["name"]):
        e = state.get(real) or {}
        if e.get("how") == "git":
            src = f"git checkout {e.get('checkout')}"
        elif e.get("repo"):
            src = f"{e['repo']}/{e['path']}".rstrip("/.") + f"  [{e['how']}{', %d%%' % (100 * e['confidence']) if e.get('confidence') is not None else ''}]"
        else:
            src = "unknown"
        flag = "  (ignored)" if e.get("ignored") else ""
        print(f"{info['name']:<36} {src}{flag}")
    known = sum(1 for r in skills if (state.get(r) or {}).get("repo") or (state.get(r) or {}).get("how") == "git")
    print(f"\n{len(skills)} skills, {known} with a known source")


def cmd_check(args, skills, state, repos):
    resolve_all(skills, state, repos, use_search=not args.no_search)
    results = assess(skills, state, repos, set(args.names) if args.names else None)
    for r in results:  # a skill that matches upstream is a known-clean baseline
        if r["status"] == "current":
            r["entry"]["installed_hash"] = r["hash"]
            r["entry"]["commit"] = r["commit"]
            state.set(r["real"], r["entry"])
    state.save()
    text = summarize(results, markdown=bool(args.report))
    print(summarize(results))
    if args.report:
        with open(args.report, "w") as fh:
            fh.write("## Skill updates\n\n" + text + "\n")
    if any(r["status"] == "differs" for r in results):
        print("\n'differs' means there is no record of the installed version yet, so it may be outdated or edited."
              "\nRun update to take upstream's version; later runs can then tell your edits apart.")
    return 10 if any(r["status"] in ("update", "differs") for r in results) else 0


def cmd_update(args, skills, state, repos):
    resolve_all(skills, state, repos, use_search=not args.no_search)
    results = assess(skills, state, repos, set(args.names) if args.names else None)
    todo = []
    for r in results:
        if r["status"] == "update" or (r["status"] == "differs" and not args.only_known):
            todo.append(r)
        elif r["status"] == "edited" and args.force:
            todo.append(r)
    held = []
    if args.safe:
        held = [r for r in todo if r["scripts"]]
        todo = [r for r in todo if not r["scripts"]]
        for r in held:
            log(f"  held back (changes scripts): {r['name']}")
    waiting = held + [r for r in results if r["status"] == "differs" and args.only_known]
    if args.notify:
        import atexit
        atexit.register(notify, todo, waiting, args.dry_run)
    edited = [r["name"] for r in results if r["status"] == "edited" and not args.force]
    if edited:
        log(f"  kept your edits (use --force to overwrite): {', '.join(edited)}")
    for r in results:  # record a baseline for skills that already match
        if r["status"] == "current":
            r["entry"]["installed_hash"] = r["hash"]
            r["entry"]["commit"] = r["commit"]
            state.set(r["real"], r["entry"])
    if not todo:
        state.save()
        print("nothing to update")
        return 0
    print(summarize(todo))
    if not args.yes:
        if not sys.stdin.isatty():
            log("not a terminal; pass --yes to apply")
            state.save()
            return 1
        if input(f"\nUpdate {len(todo)} skill(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            state.save()
            return 1
    if args.dry_run:
        print("dry run: nothing changed")
        state.save()
        return 0
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(CACHE_DIR, "backups", stamp)
    for r in todo:
        apply_update(r, backup_root)
        e = r["entry"]
        e["installed_hash"] = tree_hash(r["real"])
        e["commit"] = r["commit"]
        e["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        state.set(r["real"], e)
        print(f"updated {r['name']}")
    state.save()
    print(f"backups: {backup_root}")
    if args.report:
        with open(args.report, "w") as fh:
            fh.write("## Skill updates\n\n" + summarize(todo, markdown=True) + "\n")
    return 0


def notify(done, waiting, dry_run=False):
    """Desktop notification (notify-send) summarising an unattended run."""
    if dry_run or not (done or waiting) or not shutil.which("notify-send"):
        return
    parts = []
    if done:
        parts.append(f"Updated {len(done)}: " + ", ".join(r["name"] for r in done[:6]) + ("…" if len(done) > 6 else ""))
    if waiting:
        parts.append(f"{len(waiting)} need review: run `agent-skills-updater update`")
    subprocess.run(["notify-send", "--app-name=agent-skills-updater", "Agent skills", "\n".join(parts)], check=False)


def find_by_name(skills, name):
    hits = [real for real, i in skills.items() if i["name"] == name]
    if not hits:
        sys.exit(f"no installed skill named {name}")
    return hits


def cmd_pin(args, skills, state, repos):
    repo, _, path = args.source.partition(":")
    for real in find_by_name(skills, args.name):
        p, score = repos.best_match(repo, args.name, real, path or None)
        if not p:
            sys.exit(f"no SKILL.md for {args.name} found in {repo}" + (f" at {path}" if path else ""))
        e = state.get(real) or {}
        e.update({"name": args.name, "repo": repo, "path": p, "how": "pinned", "confidence": round(score, 3)})
        state.set(real, e)
        print(f"{args.name}: pinned to {repo}/{p} ({score:.0%} match)")
    state.save()


def cmd_ignore(args, skills, state, repos, value=True):
    for real in find_by_name(skills, args.name):
        e = state.get(real) or {"name": args.name}
        e["ignored"] = value
        state.set(real, e)
    state.save()
    print(f"{args.name}: {'ignored' if value else 'no longer ignored'}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent-skills-updater", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=VERSION)
    ap.add_argument("--dir", action="append", help="skills folder to manage (repeatable; default: the usual install locations)")
    ap.add_argument("--state", default=DEFAULT_STATE, help=f"state file (default {DEFAULT_STATE})")
    ap.add_argument("--no-search", action="store_true", help="don't use GitHub code search to find sources")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="list installed skills and their sources")
    s.add_argument("--refresh", action="store_true", help="detect sources again, ignoring saved results")
    c = sub.add_parser("check", help="show available updates (exit code 10 if any)")
    c.add_argument("names", nargs="*")
    c.add_argument("--report", help="also write a Markdown report here")
    u = sub.add_parser("update", help="apply updates")
    u.add_argument("names", nargs="*")
    u.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    u.add_argument("--safe", action="store_true", help="skip updates that add or change scripts")
    u.add_argument("--only-known", action="store_true",
                   help="only update skills with a recorded baseline (safe for unattended runs)")
    u.add_argument("--force", action="store_true", help="also overwrite skills you edited")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--report", help="write a Markdown report of what was updated")
    u.add_argument("--notify", action="store_true", help="desktop notification of what changed or needs review")
    p = sub.add_parser("pin", help="set a skill's source: OWNER/REPO or OWNER/REPO:PATH")
    p.add_argument("name")
    p.add_argument("source")
    i = sub.add_parser("ignore", help="never update a skill")
    i.add_argument("name")
    ui = sub.add_parser("unignore", help="update a skill again")
    ui.add_argument("name")
    args = ap.parse_args(argv)

    roots = args.dir or DEFAULT_ROOTS
    skills = find_skills(roots)
    if not skills:
        sys.exit("no skills found in: " + ", ".join(roots))
    state = State(os.path.expanduser(args.state))
    repos = Repos()
    if args.cmd == "scan":
        return cmd_scan(args, skills, state, repos)
    if args.cmd == "check":
        return cmd_check(args, skills, state, repos)
    if args.cmd == "update":
        return cmd_update(args, skills, state, repos)
    if args.cmd == "pin":
        return cmd_pin(args, skills, state, repos)
    if args.cmd == "ignore":
        return cmd_ignore(args, skills, state, repos, True)
    if args.cmd == "unignore":
        return cmd_ignore(args, skills, state, repos, False)


if __name__ == "__main__":
    sys.exit(main() or 0)
