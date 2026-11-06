#!/usr/bin/env bash
#
# Controlled upstream review for the vendored control-plane fork.
#
#   scripts/control_plane_upstream.sh status        # how far behind the pin is
#   scripts/control_plane_upstream.sh diff [commit] # reviewable diff, kept files only
#   scripts/control_plane_upstream.sh files         # files this fork still carries
#
# This script never merges, rebases or writes to web/. Updating the fork is a
# manual, reviewed cherry-pick; see
# docs/architecture/control-plane-fork-boundary.md.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pin_file="$repo_root/web/UPSTREAM.json"

read_pin() {
  python3 - "$pin_file" "$1" <<'PY'
import json, sys
pin = json.load(open(sys.argv[1]))
keys = sys.argv[2].split('.')
value = pin
for key in keys:
    value = value[key]
print(value)
PY
}

upstream_url="$(read_pin upstream.repository)"
pinned_commit="$(read_pin upstream.pinnedCommit)"
remote_name="$(read_pin upstream.remote)"

ensure_remote() {
  if ! git -C "$repo_root" remote get-url "$remote_name" >/dev/null 2>&1; then
    git -C "$repo_root" remote add "$remote_name" "$upstream_url"
  fi
  git -C "$repo_root" fetch --quiet "$remote_name"
}

# Paths this fork still carries. Removed capabilities are not re-surfaced for
# review, so an upstream change to a deleted file cannot creep back in.
kept_paths() {
  git -C "$repo_root" ls-files 'web/*' \
    | grep -v '^web/node_modules/' \
    | sed 's|^web/||'
}

case "${1:-status}" in
  status)
    ensure_remote
    head_commit="$(git -C "$repo_root" rev-parse "$remote_name/main")"
    echo "upstream repository : $upstream_url"
    echo "pinned commit       : $pinned_commit"
    echo "upstream HEAD       : $head_commit"
    if [[ "$head_commit" == "$pinned_commit" ]]; then
      echo "status              : up to date"
    else
      behind="$(git -C "$repo_root" rev-list --count "$pinned_commit..$head_commit")"
      echo "status              : $behind commit(s) behind"
      echo
      echo "Review with: scripts/control_plane_upstream.sh diff"
      echo "Do NOT merge or rebase; cherry-pick under review."
    fi
    ;;

  diff)
    ensure_remote
    target="${2:-$remote_name/main}"
    mapfile -t paths < <(kept_paths)
    git -C "$repo_root" diff --stat "$pinned_commit" "$target" -- "${paths[@]}"
    echo
    echo "Full diff: git diff $pinned_commit $target -- <path>"
    ;;

  files)
    kept_paths
    ;;

  *)
    echo "usage: $0 {status|diff [commit]|files}" >&2
    exit 2
    ;;
esac
