#!/usr/bin/env bash
# Keep the Devin CLI running inside tmux and send it instructions without restarting it.
#
#   devin-tmux.sh start <name> <work folder> [devin args...]   e.g. start docs ~/src/my-app -r <devin-session-name>
#   devin-tmux.sh send  <name> [instruction file|-]             stdin when omitted or -. Multi-line text goes in as one bracketed paste
#   devin-tmux.sh wait  <name> [seconds=540]                    prints only the state on line 1 and exits with a code per state
#                                                               TURN_END=0 PERMISSION_PROMPT=10 EXITED=11 STILL_RUNNING=12
#   devin-tmux.sh read  <name> [lines=40]                       prints the end of the screen
#   devin-tmux.sh keys  <name> <key...>                         answers a permission prompt and the like (e.g. keys docs Enter)
#   devin-tmux.sh log   <name>                                  prints the path of the log file
#   devin-tmux.sh view  <name>                                  keeps redrawing the screen for the user, without attaching as a tmux client
#   devin-tmux.sh stop  <name>
#
# Why not resume with --prompt-file: every confirmation in that turn is rejected as NonInteractive.
# Sent as ordinary input, the permission prompt appears and can be answered with keys.
set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/devin-tmux"
LOG_DIR="$HOME/.local/share/devin/cli/logs"

die() { echo "devin-tmux: $*" >&2; exit 2; }

session() { echo "devin-$1"; }

# The real log belongs to the child `devin acp`. Find it from the child pids of devin in the pane
log_file() {
  local pane_pid c f
  pane_pid=$(tmux display -p -t "$(session "$1")" '#{pane_pid}')
  for c in $(pgrep -P "$pane_pid" 2>/dev/null); do
    f=$(ls "$LOG_DIR"/*_"$c".log 2>/dev/null | head -1 || true)
    [ -n "$f" ] && { echo "$f"; return 0; }
  done
  return 1
}

# load_session lines are the reload on resume, not the end of a turn
count() { grep "$2" "$1" 2>/dev/null | grep -vc load_session || true; }

# send-keys from outside targets a client attached to the session. When that client is read-only
# (tmux attach -r), it fails with "client is read-only", so attach a writable control-mode client just
# for the send. paste-buffer would get through, but Devin does not treat pasted key sequences as key presses.
# The control-mode client parses its input as tmux commands, so only plain key names may reach it.
press() {
  local k
  for k in "$@"; do
    [[ $k =~ ^[A-Za-z0-9_+-]+$ ]] || die "keys takes key names such as Enter, Down or C-c; send text with send: $k"
  done
  if tmux list-clients -t "$s" -F '#{client_readonly}' | grep -q 1; then
    printf 'send-keys -t %s %s\n' "$s" "$*" | tmux -C attach -t "$s" >/dev/null
  else
    tmux send-keys -t "$s" "$@"
  fi
}

cmd=${1:?usage: devin-tmux.sh start|send|wait|read|keys|log|view|stop <name> ...}
name=${2:?name required}
shift 2
# The name goes into tmux targets and into the folder that stop deletes, so ., :, / and .. must not get through
[[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || die "a name may contain only letters, digits, - and _: $name"
s=$(session "$name")
mkdir -p -m 700 "$STATE_DIR"

case "$cmd" in
  start)
    dir=${1:?work folder required}; shift
    # Given devin and its arguments separately, tmux runs them without a shell, so no argument is parsed again
    tmux new-session -d -s "$s" -x 220 -y 50 -c "$dir" devin "$@"
    sleep 8
    tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -15
    echo "--- If a trust prompt or a theme picker is open, close it with keys before you send"
    ;;
  send)
    # Keep each instruction in a folder per name. A shared temp file chosen by the caller would let parallel agents overwrite each other
    dir="$STATE_DIR/$name"; mkdir -p -m 700 "$dir"
    n=$(( $(ls "$dir" | wc -l) + 1 )); file="$dir/prompt-$n.md"
    if [ "${1:--}" = - ]; then cat > "$file"; else cp "$1" "$file"; fi
    L=$(log_file "$name")
    count "$L" "turn_stats: close" > "$STATE_DIR/$name.turns"
    count "$L" "prompting user" > "$STATE_DIR/$name.prompts"
    tmux load-buffer -b "dt-$name" "$file"
    tmux paste-buffer -p -d -b "dt-$name" -t "$s"
    sleep 1
    press Enter
    echo "sent: $file"
    ;;
  wait)
    limit=${1:-540}
    # Arithmetic would evaluate anything else as an expression
    [[ $limit =~ ^[0-9]+$ ]] || die "seconds must be a whole number: $limit"
    L=$(log_file "$name")
    t0=$(cat "$STATE_DIR/$name.turns" 2>/dev/null || echo 0)
    p0=$(cat "$STATE_DIR/$name.prompts" 2>/dev/null || echo 0)
    end=$((SECONDS + limit))
    while [ $SECONDS -lt $end ]; do
      tmux has-session -t "$s" 2>/dev/null || { echo "EXITED"; exit 11; }
      [ "$(count "$L" "turn_stats: close")" -gt "$t0" ] && { echo "TURN_END"; exit 0; }
      [ "$(count "$L" "prompting user")" -gt "$p0" ] && {
        echo "PERMISSION_PROMPT"; tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -20; exit 10; }
      sleep 5
    done
    echo "STILL_RUNNING"; exit 12
    ;;
  read)
    lines=${1:-40}
    [[ $lines =~ ^[0-9]+$ ]] || die "lines must be a whole number: $lines"
    tmux capture-pane -p -J -S -2000 -t "$s" | grep -v '^\s*$' | tail -"$lines"
    ;;
  keys)
    press "$@"
    # Move the baseline to the current count, so the next wait does not return at once for the prompt just answered.
    # The trust prompt comes before any log exists, so don't fail keys there
    if L=$(log_file "$name"); then count "$L" "prompting user" > "$STATE_DIR/$name.prompts"; fi
    ;;
  log)  log_file "$name" ;;
  view)
    # Attaching with tmux attach -r would make send-keys fail, so copy the screen without becoming a client
    while tmux has-session -t "$s" 2>/dev/null; do
      screen=$(tmux capture-pane -p -e -t "$s" | grep -v '^\s*$' | tail -n "$(( $(tput lines 2>/dev/null || echo 40) - 1 ))" || true)
      printf '\033[H\033[2J%s\n' "$screen"
      sleep 2
    done
    echo "devin-$name has exited"
    ;;
  stop) tmux kill-session -t "$s"; rm -r "${STATE_DIR:?}/$name" 2>/dev/null || true; rm -f "$STATE_DIR/$name".* ;;
  *) die "unknown command: $cmd" ;;
esac
