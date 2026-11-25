---
name: devin-tmux
description: Keep the Devin CLI (`devin`) running inside tmux, so an agent can send it follow-up instructions without restarting it, read its screen, wait for the end of a turn or a permission prompt, and answer that prompt. Works from Claude Code and Codex, in any folder. Use it whenever you hand work to the Devin CLI or continue it — phrases like "Devin CLI に指示して", "Devin に続きを頼んで", "Devin のセッション <名前> を再開して", "ask the Devin CLI to …", "continue Devin session <name>". Use it instead of restarting Devin with `devin -r … --prompt-file` for every instruction.
---

# devin-tmux

An agent's terminal tool cannot type into a program that is already running. Run Devin inside tmux, and the agent can send it input and read its screen through `tmux`. Do everything through `scripts/devin-tmux.sh` under this skill's base directory (called `dt` below; call it by its absolute path).

## Steps

1. Start, once per task. The tmux session is named `devin-<name>`; a name takes letters, digits, `-` and `_`.
   - New: `dt start <name> <work folder> --permission-mode accept-edits`. Without `--model`, `start` picks the newest free SWE model at high effort (see "Choosing a model").
   - Resume: `dt start <name> <work folder> -r <Devin session name>` (the names are in the `sessions` table of `~/.local/share/devin/cli/sessions.db`). A resumed session ignores `--permission-mode` (see Notes). After `start`, read the status line with `dt read <name> 3`; if it does not end in `· accept edits`, send `/accept-edits` with `dt send` before the instruction.
   - Give the folder the work happens in (the worktree, for example), not a parent folder. Devin then reads that repository's `AGENTS.md`, and `accept-edits` covers edits inside it only. It reads its settings only from that folder, though (see Notes).
   - `--permission-mode accept-edits` lets file edits inside the work folder run without a prompt, while every shell command still asks. Without it, each edit asks too. `start` refuses `smart`, `bypass`, `dangerous` and `autonomous`: in tests on Devin CLI 3000.12.57 and 3000.13.4, `git commit`, `git push` and `rm` ran in `smart` mode without asking, although the docs say these always ask, and the others approve every call.
   - `start` adds `--process-model in-process` unless you pass `--process-model` yourself. Don't pass `daemon`: `wait` cannot see the end of a turn that runs in the shared daemon (see Notes).
   - If the output shows "Do you trust the authors of this directory?" or a theme picker, close it first with `dt keys <name> Enter` or similar. While it stays open, permission prompts are rejected automatically.
   - Then let the user watch Devin. In the Claude Code desktop app, run `<this skill's base directory>/scripts/devin-tmux.sh view <name>` in a Terminal tab (`run_in_terminal`, with a title such as `Devin <name> (view)`, and with `cwd` set to the absolute path of this session's working folder: without it, the app fills `cwd` in, and the auto mode classifier sends the call back once because its input changed), then show it with `show_pane` (`pane: "terminal"` and that `tab_id`). `view` redraws the screen every 2 seconds; typing in that tab does not reach Devin. Where you cannot open a terminal tab (Codex, for example), give the user this command.
2. Send: pass the instruction on stdin (`dt send <name> <<'EOF'` … `EOF`). The script keeps a copy at `~/.local/state/devin-tmux/<name>/prompt-<n>.md` before pasting it, so don't choose a temp file name yourself (parallel agents would choose the same one and overwrite each other). If the instruction already lives in a file you keep, `dt send <name> <file>` works too. It goes in as a bracketed paste, so multi-line text is not submitted halfway. Slash commands such as `/accept-edits` and `/model <id>` go through `dt send` too. They start no turn, so `dt wait` after one only returns `STILL_RUNNING`; check the status line with `dt read <name> 3` instead. `send` refuses while a prompt or menu is open, refuses a slash command that switches to one of the modes `start` refuses, and refuses while the status line shows one of them; then send `/accept-edits` on its own first (anything after it would reach Devin as a message and run without asking).
3. Wait: `dt wait <name> [seconds]` (default 540, to stay inside a 10-minute tool timeout). Line 1 is the state alone. Branch on the exit code (`dt wait x; case $? in 0) …;; 10) …;; esac`), never on the whole output. On a permission prompt, the screen follows from line 2.
   - In Claude Code, run `wait`, alone or after `send` or `keys` in the same command, with `run_in_background: true`. A wait can take minutes, and in the foreground it holds up the conversation. The notice when it ends gives the exit code, and line 1 of its output file is the state.
   - `TURN_END` (0): the turn ended. Read the answer with `dt read <name> 80`, and check the files and diffs it produced yourself.
   - `PERMISSION_PROMPT` (10): Devin waits for an answer; the end of the screen follows. If the operation is within what you were asked to do, answer with `dt keys <name> Enter`, which picks the highlighted `Allow` for this once. Otherwise pick `Deny` (see "Answering a prompt") or tell the user.
   - `STILL_RUNNING` (12): still working. Run `dt wait` again.
   - `EXITED` (11): Devin exited.
   - Exit 2: the script refused, and stderr says why. `send`, `wait` and `log` refuse a Devin that runs in the shared daemon, as one started by devin-tmux 1.2.0 or earlier on Devin CLI 3000.13.4 does. Once its turn has ended (check with `dt read`), run `dt stop <name>` and then `dt start <name> <work folder> -r <Devin session name>`.
4. For follow-up instructions, repeat 2 and 3. Don't restart Devin.
5. When done, run `dt stop <name>`. It also deletes the saved instructions.

To work alongside, the user can run `tmux attach -t devin-<name>` in a terminal and share the same screen (detach with `Ctrl-b d`).

## Choosing a model

Pass the model to `dt start` with `--model`. `--model swe-latest-<effort>` (`low`, `medium`, `high`, `xhigh`, `max`) names the newest SWE release whose model at that effort `devin models` marks `Free`, and a new session without `--model` gets `swe-latest-high`. `start` prints the resolved command on stderr; `dt latest-model [effort]` prints the ID alone. Don't write a fixed SWE ID such as `swe-2-high`: it stays on an old release after a new one comes out. A resume keeps only a model saved with the session. Devin CLI 3000.14.30821 saves none for a session started with `--model`; it saves the model given on a resume (or by `/model`) once a turn runs. So a resume with `-r <ID>` and no `--model` gets `swe-latest-high` when nothing is saved, and keeps the saved model otherwise. To come back to a session started at another model, such as `swe-latest-max` or a Fusion pairing, pass that `--model` again. `-c` and `-r` without an ID get nothing added, and fall back to `agent.model` in the config when nothing is saved. To change it in a running session, send `/model <id>` with `dt send`. That also saves the model as the default for new sessions (`agent.model` in `~/.config/devin/config.json`), so prefer `--model` when the change is for this task only. `devin models` lists every model with its price on the signed-in account. Trust that list over the docs.

### Who pays

In the Devin CLI, the model decides who pays.

- A model that `devin models` marks `Free` costs nothing. On Pro, Max, and Teams, this covers the SWE models (for example SWE-2.1 and SWE-2, but not their `lightning` variants) and the SWE-2 sidekick in Fusion. Enterprise accounts pay for SWE.
- A GPT model bills a ChatGPT plan when the account shares one. Sharing needs a ChatGPT Go, Plus, or Pro plan linked in Devin, on a Devin Pro, Max, or Teams plan. Run `/billing` to check. It shows `Use your ChatGPT plan: on` while GPT goes to the ChatGPT plan. Fast and priority GPT variants never go to the ChatGPT plan.
- Every other model bills the Devin quota at the price that `devin models` shows, and then on-demand credits. A Teams flex seat has no quota, so it bills the credits directly.

### Pick by the work

| Work | Model |
| --- | --- |
| Most tasks | `swe-latest-high` (the default) |
| A harder task, or one that `swe-latest-high` got wrong | `swe-latest-max` |
| A design decision, a subtle bug, or a task that `swe-latest-max` got wrong | A Fusion pairing with a frontier lead and the SWE-2 sidekick. Pick the lead in the next section. |

### Pick the Fusion lead

The lead does the planning and the review, and the free SWE-2 sidekick writes most of the code. So the lead decides what a Fusion session costs.

- Without a shared ChatGPT plan, every lead bills the Devin quota or credits. Use `fusion-claude-opus-5-5-high-sidekick-swe-2-medium`. Claude Opus 5.5 costs less per token than GPT-6 Astra.
- With a shared ChatGPT plan, use `fusion-gpt-6-astra-high-sidekick-swe-2-medium`. The GPT lead bills the ChatGPT plan, a flat fee, so it adds no cost until the plan's allowance runs out. That allowance is the one Codex uses. If the user needs it for Codex, use the Claude lead, or ask the user to cap the share for Devin in ChatGPT under **Settings > Usage**.

Use an `xhigh` lead for the hardest work. Name the whole pairing ID, including the lead's effort. The default pairing uses a Medium lead. Whether GPT goes to the ChatGPT plan is the account owner's choice, so don't change `/billing` yourself.

### Check before you start

- Skip model IDs that contain `lightning`, `priority`, or `fast`. They bill the Devin quota or credits at a higher price.
- With a shared ChatGPT plan, a request that the plan cannot serve goes to the Devin quota and then to on-demand credits, without asking. When both are used up, the request fails.
- The line under the Devin CLI banner shows the remaining quota, such as `Teams · 0% remaining`. Read it with `dt read` after `dt start`. The credit balance is in Devin's **Settings > Usage & limits**.
- Free SWE-2 still has a rate limit. When Devin reports it, tell the user. Don't switch to a paid model to keep going.
- A Devin Cloud session started from the Devin MCP takes a mode, not a model. It needs Devin quota or on-demand credits to start, and it bills its machine time there. For work in a local checkout, use the CLI.

## Answering a prompt

- A command prompt lists `Allow`, `Bypass mode` and `Deny`; an edit prompt, or a command that writes into the work folder, lists `Allow`, `Accept edits mode`, `Bypass mode` and `Deny`. `Allow` is highlighted. To pick another entry, press `Down`, check where `❭` is with `dt read <name> 6`, then press `Enter`.
- On a command prompt, the `Allow` row also has a scope, such as `Allow ← ◼◼◼◼ → Once Exec(date)`, with `←→ 1-4 scope` in the footer (Devin CLI 3000.13.4). It starts at `Once`, so `Enter` approves this call only. `Right` and `Left` move it through `Session` (until this Devin exits, kept in memory), `Project` (in a test on 3000.13.4 it wrote the rule to the work folder's `.devin/config.local.json`; the docs say `.devin/config.json`) and `Always` (the user config `~/.config/devin/config.json`, per the docs). The wider scopes keep approving the rule after this call, and `Project` and `Always` write it into a config file, so `dt keys` does not send `Right` or `Left`: an agent approves at `Once` only. A wider grant is for the user to pick, in `tmux attach -t devin-<name>`.
- `dt keys` takes `Enter`, `Up` and `Down` only while a prompt or menu is open (elsewhere `Up` brings back earlier input from any session, for `Enter` to submit again), and `Escape` and `C-c` at any time. It refuses number keys (`1` to `4` pick a scope and approve in one keypress, without `Enter`) and Shift-Tab (it cycles the mode into smart and bypass), and takes `Enter` only on its own, so that the highlighted row can be checked first.
- `e` on a command prompt opens the command for editing before it runs (`↵ run edited command · esc back`); `dt keys` does not send it. A command that writes into the work folder has `e` but no scope, and an edit prompt has neither.
- `Accept edits mode` on an edit inside the work folder stops the prompts for later edits in this session, as `--permission-mode accept-edits` does.
- Don't pick `Bypass mode`: it approves every later call, pushes included. `dt keys` refuses `Enter` while it is highlighted.
- `Deny` ends Devin's turn. Tell it why and what to do next with `dt send`.
- When Devin runs several tool calls at once, their prompts come one after another. Run `dt wait` again after each answer; it reports the next one.

## Fewer prompts: the approve hook

Each prompt costs the agent a `wait`, a `keys` and another `wait`, all reading its whole conversation again. `scripts/approve-safe-commands.py` answers the routine ones for it. It is a Devin `PermissionRequest` hook: when a shell command would show a prompt, it approves the command if every part of it only reads, tests, lints or type-checks (`sed` without `-i`, `find` without `-delete` or `-exec`, `git status`/`log`/`diff`/`fetch`, `gh pr view`/`checks`, `bundle exec rspec`/`rubocop`, `pnpm nx test`/`lint`/`typecheck`, and the like; the lists are at the top of the script). Otherwise it prints nothing, and Devin shows its prompt as before.

- It leaves to the prompt: writes through the shell (`>`, `touch`, `mkdir`, `rm`) and through a tool's output option (`sort -o`, `rspec --out`, …), commits, pushes, `gh` writes, `git fetch` with a refspec, `cd` out of the folder Devin started in (`DEVIN_PROJECT_DIR`; another repository's git config or `package.json` can run code there, as `git -C` would), `xargs`, `npx`, MCP tools, environment variables other than a few such as `RAILS_ENV=test`, and any command that names a secret (`.env`, `.ssh`, `token`, `credential`, …).
- It also leaves to the prompt any shell syntax where its parser and the shell could read a command differently: `$(...)`, backquotes, heredocs, multi-line scripts, backslashes outside quotes (`find . \; …`), globs, braces, parentheses, `!`, and variables other than `$HOME`, `$PWD` and `$?`. Quoted separators such as `find . ';' …` stay arguments of the command they belong to. An independent review found bypasses of these kinds in the first version; its cases are in the tests.
- It never blocks. Devin treats a hook that fails or times out as if it had not run, so in accept-edits mode a broken hook costs a prompt, not a command that should have asked. Keep blocking in the `deny` list.
- On Devin CLI 3000.14.30821, approved commands ran without a prompt, and a deny rule still won over a hook that approved everything (`PreToolUse` or `PermissionRequest`): `git -C . status` and `git push` were refused. The hook also skips a command that matches a deny rule.
- Register it once in `~/.config/devin/config.json`, with the absolute path of the script under this skill's base directory. Devin reads hooks when it starts, so restart running sessions.

  ```json
  "hooks": {
    "PermissionRequest": [
      { "matcher": "exec", "hooks": [{ "type": "command", "command": "python3 <base directory>/scripts/approve-safe-commands.py", "timeout": 10 }] }
    ]
  }
  ```
- Before widening its lists, check them against what the project lets an agent approve. Run its tests with `python3 -m unittest discover -s <base directory>/scripts -p 'test_*.py'`.
- Devin asks less when it writes files with its edit tool instead of `cat > file`, and runs checks as one-line commands. Rules in `~/.codeium/windsurf/memories/global_rules.md` can tell it so in every folder.

## Notes

- From Codex, the sandbox may refuse to connect to the tmux socket (`/private/tmp/tmux-<uid>/`). If it does, run with escalated permissions or ask the user to allow it. Don't look for another way to run Devin.
- In a turn resumed with `devin -r … --prompt-file …`, every operation that needs confirmation (git commands that change things, installing packages, …) is rejected automatically with `NonInteractive("User skipped this tool call")`. Sent as ordinary input through tmux, it shows the permission prompt instead.
- Devin reads `.devin/config.json`, `.devin/config.local.json` and `.windsurf/rules/` only in the folder it starts in, not in parent folders. Started in a worktree under a project folder, it misses the project's permission rules and rules. Put what must apply in every folder in `~/.config/devin/config.json` (`permissions`, `permission_mode`) and `~/.codeium/windsurf/memories/global_rules.md`.
- A resumed session runs in the mode saved with it, not in the one that `--permission-mode` or the config's `permission_mode` gives (Devin CLI 3000.13.4). Only a mode switched to inside the session, with `/accept-edits` and the like, is saved (the `agent_mode` column of `sessions`); the mode given at start is not. So a session started with `--permission-mode accept-edits` resumes in the default mode and asks for every edit. A saved bypass mode comes back the same way (the status line shows, for example, `SWE-2 High · bypass`): send `/accept-edits`, or write in the instruction where Devin must stop (before a commit or a push, for example).
- The end of a turn is detected from the log (`dt log <name>`; the file is the child `devin acp`'s `~/.local/share/devin/cli/logs/devin_<datetime>_<pid>.log`) by the line `chisel_core::turn_stats: close`. `send` records how many there are, and `wait` waits for one more. A resume writes one such line when it reloads the session (with `from=None` on Devin CLI 3000.13.4), before the first `send`, so it does not count; lines with `load_session` don't count either.
- That log has the turn ends only while the agent runs inside `devin acp`. Devin CLI 3000.13.4 instead runs it by default in `devin daemon run --for-client`, a background process shared by every `devin` (3000.12.57 did not). The child then shows as `devin acp --process-model=daemon` and only relays: its log stops after startup, and the turn ends go to the daemon's log, whose lines don't name the session. Counting those would pick up other sessions' turns, so `start` passes `--process-model in-process`, and `send`, `wait` and `log` refuse a child that relays to the daemon.
- A permission prompt is detected from the screen, not the log, by the menu's `↵ confirm · esc cancel` footer: for parallel tool calls the log records every prompt at once, while the screen shows them one at a time. `wait` counts a menu only when it is still there 2 seconds later, so the one just answered with `keys` does not count. These log lines, the process model and the footer are not a documented interface and may change in a new Devin CLI version; if `wait` never returns `TURN_END` or `PERMISSION_PROMPT`, check them first (`devin --version`, and the child of `devin` in `ps`).
- If you watch the log yourself, don't put `cut` in the pipe: it holds lines back instead of passing each one on, so no notification arrives. To shorten lines, use `awk '{print substr($0,1,200); fflush()}'`.
- To only watch, use `dt view`, not `tmux attach -r`. While a read-only client is attached, the window shrinks to its size (and stays shrunk after it leaves), and `tmux send-keys` from outside fails with "client is read-only". `dt send` and `dt keys` go through a control-mode client, so they still work. Key sequences such as arrows pasted with `paste-buffer` are not treated as key presses by Devin.
- `dt keys` takes only the key names listed under "Answering a prompt". Send text with `dt send`.
- Don't open the same Devin session in two processes. If it is running in another tab, stop only that process before `dt start`. Other Devin sessions may be running at the same time, so don't stop them all with something like `pkill devin`.
- Choose Allow on a permission prompt only for operations within what you were asked to do. Pushes, pull requests, posts to outside services and the like follow the project's approval rules.
- Claude Code's permission rules and hooks don't reach what Devin runs. Devin imports the hooks in `~/.claude/settings.json`, but in a test on 3000.13.4 one that blocks `git add -A` for Claude let Devin run it, and Devin does not read a project's `.claude/settings.json` above the work folder. Devin also connects to the MCP servers set up on the machine (13 in one setup, Slack, GitHub and Google Workspace among them), where Claude Code's rules don't apply either. What stops Devin is its own deny list, your answers to its prompts, and the server side (branch protection, git hooks).
- Put what Devin must never run in the `deny` list of `~/.config/devin/config.json`, for example `Exec(git push)`, `Exec(git -C)`, `Exec(git rebase)`, `Exec(gh pr merge)` and `Exec(gh api)`. A rule matches the start of each command of a compound command, word for word: `Exec(rm -rf)` stopped `rm -rf` but not `rm -r -f`, and `Exec(git push)` stopped `cd . && git push` but not `git -C . push`. Devin reads the file when it starts, so restart running sessions after a change.
