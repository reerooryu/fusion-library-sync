#!/usr/bin/env bash
# Tag every shipped state and publish it as a GitHub release.
#
# Release notes come from CHANGELOG.md, so there is one source of truth: each
# release body is that version's section, verbatim. Edit the changelog, not
# this script.
#
# Safe to re-run. Existing tags and releases are left alone, and a tag that
# already points somewhere unexpected is reported rather than moved.
#
#   ./scripts/publish.sh          show what would happen
#   ./scripts/publish.sh --go     do it

set -euo pipefail
cd "$(dirname "$0")/.."

DRY=1
RETAG=0
for a in "$@"; do
  case "$a" in
    --go) DRY=0 ;;
    # Tags pushed before a history rewrite still point into the old, orphaned
    # commits - which is also what keeps GitHub from ever collecting them.
    # --retag moves them onto the rewritten history and force-pushes.
    --retag) RETAG=1 ;;
    *) echo "unknown option: $a"; exit 1 ;;
  esac
done

# tag<TAB>commit subject<TAB>prerelease
#
# Commits are found by SUBJECT, not SHA. Reconciling this repo with GitHub
# rewrites every SHA, and a map of hashes would quietly point at abandoned
# commits after the first rebase. Subjects survive rebase, cherry-pick and
# merge. They are matched exactly, against the current branch only.
#
# "HEAD" means whatever is checked out - so run this from the release commit.
MAP=$(cat <<'MAPEOF'
v0.1.0	chore: version 0.1	yes
v0.1.1	fix(github): stream tarball download with progress and cancellation	yes
v0.1.2	fix(plan): use git-style glob semantics for include patterns	yes
v0.1.3	fix(ui): distinguish download and upload progress phases	yes
v0.1.4	fix(datapanel): fire uploads without blocking on uploadState	yes
v0.1.5	perf(sync): list each folder once per settle pass	yes
v0.2.0	test(datapanel): cover upload state transitions	yes
v0.2.1	perf(sync): scan folders before polling upload futures	yes
v0.2.2	fix(ui): populate renamed report and bind folder input to source	yes
v0.2.3	fix(manifest): stamp ref, commit and timestamp on every sync	yes
v0.3.0	feat(sync): detect manifest entries missing from the Data Panel	yes
v0.3.1	HEAD	no
MAPEOF
)

say() { printf '%s\n' "$*"; }

# A prerelease badge is easy to miss on a list of twelve. Broken releases say
# so in the title, where nobody can scroll past it.
label() {  # $1 tag, $2 title, $3 prerelease
  if [ "$3" = "yes" ]; then printf '%s (broken) — %s' "$1" "$2"
  else printf '%s — %s' "$1" "$2"; fi
}
run() { if [ "$DRY" = 1 ]; then say "  would: $*"; else "$@"; fi; }

if ! command -v gh >/dev/null; then
  if [ "$DRY" = 0 ]; then
    say "gh CLI not found. Install it (brew install gh) and gh auth login."
    exit 1
  fi
  say "(gh not installed here - release steps are shown, not checked)"
fi
git rev-parse --verify HEAD >/dev/null

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Pull one version's heading and body out of CHANGELOG.md into $WORK/{title,body}.
# The body goes through a file, never a shell variable - it is arbitrary markdown.
section() {
  python3 - "$1" "$WORK" <<'PYEOF'
import os, re, sys
tag, work = sys.argv[1], sys.argv[2]
text = open("CHANGELOG.md", encoding="utf-8").read()
pattern = r"^## %s(?: — ([^\n]*))?$(.*?)(?=^## |\Z)" % re.escape(tag)
m = re.search(pattern, text, re.M | re.S)
if not m:
    sys.exit("no CHANGELOG.md section for %s" % tag)
with open(os.path.join(work, "title"), "w", encoding="utf-8") as fh:
    fh.write((m.group(1) or "").strip())
with open(os.path.join(work, "body"), "w", encoding="utf-8") as fh:
    fh.write(m.group(2).strip() + "\n")
PYEOF
}

# Exactly one commit on this branch with this subject, or refuse to guess.
resolve() {
  if [ "$1" = "HEAD" ]; then git rev-parse --verify HEAD; return; fi
  local hits
  hits=$(git log HEAD --format='%H%x09%s' | awk -F'\t' -v s="$1" '$2==s {print $1}')
  local n; n=$(printf '%s' "$hits" | grep -c . || true)
  if [ "$n" != "1" ]; then
    say "  !! $n commits match subject: $1" >&2
    return 1
  fi
  printf '%s' "$hits"
}

say "== tags"
MISSING=0
MOVED=0
while IFS=$'\t' read -r tag commit pre; do
  [ -z "$tag" ] && continue
  if ! sha=$(resolve "$commit"); then MISSING=1; continue; fi
  section "$tag"; title=$(cat "$WORK/title"); text=$(label "$tag" "$title" "$pre")
  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    have=$(git rev-parse "refs/tags/$tag^{commit}")
    if [ "$have" != "$sha" ]; then
      if [ "$RETAG" = 1 ]; then
        say "  ->  $tag  ${have:0:7} => ${sha:0:7}  $text"
        run git tag -f -a "$tag" "$sha" -m "$text"
        MOVED=1
      else
        say "  !! $tag exists at ${have:0:7}, expected ${sha:0:7} - NOT moved"
        say "     (--retag moves it; without that the old commits stay alive)"
      fi
    else
      say "  ok $tag -> ${sha:0:7}"
    fi
    continue
  fi
  say "  +  $tag -> ${sha:0:7}  $text"
  run git tag -a "$tag" "$sha" -m "$text"
done <<< "$MAP"

if [ "$MISSING" != "0" ]; then
  say ""
  say "Some subjects did not resolve. Reconcile history first; nothing pushed."
  exit 1
fi

say ""
say "== push"
run git push origin HEAD
if [ "$MOVED" = 1 ]; then
  # Moved tags need force; a plain push silently leaves the remote's old one.
  run git push --force origin --tags
else
  run git push origin --tags
fi

say ""
say "== releases"
while IFS=$'\t' read -r tag _commit pre; do
  [ -z "$tag" ] && continue
  section "$tag"; title=$(cat "$WORK/title"); text=$(label "$tag" "$title" "$pre")
  exists=no
  command -v gh >/dev/null && gh release view "$tag" >/dev/null 2>&1 && exists=yes

  if [ "$pre" = "yes" ]; then create_flag="--prerelease"; edit_flag="--prerelease=true"
  else create_flag="--latest"; edit_flag="--prerelease=false"; fi

  if [ "$exists" = yes ]; then
    # CHANGELOG.md is the source of truth, so an existing release is brought
    # into line with it rather than left carrying whatever it shipped with.
    say "  ~  $tag  update  \"$text\"  $edit_flag"
    if [ "$DRY" = 0 ]; then
      gh release edit "$tag" --title "$text" \
        --notes-file "$WORK/body" $edit_flag
    fi
  else
    say "  +  $tag  create  \"$text\"  $create_flag"
    if [ "$DRY" = 0 ]; then
      gh release create "$tag" --title "$text" \
        --notes-file "$WORK/body" $create_flag
    fi
  fi
done <<< "$MAP"

say ""
if [ "$DRY" = 1 ]; then
  say "Dry run. Re-run with --go to apply."
else
  say "Done. Check: gh release list"
fi
