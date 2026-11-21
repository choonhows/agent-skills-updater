# agent-skills-updater

Keep a vendored collection of agent skills (`SKILL.md` folders for Claude Code, OpenCode, Codex, Cursor and others) up to date with their upstream repositories, without letting third-party code onto your machine unreviewed.

It works in two steps, with a pull request between them:

1. **Upstream → pull request.** A weekly GitHub Action fetches every upstream you track, refreshes the skills that changed, and opens one pull request. Its description lists new or changed scripts first, links each upstream compare view, and names any new upstream skills it did not add.
2. **Merged → your machine.** A daily systemd user timer pulls your merged branch and installs it into `~/.agents/skills`, linking new skills into `~/.claude/skills`. It never overwrites a skill you edited locally.

Standard library Python 3 and Bash only. No dependencies, no API keys.

## Expected layout

The tools run inside your own skills repository:

```text
your-skills-repo/
  skills/<name>/SKILL.md        # one folder per skill
  sources/tracked.json          # where each skill comes from (generated below)
  sources/removed-skills.json   # optional: skills you never want back
  tools/                        # copied from this repo
  .github/workflows/update-skills.yml
```

## Setup

1. Copy `tools/` and `.github/workflows/update-skills.yml` into your skills repository.
2. Generate `sources/tracked.json` from the upstreams your skills came from:

   ```bash
   python3 tools/discover_sources.py emilkowalski/skills cloudflare/security-audit-skill --write
   ```

   It matches each local skill to the closest upstream folder and reports skills that more than one upstream claims and skills no upstream claims. Then edit the file:
   - `"mode": "report"` for a source whose skills you changed locally, so updates are listed rather than applied.
   - `"report_new_skills": true` for bundles where you want to hear about skills you don't have yet.
   - `"exclude"` for upstream files you deliberately left out.

   See [`examples/tracked.json`](examples/tracked.json).
3. Optional: add `sources/removed-skills.json` (see [`examples/removed-skills.json`](examples/removed-skills.json)) for skills you removed and never want restored.
4. Check that it works without changing anything:

   ```bash
   python3 tools/update_skills.py --dry-run
   ```

5. In your repository on GitHub, go to **Settings → Actions → General → Workflow permissions** and tick **Allow GitHub Actions to create and approve pull requests**. Otherwise the workflow can't open its pull request.
6. On each machine (Linux with systemd):

   ```bash
   tools/install-sync-timer.sh https://github.com/you/your-skills-repo.git
   agent-skills-sync --dry-run
   ```

   On the first run, add `--adopt` once if your installed copies are older than the repository's. Logs are in `journalctl --user -u agent-skills-sync`. Remove with `tools/install-sync-timer.sh --uninstall`.

## Safety model

- **Nothing lands unreviewed.** Upstream changes only ever reach a pull request. The sync installs only what you merged.
- **Scripts first.** The pull request lists every new or changed script or binary at the top.
- **No symlinks.** Upstream symlinks are skipped and reported, never copied.
- **Removed means removed.** Skills in `removed-skills.json` are never refreshed or re-added.
- **New skills are opt-in.** New upstream skills are reported, not added. Map one in `tracked.json` to start tracking it.
- **Your edits win.** The sync skips any installed skill that no longer matches the previous repository version, and any skill whose `~/.claude/skills` entry points somewhere else, such as a development checkout. Skills that exist only on your machine are never touched.
- **Removals propagate carefully.** A skill deleted from the repository and listed in `removed-skills.json` is uninstalled only if your copy is unedited.

## Configuration

`tools/sync-local.sh` reads its settings from the environment or from `~/.config/agent-skills-sync.env`:

| Variable | Default |
| --- | --- |
| `SKILLS_REPO_URL` | required |
| `SKILLS_BRANCH` | `main` |
| `SKILLS_CLONE` | `~/.local/share/agent-skills` |
| `SKILLS_TARGET` | `~/.agents/skills` |
| `SKILLS_LINKS` | `~/.claude/skills` |

The workflow runs Mondays at 02:00 UTC. Change the `cron` line to run it at another time, or start it by hand from the Actions tab.

## License

[MIT](LICENSE). Skills you vendor keep their own licenses. Check that you may redistribute a skill before publishing a collection that contains it.
