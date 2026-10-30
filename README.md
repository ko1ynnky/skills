# skills

Agent skills and plugins for Claude Code and Codex.

## ask-me

Agents often end a turn with "please confirm these 4 points", and you answer by typing. `ask-me` sends the agent back to ask those points with its question tool, so you click through a form instead.

A Stop hook reads the last message of each turn. When the end of the message holds a decision list, the hook blocks the stop once and tells the agent to use `AskUserQuestion` (Claude Code) or `request_user_input` (Codex). A `/ask-me` skill clears open decisions on demand, for anything the hook missed.

### Install

Claude Code:

```sh
claude plugin marketplace add ko1ynnky/skills
claude plugin install ask-me@ko1ynnky
```

Codex:

```sh
codex plugin marketplace add ko1ynnky/skills
codex plugin add ask-me@ko1ynnky
```

Codex does not run a plugin's hooks until you trust them. Run `/hooks` after installing, and again after an update that changes the hook, because Codex withdraws trust when the script changes.

Skill only, for any agent that reads skills:

```sh
npx skills add ko1ynnky/skills -s ask-me
```

This route installs the skill and no hook, so nothing runs automatically. Use a plugin route to get the hook.

The hook needs `python3` on your `PATH`. It has no other dependencies.

### What the hook does

It looks at the last 15 non-empty lines, ignoring code, blockquotes and table contents, and blocks when it finds one of these:

- Two or more question lines.
- An ask such as "please confirm" or 確認してほしい点, beside a list or a table.
- A which-one question right after a list.
- An ask that counts its points, such as "3 points to confirm".

It passes a lone question such as "Shall I proceed?", plain reports, next-step lists, and requests to act such as "tell me when the deploy finishes".

It does nothing in non-interactive runs, so `claude -p`, the Agent SDK, `claude mcp serve`, the GitHub Action, `codex exec` and Codex subagents keep their output. The hook never blocks twice in a row, so the agent can always end its turn. The detector reads Japanese and English.

### Tests

```sh
python3 -m unittest discover -s plugins/ask-me/hooks
```

## 日本語

`ask-me` は、ターンの最後に「次の 4 点を確認してください」と文章で残された判断を、質問ツールで聞き直させるプラグインです。回答を手で打たず、フォームで選べるようになります。

Stop フックが各ターンの最後のメッセージを読み、末尾に判断のリストがあれば、一度だけ停止を止めて `AskUserQuestion`（Claude Code）か `request_user_input`（Codex）で聞くよう指示します。フックが見逃したものは `/ask-me` スキルでまとめて解消できます。

インストールは上の Install の手順です。次の 2 点に注意してください。

- `npx skills` で入るのはスキルだけです。フックは入らないので、自動では動きません。フックにはプラグインでのインストールが要ります。
- Codex では、インストール後に `/hooks` でフックを信頼してください。信頼するまでフックは動きません。スクリプトが変わる更新の後も、信頼し直しが要ります。

`claude -p` や `codex exec` などの非対話の実行では、フックは何もしません。

## License

MIT
