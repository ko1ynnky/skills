import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOK_DIR)

from ask_me_stop import find_open_questions  # noqa: E402

HOOK = os.path.join(HOOK_DIR, "ask_me_stop.py")

QUESTION_LIST = "残りの論点です。\n\n1. 期限は今月末でよいか\n2. 通知は全員に送るか\n3. 旧画面は残すか\n"
PLAIN_REPORT = "テストはすべて通りました。\n\n- 認証の修正を反映しました。\n- ログの出力を減らしました。\n"


def message(text):
    return textwrap.dedent(text).strip()


class BlockedMessages(unittest.TestCase):
    cases = {
        "numbered Japanese questions": QUESTION_LIST,
        "two question marks": "件名はこのままでよいですか？\n送信は明日の朝でよいですか？",
        "two English questions": "Should the retry stay synchronous?\nIs a 30 second timeout enough?",
        "questions with a recommendation after each": """
            1. 既存のメールも移しますか。おすすめは「はい」です。
            2. 通知はどれを残しますか。おすすめは週次だけです。
            """,
        "question mark before a recommendation": """
            1. Should I keep the old endpoint? I recommend removing it.
            2. Should I bump the major version? I recommend yes.
            """,
        "Japanese ask heading with bullets": """
            ## 決めてほしい点

            - 公開日を今週にするか来週にするか
            - 告知を誰の名前で出すか
            """,
        "bold lead-in with bullets": """
            **確認してほしい点**

            - 文面の敬称
            - 添付する資料の版
            """,
        "ask closing a list": """
            - 案 A は移行が速いが、費用が増えます。
            - 案 B は費用が抑えられるが、移行が遅れます。

            この 2 案のどちらにするか、ご判断をお願いします。
            """,
        "English ask with bullets": """
            Please confirm the following before I deploy:

            - The migration can run during business hours.
            - The old index can be dropped.
            """,
        "which-one question after numbered options": """
            次は、

            1. 自分でレビューする
            2. このままコミットする
            3. 親 Issue だけ先に作る

            どれを進めますか？
            """,
        "which-one question with no ask phrase": """
            - 親に残して開いたままにする
            - 別の Issue に移して閉じる

            どちらがよいですか？
            """,
        "English which-one question after bullets": """
            - Ship it behind a flag.
            - Wait for the next release.

            Which one do you want?
            """,
        "ask that counts its points in one line": "確認したい点は 3 つです。期限、宛先、文面をどうするか決めてください。",
        "English ask that counts its points": "I have 3 questions for you about the rollout.",
        "ask beside a table": """
            ## 決めていただきたい点

            | 論点 | 推奨 |
            |---|---|
            | 保存先 | S3 |
            | 保持期間 | 1 年 |
            """,
        "ask beside a table that is longer than the tail": "## 判断が要るもの\n\n| 論点 |\n|---|\n"
        + "\n".join(f"| 論点 {n} |" for n in range(40)),
    }

    def test_blocks(self):
        for name, text in self.cases.items():
            with self.subTest(name):
                self.assertTrue(find_open_questions(message(text)))


class PassedMessages(unittest.TestCase):
    cases = {
        "empty": "",
        "lone question": "修正を反映しました。進めますか？",
        "lone English question": "The fix is in. Shall I proceed?",
        "lone yes or no question after a report list": PLAIN_REPORT + "\nこの内容でコミットしますか？",
        "plain report": PLAIN_REPORT,
        "plain English report": "Tests pass.\n\n- Fixed the auth bug.\n- Reduced log noise.",
        "next steps with no ask": """
            ## 次の作業

            1. STG にデプロイする
            2. 本番の設定を更新する
            3. 監視を確認する
            """,
        "additional candidates": """
            **追加候補**

            - 未使用の設定キーの削除
            - ログ出力の整理
            """,
        "past-tense summary of questions": """
            いただいた回答を反映しました。

            - 確認したい点として挙げた期限は、今月末で確定しました。
            - 判断をお願いした通知先は、全員に決まりました。
            """,
        "ask phrase quoted inside list items": """
            - PR 本文の「確認してほしい点」を更新しました。
            - 「決めてほしいこと」の節を削除しました。
            """,
        "ask phrase inside a report bullet": """
            - 認証の修正を反映しました。
            - 前回の確認したい点も解消しました。
            - ログの出力を減らしました。
            """,
        "completion notice": PLAIN_REPORT + "\nデプロイが終わったら教えてください。",
        "conditional offer": PLAIN_REPORT + "\n別の名前がよければ教えてください。",
        "reported speech": PLAIN_REPORT + "\n担当者には「内容を確認してほしい」と伝えれば足ります。",
        "questions for a third party": "## 山田さんに確認したい点\n\n- 請求の締め日\n- 振込先の名義",
        "English offer": PLAIN_REPORT + "\nLet me know if you want me to open a PR.",
        "questions inside a code fence": """
            サンプルを作りました。

            ```text
            1. 名前は何ですか？
            2. 住所はどこですか？
            ```
            """,
        "questions inside a blockquote": "> 期限はいつですか？\n> 宛先は誰ですか？\n\n上の 2 問は回答済みです。",
        "questions inside table cells": "| 質問 | 回答 |\n|---|---|\n| 期限は？ | 月末 |\n| 宛先は？ | 全員 |",
        "questions inside inline code": "- `foo?` を修正しました。\n- `bar?` を削除しました。",
        "rhetorical heading questions": "## なぜ失敗したのか？\n\n設定の漏れです。\n\n## どう直すか？\n\n値を足しました。",
        "questions above the tail": QUESTION_LIST + "\n" + "\n".join(f"作業 {n} を終えました。" for n in range(20)),
        "numbered list that only looks indirect": "1. 候補がいくつか\n2. 他の案がほか",
    }

    def test_passes(self):
        for name, text in self.cases.items():
            with self.subTest(name):
                self.assertEqual(find_open_questions(message(text)), [])


