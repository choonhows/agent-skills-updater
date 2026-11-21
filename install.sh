#!/usr/bin/env bash
# Install agent-skills-updater to ~/.local/bin and, on systemd, a daily user timer.
#   ./install.sh               install or upgrade
#   ./install.sh --no-timer    install the command only
#   ./install.sh --uninstall   remove the command and timer (keeps your state file)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
bin="$HOME/.local/bin/agent-skills-updater"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
have_systemd() { command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; }

if [ "${1:-}" = "--uninstall" ]; then
  if have_systemd; then
    systemctl --user disable --now agent-skills-updater.timer 2>/dev/null || true
  fi
  rm -f "$bin" "$units/agent-skills-updater.service" "$units/agent-skills-updater.timer"
  if have_systemd; then systemctl --user daemon-reload; fi
  echo "removed (your state is still in ~/.config/agent-skills-updater)"
  exit 0
fi

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
mkdir -p "$(dirname "$bin")"
install -m 755 "$here/agent_skills_updater.py" "$bin"
echo "installed $bin"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add ~/.local/bin to your PATH" ;; esac

if [ "${1:-}" != "--no-timer" ] && have_systemd; then
  mkdir -p "$units"
  install -m 644 "$here/systemd/agent-skills-updater.service" "$here/systemd/agent-skills-updater.timer" "$units/"
  systemctl --user daemon-reload
  systemctl --user enable --now agent-skills-updater.timer
  echo "daily timer enabled (logs: journalctl --user -u agent-skills-updater)"
fi

cat <<'EOF'

Next:
  agent-skills-updater scan      # find where each installed skill comes from (once)
  agent-skills-updater check     # see what's out of date
  agent-skills-updater update    # review and apply; afterwards the timer keeps them current
EOF
