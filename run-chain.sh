#!/usr/bin/env bash
# run-chain.sh - run a chain of dev atoms, one at a time, committing each.
#
# Usage:  run-chain.sh <chain-file> [tag ...]
#
# The chain file opens with two declarations - which repositories it spans and
# which branch they must be on - and then has one atom per line, four
# TAB-separated fields:
#
#     tag <TAB> brief-path <TAB> allowed-paths <TAB> subject
#
# `allowed-paths` is a COMMA-separated list of repo-relative paths the atom is
# allowed to touch, each prefixed `phs:` or `sdk:`. It is not decoration:
#
#   * only those paths are staged, so a parallel edit by the maintainer in some
#     other file cannot be swept into the atom's commit. That happened twice in
#     the PHS-522 line with `git add -A` and produced commits whose message
#     described something other than their diff;
#   * anything the run changed OUTSIDE them stops the chain. An atom straying
#     past its declared scope is a finding, not something to commit quietly.
#
# One run at a time, always: an architecture run and a dev run cannot share the
# tree, and two dev runs cannot either.
#
# The chain does NOT build and does NOT run gates. That is the maintainer's
# single pass at the end, which is where this project has found most of its
# gate defects anyway.
#
# CONTINUING A STOPPED ATOM. Normally every atom must start on a clean tree:
# leftovers mean the previous atom is not committed. When an atom stopped with
# its work still in the tree - for example a run that ended waiting on a
# decision - and the maintainer wants it redone with a sharper brief ON THAT
# CODE, set CHAIN_CONTINUE_DIRTY=1 and name the atoms to run, the stopped one
# first. Only the FIRST atom may then start dirty, and only if every changed
# path is inside that atom's declared paths; anything else still stops the
# chain. Every later atom requires a clean tree as before.
set -uo pipefail

# The workspace and the repositories it chains over. Defaults match this
# project's layout; both are overridable so the script carries no knowledge of
# one deployment. CHAIN_REPOS is "alias:relative-path" pairs, and the alias is
# what an atom's declared paths are prefixed with.
ROOT="${CHAIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
declare -A REPO=()
for r in "${!REPO[@]}"; do
    [[ -d "${REPO[$r]}/.git" ]] || { echo "not a git repository: ${REPO[$r]} (alias $r)" >&2; exit 2; }
done

CHAIN="${1:?usage: run-chain.sh <chain-file> [tag ...]}"; shift
[[ -r "$CHAIN" ]] || { echo "cannot read chain file: $CHAIN" >&2; exit 2; }

# WHICH REPOSITORIES, declared by the chain file and not by this script, so the
# tool carries no knowledge of any one project:
#
#     #repos: phs:dataplane-phs-ng sdk:dataplane-sdk
#
# Each alias is what an atom's declared paths are prefixed with. CHAIN_REPOS
# overrides the line; one of the two must be present.
repos="${CHAIN_REPOS:-$(sed -n 's|^#repos:[[:space:]]*||p' "$CHAIN" | head -1)}"
[[ -n "$repos" ]] || { echo "$CHAIN declares no '#repos:' line and CHAIN_REPOS is unset" >&2; exit 2; }
for pair in $repos; do
    [[ "$pair" == *:* ]] || { echo "malformed repo pair '$pair', want alias:path" >&2; exit 2; }
    REPO[${pair%%:*}]="$ROOT/${pair#*:}"
done

# WHICH BRANCH, also declared by the chain file:
#
#     #branch: feat/phs-ct-1e1-lane-directory
#
# Every repository the chain touches must already be on it, and the runner
# refuses to start otherwise. A chain is a long unattended sequence of commits;
# discovering afterwards that they landed on the wrong branch costs a rewrite
# of history that a pushed branch may not allow at all. The runner does NOT
# create the branch - deciding where work belongs is not a thing to automate -
# it only refuses to proceed if the answer is not already in place.
branch="$(sed -n 's|^#branch:[[:space:]]*||p' "$CHAIN" | head -1)"
if [[ -n "$branch" ]]; then
    for r in "${!REPO[@]}"; do
        on="$(git -C "${REPO[$r]}" rev-parse --abbrev-ref HEAD)"
        [[ "$on" == "$branch" ]] || {
            echo "${REPO[$r]} is on '$on', the chain declares '$branch'" >&2
            echo "create or check it out first; this runner will not do it for you" >&2
            exit 2
        }
    done
fi

NAME=$(basename "$CHAIN" .chain)
OUT="${CHAIN_OUT:-$(cd "$(dirname "$CHAIN")" && pwd)}"
LOG="$OUT/$NAME-chain.log"
STATE="$OUT/$NAME-chain.state"
mkdir -p "$OUT"

say() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }

mapfile -t ALL < <(grep -vE '^\s*(#|$)' "$CHAIN")
if [[ "$#" -eq 0 ]]; then
    ATOMS=("${ALL[@]}")
