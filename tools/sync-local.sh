#!/usr/bin/env bash
# Install the merged skills from your skills repository into ~/.agents/skills and link
# them into ~/.claude/skills. Meant to run from a systemd user timer (see tools/systemd/).
#
#   sync-local.sh [--adopt] [--dry-run]
#
# Configuration (environment, or ~/.config/agent-skills-sync.env):
#   SKILLS_REPO_URL  git URL of your skills repository (required)
#   SKILLS_BRANCH    branch to install from (default main)
#   SKILLS_CLONE     local clone (default ~/.local/share/agent-skills)
#   SKILLS_TARGET    install folder (default ~/.agents/skills)
#   SKILLS_LINKS     folder that gets symlinks (default ~/.claude/skills)
#
# - Keeps a clone at $SKILLS_CLONE and fast-forwards it to origin/$SKILLS_BRANCH.
# - A skill whose installed copy no longer matches the previous clone (you edited it) is
#   left alone and reported. --adopt overwrites those once, to start from a clean state.
# - A skill whose ~/.claude/skills entry points somewhere other than ~/.agents/skills
#   (a dev checkout, another tool's folder) is owned elsewhere and is skipped.
# - A skill deleted from the repository and listed in sources/removed-skills.json is
#   uninstalled, unless you edited it.
# - Skills that exist only on this machine are never touched.
set -euo pipefail

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/agent-skills-sync.env"
# shellcheck disable=SC1090
[ -f "$CONFIG" ] && . "$CONFIG"
REPO_URL="${SKILLS_REPO_URL:-}"
BRANCH="${SKILLS_BRANCH:-main}"
CLONE="${SKILLS_CLONE:-$HOME/.local/share/agent-skills}"
TARGET="${SKILLS_TARGET:-$HOME/.agents/skills}"
LINKS="${SKILLS_LINKS:-$HOME/.claude/skills}"
ADOPT=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --adopt) ADOPT=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '%s\n' "$*"; }
do_() { if [ "$DRY" = 1 ]; then log "  would: $*"; else "$@"; fi; }

if [ -z "$REPO_URL" ]; then
  echo "error: set SKILLS_REPO_URL (environment or $CONFIG)" >&2
  exit 2
fi
mkdir -p "$TARGET" "$LINKS"

first_run=0
if [ ! -d "$CLONE/.git" ]; then
  log "cloning $REPO_URL -> $CLONE"
  git clone --quiet --branch "$BRANCH" "$REPO_URL" "$CLONE"
  first_run=1
fi

if [ -n "$(git -C "$CLONE" status --porcelain)" ]; then
  log "error: $CLONE has local changes; refusing to sync" >&2
  exit 1
fi

old_head="$(git -C "$CLONE" rev-parse HEAD)"

# Owned-elsewhere and locally-edited skills, judged against the clone *before* pulling.
declare -A skip=()
owned_elsewhere() {
  local name="$1" link="$LINKS/$1"
  if [ -L "$link" ]; then
    local dest
    dest="$(readlink -f "$link" || true)"
    [ "$dest" != "$(readlink -f "$TARGET/$name" 2>/dev/null || echo "$TARGET/$name")" ] && return 0
  elif [ -e "$link" ]; then
    return 0 # a real folder in ~/.claude/skills
  fi
  return 1
}
for dir in "$CLONE"/skills/*/; do
  name="$(basename "$dir")"
  if owned_elsewhere "$name"; then
    skip[$name]="owned elsewhere ($(readlink -f "$LINKS/$name" 2>/dev/null || echo "$LINKS/$name"))"
  elif [ -d "$TARGET/$name" ] && [ "$first_run" = 0 ] && [ "$ADOPT" = 0 ] \
       && ! diff -rq "$TARGET/$name" "$dir" >/dev/null 2>&1; then
    skip[$name]="edited locally"
  fi
done

git -C "$CLONE" fetch --quiet origin "$BRANCH"
git -C "$CLONE" merge --quiet --ff-only "origin/$BRANCH"
new_head="$(git -C "$CLONE" rev-parse HEAD)"
log "repo ${old_head:0:10} -> ${new_head:0:10}"

installed=0 updated=0 removed=0
for dir in "$CLONE"/skills/*/; do
  name="$(basename "$dir")"
  [ -n "${skip[$name]:-}" ] && continue
  dest="$TARGET/$name"
  if [ -d "$dest" ] && diff -rq "$dest" "$dir" >/dev/null 2>&1; then
    :
  else
    if [ -d "$dest" ]; then updated=$((updated + 1)); log "update  $name"; else installed=$((installed + 1)); log "install $name"; fi
    do_ rsync -a --delete "$dir" "$dest/"
  fi
  if [ ! -e "$LINKS/$name" ] && [ ! -L "$LINKS/$name" ]; then
    do_ ln -s "$dest" "$LINKS/$name"
  fi
done

# Uninstall skills that were deleted from the repository on purpose.
removed_list="$(python3 -I -c 'import json,os,sys; p=sys.argv[1]; print("\n".join(s["name"] for s in json.load(open(p))["skills"]) if os.path.exists(p) else "")' \
  "$CLONE/sources/removed-skills.json")"
while IFS= read -r name; do
  [ -z "$name" ] && continue
  [ -d "$CLONE/skills/$name" ] && continue
  [ -d "$TARGET/$name" ] || continue
  if owned_elsewhere "$name"; then continue; fi
  # only remove copies that match what this repo shipped before
  if git -C "$CLONE" cat-file -e "$old_head:skills/$name" 2>/dev/null; then
    tmp="$(mktemp -d)"
    git -C "$CLONE" archive "$old_head" "skills/$name" | tar -x -C "$tmp"
    if diff -rq "$TARGET/$name" "$tmp/skills/$name" >/dev/null 2>&1 || [ "$ADOPT" = 1 ]; then
      log "remove  $name"
      do_ rm -rf "$TARGET/$name"
      [ -L "$LINKS/$name" ] && do_ rm "$LINKS/$name"
      removed=$((removed + 1))
    else
      log "keep    $name (removed upstream but edited locally)"
    fi
    rm -rf "$tmp"
  fi
done <<< "$removed_list"

for name in "${!skip[@]}"; do log "skip    $name: ${skip[$name]}"; done
log "done: $installed installed, $updated updated, $removed removed, ${#skip[@]} skipped"
