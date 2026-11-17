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
# Modes that run commands without asking. In tests on Devin CLI 3000.12.57 and 3000.13.4, smart ran git commit,
# git push and rm unasked, although its docs say these always ask; the others approve every call. Claude Code's
# permission rules and hooks never see what Devin runs, so this script is the last place to stop an agent picking them
UNSAFE_MODES='smart|bypass|yolo|dangerous|autonomous'

die() { echo "devin-tmux: $*" >&2; exit 2; }

session() { echo "devin-$1"; }

# The real log belongs to the child `devin acp`. Find it from the child pids of devin in the pane.
# With the shared daemon (the default on Devin CLI 3000.13.4), the turn ends go to the daemon's log, whose
# lines name no session, so counting them would pick up the turns of every other devin on it. Refuse
# rather than wait on a log that never changes. The daemon itself can be a child too, of the devin that started it
log_file() {
  local pane_pid c f
  tmux has-session -t "$(session "$1")" 2>/dev/null || die "devin-$1 is not running; start it with start"
  pane_pid=$(tmux display -p -t "$(session "$1")" '#{pane_pid}')
  for c in $(pgrep -P "$pane_pid" 2>/dev/null); do
    case $(ps -o command= -p "$c" 2>/dev/null) in
      *" daemon run"*) continue ;;
      *--process-model=daemon*)
        die "devin-$1 runs its agent in the shared Devin daemon, so the end of its turns cannot be told apart from other sessions'. Stop it with stop and start it again with start (add -r <Devin session name> to resume): start runs Devin with --process-model in-process" ;;
    esac
    f=$(ls "$LOG_DIR"/*_"$c".log 2>/dev/null | head -1 || true)
    [ -n "$f" ] && { echo "$f"; return 0; }
  done
  die "found no log of the child devin acp of devin-$1 in $LOG_DIR; if Devin has just started, try again in a few seconds"
}

# load_session lines are the reload on resume, not the end of a turn
count() { grep "$2" "$1" 2>/dev/null | grep -vc load_session || true; }

# A prompt is read from the screen, not counted in the log: for parallel tool calls the log records every
# prompt at once while the screen shows them one at a time, so the prompts after the first add no log line.
# A menu waiting for an answer ends with a footer such as "↑↓ select · ↵ confirm · esc cancel". Only the
# ASCII words are matched, so the locale does not matter
prompt_on_screen() { tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -3 | grep -q 'confirm.*esc cancel'; }

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

# A menu (a permission prompt, the trust prompt, the command editor) ends the screen with its key hints.
# Text pasted into it would be taken as key presses
menu_open() { tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -1 | grep -q 'esc cancel\|esc back'; }

# The status line ends the screen: the model, then " · " and the mode ("SWE-2 High · accept edits"), or the
# mode first for plan ("Plan · SWE-2 High"). A Fusion model name has " · " in it too, so match whole parts.
# Hints on the right follow a run of spaces. Prints the unsafe mode, if any
unsafe_mode_on_screen() {
  local left re=" · ($UNSAFE_MODES) · "
  left=$(tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -1 | sed 's/   .*//')
  [[ " · $left · " =~ $re ]] && echo "${BASH_REMATCH[1]}"
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
    mode=${DEVIN_PERMISSION_MODE:-}; prev=
    for a in "$@"; do
      [ "$prev" = --permission-mode ] && mode=$a
      case $a in --permission-mode=*) mode=${a#*=} ;; esac
      prev=$a
    done
    [[ $mode =~ ^($UNSAFE_MODES)$ ]] && die "permission mode $mode runs commands without asking (smart ran git commit, push and rm in tests); use accept-edits"
    # In-process keeps the agent's log to this devin alone; see log_file. Devin rejects the option given twice
    case " $* " in *" --process-model"[\ =]*) ;; *) set -- "$@" --process-model in-process ;; esac
    # Given devin and its arguments separately, tmux runs them without a shell, so no argument is parsed again
    tmux new-session -d -s "$s" -x 220 -y 50 -c "$dir" devin "$@"
    sleep 8
    tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -15
    echo "--- If a trust prompt or a theme picker is open, close it with keys before you send"
    ;;
  send)
    # First, since behind the trust prompt the child that writes the log has not started yet
    menu_open && die "a prompt or menu is open in devin-$name; answer or close it with keys first"
    # Before saving the instruction, so that no copy is left behind when Devin cannot receive it
    L=$(log_file "$name")
    # Pasted text that ends in a newline goes to the agent as a message, so a slash command such as
    # /accept-edits from a heredoc would not run. $(...) drops the trailing newlines
    if [ "${1:--}" = - ]; then text=$(cat); else text=$(cat "$1"); fi
    re="^/(mode[[:space:]]+)?($UNSAFE_MODES)([[:space:]]|\$)"
    [[ $text =~ $re ]] && die "refusing to switch devin-$name to ${BASH_REMATCH[2]} mode, which runs commands without asking"
    # Switching back to a safe mode is what the refusal below asks for, so it gets through
    re='^/(mode[[:space:]]+)?(accept-edits|normal|plan)([[:space:]]|$)'
    if ! [[ $text =~ $re ]]; then
      m=$(unsafe_mode_on_screen || true)
      [ -z "$m" ] || die "devin-$name runs in $m mode, which runs commands without asking; send /accept-edits first"
    fi
    # Keep each instruction in a folder per name. A shared temp file chosen by the caller would let parallel agents overwrite each other
    dir="$STATE_DIR/$name"; mkdir -p -m 700 "$dir"
    n=$(( $(ls "$dir" | wc -l) + 1 )); file="$dir/prompt-$n.md"
    printf '%s' "$text" > "$file"
    count "$L" "turn_stats: close" > "$STATE_DIR/$name.turns"
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
    # Devin may have exited since the last call; that is EXITED too, not an error from log_file
    tmux has-session -t "$s" 2>/dev/null || { echo "EXITED"; exit 11; }
    L=$(log_file "$name")
    t0=$(cat "$STATE_DIR/$name.turns" 2>/dev/null || echo 0)
    end=$((SECONDS + limit))
    while [ $SECONDS -lt $end ]; do
      tmux has-session -t "$s" 2>/dev/null || { echo "EXITED"; exit 11; }
      [ "$(count "$L" "turn_stats: close")" -gt "$t0" ] && { echo "TURN_END"; exit 0; }
      # A menu just answered with keys can stay drawn for a moment, so count only one still there 2 seconds later
      if prompt_on_screen && sleep 2 && prompt_on_screen; then
        echo "PERMISSION_PROMPT"; tmux capture-pane -p -t "$s" | grep -v '^\s*$' | tail -20; exit 10
      fi
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
    for k in "$@"; do
      [[ $k =~ ^[0-9]+$ ]] && die "number keys answer a permission prompt at once, with the scope of that number (3 Project and 4 Always save the rule in a config file); move with Down and press Enter on its own"
      case $k in
        Enter|C-m|KPEnter)
          # Checked on the screen before the keys go in, so Enter must come on its own after any move
          [ $# -eq 1 ] || die "send Enter on its own, after checking the highlighted row with read"
          tmux capture-pane -p -t "$s" | grep -q '^[[:space:]]*❭[[:space:]]*Bypass mode' &&
            die "Bypass mode is highlighted; it would approve every later call without asking. Move to Allow or Deny first" ;;
      esac
    done
    press "$@"
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