else
    ATOMS=()
    for want in "$@"; do
        hit=""
        for a in "${ALL[@]}"; do [[ "${a%%	*}" == "$want" ]] && { ATOMS+=("$a"); hit=1; }; done
        [[ -n "$hit" ]] || { echo "unknown atom: $want" >&2; exit 2; }
    done
fi

set -a; . ~/.config/orchestration/deepseek.env; . ~/.config/orchestration/gemini.env; set +a
[[ -n "${DEEPSEEK_API_KEY:-}" && -n "${GEMINI_API_KEY:-}" ]] \
    || { echo "a provider key is missing - the arbiter would abort mid-chain" >&2; exit 2; }

: > "$STATE"
say "chain $NAME: ${#ATOMS[@]} atom(s)"

# Count, and log, the changed paths that lie outside an atom's declared list.
outside_paths() {
    local tag="$1" allowed="$2" r want path a ok n=0
    for r in "${!REPO[@]}"; do
        want=$(printf '%s\n' "${allowed//,/$'\n'}" | sed -n "s|^$r:||p")
        while read -r _ path; do
            [[ -n "$path" ]] || continue
            ok=0
            while read -r a; do [[ -n "$a" ]] || continue; [[ "$path" == "$a" || "$path" == "$a"/* ]] && ok=1; done <<< "$want"
            if [[ "$ok" -eq 0 ]]; then
                say "$tag: OUTSIDE its declared paths: $r:$path"
                n=$((n+1))
            fi
        done < <(git -C "${REPO[$r]}" status --porcelain | sed 's/^...//;s/^/x /')
    done
    return "$n"
}

first=1
for entry in "${ATOMS[@]}"; do
    IFS=$'\t' read -r tag brief allowed subject <<< "$entry"

    dirty=0
    for r in "${!REPO[@]}"; do dirty=$(( dirty + $(git -C "${REPO[$r]}" status --porcelain | wc -l) )); done
    if [[ "$dirty" -ne 0 ]]; then
        if [[ "$first" -eq 1 && "${CHAIN_CONTINUE_DIRTY:-0}" == "1" ]] && outside_paths "$tag" "$allowed"; then
            say "$tag: continuing on a dirty tree ($dirty path(s), all inside its declared paths) - CHAIN_CONTINUE_DIRTY=1"
        else
            say "$tag: STOP - the tree is dirty ($dirty path(s)); the previous atom is not committed"
            echo -e "$tag\tDIRTY_TREE" >> "$STATE"; break
        fi
    fi
    first=0

    [[ -r "$ROOT/$brief" ]] || { say "$tag: STOP - no brief at $brief"; echo -e "$tag\tNO_BRIEF" >> "$STATE"; break; }

    say "$tag: starting ($brief)"
    ( cd "$ROOT" && python3 orchestration/orchestrate.py dev --task-file "$brief" ) >> "$LOG" 2>&1
    rc=$?
    run=$(ls -1dt "$ROOT"/orchestration-project/runs/*development* 2>/dev/null | head -1)
    status=$(head -1 "$run/STATUS" 2>/dev/null || echo NO_STATUS)
    say "$tag: run finished rc=$rc status=$status"

    # What the run actually changed, per repo.
    outside_paths "$tag" "$allowed"; stray=$?
    if [[ "$stray" -ne 0 ]]; then
        say "$tag: STOP - $stray path(s) outside the atom's declared scope; nothing committed, decide by hand"
        echo -e "$tag\tSTRAY_PATHS\t$status" >> "$STATE"; break
    fi

    changed=0
    for r in "${!REPO[@]}"; do changed=$(( changed + $(git -C "${REPO[$r]}" status --porcelain | wc -l) )); done
    if [[ "$changed" -eq 0 ]]; then
        say "$tag: no changes - nothing to commit"
        echo -e "$tag\tNO_CHANGES\t$status" >> "$STATE"; continue
    fi

    for r in "${!REPO[@]}"; do
        repo="${REPO[$r]}"
        [[ -n "$(git -C "$repo" status --porcelain)" ]] || continue
        while read -r a; do
            [[ -n "$a" ]] || continue
            git -C "$repo" add -- "$a" 2>/dev/null
        done < <(printf '%s\n' "${allowed//,/$'\n'}" | sed -n "s|^$r:||p")
        [[ -n "$(git -C "$repo" diff --cached --name-only)" ]] || continue
        git -C "$repo" commit -q -m "$tag: $subject

Committed by the chain runner, which neither builds nor runs a gate: the
maintainer does that in one pass at the end of the chain. Run status:
$status. Verification of this atom is therefore OUTSTANDING and is the
maintainer's, not this commit's claim.

Only the atom's declared paths were staged, so nothing edited in parallel
outside them can be in this diff."
        say "$tag: committed in $(basename "$repo") -> $(git -C "$repo" log --oneline -1 | cut -c1-64)"
    done
    echo -e "$tag\tCOMMITTED\t$status" >> "$STATE"
done

say "CHAIN FINISHED"
cat "$STATE" | tee -a "$LOG"