class HookScript(unittest.TestCase):
    def run_hook(self, stdin, entrypoint="cli"):
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENTRYPOINT"}
        if entrypoint is not None:
            env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
        if not isinstance(stdin, bytes):
            stdin = json.dumps(stdin).encode()
        return subprocess.run(
            [sys.executable, HOOK], input=stdin, env=env, capture_output=True, timeout=10
        )

    def transcript(self, first_line):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, handle.name)
        with handle:
            handle.write(first_line + "\n")
        return handle.name

    def codex_transcript(self, source):
        return self.transcript(json.dumps({"type": "session_meta", "payload": {"source": source}}))

    def assert_blocks(self, result):
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        instruction = output.get("reason") or output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AskUserQuestion", instruction)
        self.assertIn("request_user_input", instruction)

    def assert_silent(self, result):
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_blocks_a_question_list_in_an_interactive_run(self):
        for entrypoint in ("cli", "claude-desktop", "claude-vscode", "", None, "a-future-surface"):
            with self.subTest(entrypoint):
                self.assert_blocks(self.run_hook({"last_assistant_message": QUESTION_LIST}, entrypoint))

    def test_claude_code_gets_feedback_instead_of_a_hook_error(self):
        output = json.loads(self.run_hook({"last_assistant_message": QUESTION_LIST}, "cli").stdout)
        self.assertNotIn("decision", output)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "Stop")

    def test_codex_gets_the_documented_block_decision(self):
        output = json.loads(self.run_hook({"last_assistant_message": QUESTION_LIST}, None).stdout)
        self.assertEqual(output["decision"], "block")

    def test_codex_started_from_a_claude_code_shell_is_still_codex(self):
        payload = {"last_assistant_message": QUESTION_LIST, "turn_id": "turn-1"}
        for inherited in ("claude-desktop", "sdk-cli"):
            with self.subTest(inherited):
                output = json.loads(self.run_hook(payload, inherited).stdout)
                self.assertEqual(output["decision"], "block")

    def test_stays_silent_on_a_plain_report(self):
        self.assert_silent(self.run_hook({"last_assistant_message": PLAIN_REPORT}))

    def test_loop_guard_lets_a_continuation_end(self):
        payload = {"last_assistant_message": QUESTION_LIST, "stop_hook_active": True}
        self.assert_silent(self.run_hook(payload))

    def test_never_rewrites_non_interactive_claude_runs(self):
        payload = {"last_assistant_message": QUESTION_LIST}
        for entrypoint in ("sdk-cli", "sdk-py", "sdk-ts", "mcp", "claude-code-github-action"):
            with self.subTest(entrypoint):
                self.assert_silent(self.run_hook(payload, entrypoint))

    def test_never_rewrites_non_interactive_codex_runs(self):
        for source in ("exec", {"subagent": {"other": "guardian"}}):
            with self.subTest(source):
                payload = {
                    "last_assistant_message": QUESTION_LIST,
                    "transcript_path": self.codex_transcript(source),
                }
                self.assert_silent(self.run_hook(payload, None))

    def test_blocks_interactive_codex_runs(self):
        for source in ("vscode", "cli"):
            with self.subTest(source):
                payload = {
                    "last_assistant_message": QUESTION_LIST,
                    "transcript_path": self.codex_transcript(source),
                }
                self.assert_blocks(self.run_hook(payload, None))

    def test_unreadable_transcripts_count_as_interactive(self):
        paths = {
            "missing file": "/nonexistent/transcript.jsonl",
            "not json": self.transcript("not json"),
            "another record type": self.transcript(json.dumps({"type": "user", "message": {}})),
            "payload without a source": self.transcript(json.dumps({"type": "session_meta", "payload": {}})),
            "json that is not an object": self.transcript("[1, 2]"),
        }
        for name, path in paths.items():
            with self.subTest(name):
                payload = {"last_assistant_message": QUESTION_LIST, "transcript_path": path}
                self.assert_blocks(self.run_hook(payload))

    def test_malformed_input_never_stops_the_turn(self):
        inputs = {
            "bad json": b"{not json",
            "empty": b"",
            "invalid utf-8": b"\xff\xfe\x00",
            "json array": [1, 2],
            "json string": "hello",
            "missing message": {},
            "message of the wrong type": {"last_assistant_message": ["1. a?", "2. b?"]},
        }
        for name, stdin in inputs.items():
            with self.subTest(name):
                self.assert_silent(self.run_hook(stdin))


if __name__ == "__main__":
    unittest.main()
