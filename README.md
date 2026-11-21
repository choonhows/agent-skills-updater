# agent-skills-updater

Keep the agent skills you have installed up to date, whichever skills those are.

It finds every skill folder (a folder with `SKILL.md`) that Claude Code, OpenCode, Codex, Cursor, Gemini CLI or Copilot can see, works out which GitHub repository each one came from, and updates them from there. There is no list to maintain: it adapts to whatever you have installed.

One Python file, standard library only. It needs `git`. `gh` or a `GITHUB_TOKEN` is optional but improves source detection.

## Install

```bash
git clone https://github.com/choonhows/agent-skills-updater.git
cd agent-skills-updater && ./install.sh
```

This puts `agent-skills-updater` in `~/.local/bin`. On Linux with systemd it also enables a daily timer (`--no-timer` to skip, `--uninstall` to remove). On other systems, run `python3 agent_skills_updater.py` directly or schedule it with cron or launchd.

## Use

```bash
agent-skills-updater scan      # list your skills and where each one comes from
agent-skills-updater check     # what's out of date (exit code 10 if anything is)
agent-skills-updater update    # show the changes, ask, then apply them
```

Run `update` yourself the first time. After that, the timer keeps things current.

| Command | What it does |
| --- | --- |
| `scan [--refresh]` | Detect sources. Results are saved, so later runs only look up new skills. |
| `check [NAME ...] [--report FILE]` | Compare installed skills with upstream. |
| `update [NAME ...]` | Apply updates. `--yes` skips the prompt, `--safe` skips updates that add or change scripts, `--only-known` skips skills with no recorded baseline, `--force` also overwrites skills you edited, `--dry-run` changes nothing, `--notify` sends a desktop notification. |
| `pin NAME OWNER/REPO[:PATH]` | Set a skill's source by hand when detection misses it or picks the wrong one. |
| `ignore NAME` / `unignore NAME` | Never update a skill, or start again. |

Global options: `--dir PATH` (repeatable) to manage specific folders instead of the usual locations, `--state FILE`, and `--no-search`.

## Where it looks

`~/.claude/skills`, `~/.agents/skills`, `~/.config/opencode/skills`, `~/.opencode/skills`, `~/.codex/skills`, `~/.cursor/skills`, `~/.gemini/skills` and `~/.copilot/skills`. Symlinked skills are counted once. Folders that hold several skills (a cloned bundle) are searched one level down.

## How it finds a skill's source

It tries these in order and stops at the first one that confirms by comparing file contents (at least a 60% match):

1. **A git checkout.** If the skill lives inside its own clone, the updater reports it and leaves it to `git pull`.
2. **The `npx skills` lock file** (`~/.agents/.skill-lock.json`).
3. **GitHub links** in the `SKILL.md` frontmatter.
4. **Repositories it already knows.** Skills usually come in bundles, so one identified skill often places dozens of others.
5. **The [skills.sh](https://skills.sh) directory**, ranked by installs. This puts originals ahead of copies.
6. **GitHub search:** first repositories named after the skill, ranked by stars, then code search on a phrase from its description.

If none of these confirm a match, the skill is marked unknown. Use `pin` to set its source.

To look skills up, it sends their names, and a phrase from their description, to skills.sh and GitHub. Nothing else about your machine leaves it.

## Safety

- **Edits are kept.** After each update it records a hash of the installed skill. A skill you changed afterwards is reported as "edited locally" and left alone unless you pass `--force`.
- **First-time differences need you.** With no baseline yet, it can't tell an outdated skill from an edited one. It reports these as "differs", and only an update you confirm (or `--yes`) applies them. The timer passes `--only-known`, so it never touches them.
- **Scripts are reviewed.** Every update lists new or changed scripts and executables first. The timer passes `--safe`, so those updates always wait for you.
- **Backups.** Every update is copied to `~/.cache/agent-skills-updater/backups/<time>/` first.
- **No symlinks or extras.** Upstream symlinks are never copied, and nothing outside the skill folder is touched.
- **New upstream skills are never added**, only the ones you already have are updated.

State lives in `~/.config/agent-skills-updater/state.json`, and the upstream clone cache in `~/.cache/agent-skills-updater/repos/`.

## A skills repository in CI

To keep a repository that vendors skills under `skills/` current through pull requests, run a scan locally once and commit the state file:

```bash
python3 agent_skills_updater.py --dir skills --state .skills-updater.json scan
```

Then copy [`examples/update-skills.yml`](examples/update-skills.yml) into `.github/workflows/`. It opens a weekly pull request with the changes and the scripts to review.

## License

[MIT](LICENSE). Skills keep their own licenses. This tool only fetches them from where their authors publish them.
