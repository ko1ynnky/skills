---
name: devin-tmux
description: Keep the Devin CLI (`devin`) running inside tmux, so an agent can send it follow-up instructions without restarting it, read its screen, wait for the end of a turn or a permission prompt, and answer that prompt. Works from Claude Code and Codex, in any folder. Use it whenever you hand work to the Devin CLI or continue it — phrases like "Devin CLI に指示して", "Devin に続きを頼んで", "Devin のセッション <名前> を再開して", "ask the Devin CLI to …", "continue Devin session <name>". Use it instead of restarting Devin with `devin -r … --prompt-file` for every instruction.
---

# devin-tmux

An agent's terminal tool cannot type into a program that is already running. Run Devin inside tmux, and the agent can send it input and read its screen through `tmux`. Do everything through `scripts/devin-tmux.sh` under this skill's base directory (called `dt` below; call it by its absolute path).

## Steps

1. Start, once per task. The tmux session is named `devin-<name>`; a name takes letters, digits, `-` and `_`.
   - New: `dt start <name> <work folder> --model swe-2-high`
   - Resume: `dt start <name> <work folder> -r <Devin session name>` (the names are in the `sessions` table of `~/.local/share/devin/cli/sessions.db`)
   - If the output shows "Do you trust the authors of this directory?" or a theme picker, close it first with `dt keys <name> Enter` or similar. While it stays open, permission prompts are rejected automatically.
   - Then let the user watch Devin. In the Claude Code desktop app, run `<this skill's base directory>/scripts/devin-tmux.sh view <name>` in a Terminal tab (`run_in_terminal`, with a title such as `Devin <name> (view)`, and with `cwd` set to the absolute path of this session's working folder: without it, the app fills `cwd` in, and the auto mode classifier sends the call back once because its input changed), then show it with `show_pane` (`pane: "terminal"` and that `tab_id`). `view` redraws the screen every 2 seconds; typing in that tab does not reach Devin. Where you cannot open a terminal tab (Codex, for example), give the user this command.
2. Send: pass the instruction on stdin (`dt send <name> <<'EOF'` … `EOF`). The script keeps a copy at `~/.local/state/devin-tmux/<name>/prompt-<n>.md` before pasting it, so don't choose a temp file name yourself (parallel agents would choose the same one and overwrite each other). If the instruction already lives in a file you keep, `dt send <name> <file>` works too. It goes in as a bracketed paste, so multi-line text is not submitted halfway.
3. Wait: `dt wait <name> [seconds]` (default 540, to stay inside a 10-minute tool timeout). Line 1 is the state alone. Branch on the exit code (`dt wait x; case $? in 0) …;; 10) …;; esac`), never on the whole output. On a permission prompt, the screen follows from line 2.
   - `TURN_END` (0): the turn ended. Read the answer with `dt read <name> 80`, and check the files and diffs it produced yourself.
   - `PERMISSION_PROMPT` (10): waiting for permission; the end of the screen follows. If the operation is within what you were asked to do, answer with `dt keys <name> Enter` (follow the choices on screen, moving with `Down` and the like). Otherwise tell the user.
   - `STILL_RUNNING` (12): still working. Run `dt wait` again.
   - `EXITED` (11): Devin exited.
4. For follow-up instructions, repeat 2 and 3. Don't restart Devin.
5. When done, run `dt stop <name>`. It also deletes the saved instructions.

To work alongside, the user can run `tmux attach -t devin-<name>` in a terminal and share the same screen (detach with `Ctrl-b d`).

## Notes

- From Codex, the sandbox may refuse to connect to the tmux socket (`/private/tmp/tmux-<uid>/`). If it does, run with escalated permissions or ask the user to allow it. Don't look for another way to run Devin.
- In a turn resumed with `devin -r … --prompt-file …`, every operation that needs confirmation (git commands that change things, installing packages, …) is rejected automatically with `NonInteractive("User skipped this tool call")`. Sent as ordinary input through tmux, it shows the permission prompt instead.
- After a resume, a bypass mode saved in that session can win even over `--permission-mode smart`. Check the status line with `dt read` (for example `SWE-2 High · bypass`). If it is bypass, write in the instruction where Devin must stop (before a commit or a push, for example).
- The end of a turn is detected from the log (`dt log <name>`; the file is the child `devin acp`'s `~/.local/share/devin/cli/logs/devin_<datetime>_<pid>.log`) by the line `chisel_core::turn_stats: close`. Lines with `load_session` are the reload on resume and don't count. These log lines are not a documented interface and may change in a new Devin CLI version; if `wait` never returns `TURN_END`, check them first.
- If you watch the log yourself, don't put `cut` in the pipe: it holds lines back instead of passing each one on, so no notification arrives. To shorten lines, use `awk '{print substr($0,1,200); fflush()}'`.
- To only watch, use `dt view`, not `tmux attach -r`. While a read-only client is attached, the window shrinks to its size (and stays shrunk after it leaves), and `tmux send-keys` from outside fails with "client is read-only". `dt send` and `dt keys` go through a control-mode client, so they still work. Key sequences such as arrows pasted with `paste-buffer` are not treated as key presses by Devin.
- `dt keys` takes key names only (`Enter`, `Down`, `C-c`, …). Send text with `dt send`.
- Don't open the same Devin session in two processes. If it is running in another tab, stop only that process before `dt start`. Other Devin sessions may be running at the same time, so don't stop them all with something like `pkill devin`.
- Choose Allow on a permission prompt only for operations within what you were asked to do. Pushes, pull requests, posts to outside services and the like follow the project's approval rules.
