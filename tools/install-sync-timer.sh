#!/usr/bin/env bash
# Install tools/sync-local.sh as ~/.local/bin/agent-skills-sync and enable a daily
# systemd user timer for it. Run again after pulling to pick up script changes.
#
#   tools/install-sync-timer.sh <skills-repo-git-url>   # first install
#   tools/install-sync-timer.sh                         # reinstall, keep config
#   tools/install-sync-timer.sh --uninstall
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
bin="$HOME/.local/bin/agent-skills-sync"
cfg_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
units="$cfg_dir/systemd/user"
env_file="$cfg_dir/agent-skills-sync.env"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now agent-skills-sync.timer 2>/dev/null || true
  rm -f "$bin" "$units/agent-skills-sync.service" "$units/agent-skills-sync.timer"
  systemctl --user daemon-reload
  echo "removed timer, units and $bin (kept $env_file)"
  exit 0
fi

if [ -n "${1:-}" ]; then
  mkdir -p "$cfg_dir"
  printf 'SKILLS_REPO_URL=%s\n' "$1" > "$env_file"
  echo "wrote $env_file"
elif [ ! -f "$env_file" ]; then
  echo "usage: $0 <skills-repo-git-url>" >&2
  exit 2
fi

mkdir -p "$(dirname "$bin")" "$units"
install -m 755 "$here/sync-local.sh" "$bin"
install -m 644 "$here/systemd/agent-skills-sync.service" "$here/systemd/agent-skills-sync.timer" "$units/"
systemctl --user daemon-reload
systemctl --user enable --now agent-skills-sync.timer
systemctl --user list-timers agent-skills-sync.timer --no-pager
echo "first run now: $bin --dry-run   (add --adopt once if your installed copies predate the repo)"
echo "logs: journalctl --user -u agent-skills-sync"
