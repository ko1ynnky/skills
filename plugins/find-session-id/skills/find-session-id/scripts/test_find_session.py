import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import find_session  # noqa: E402

OSC_TITLE = "\x1b]0;owned\x07"
C1_CSI = "\x9b2J"
CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


class TranscriptOutput(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)
        stores = {
            "CLAUDE_PROJECTS": self.root / "claude",
            "CURSOR_PROJECTS": self.root / "cursor",
            "CODEX_SESSIONS": self.root / "codex",
        }
        for name, path in stores.items():
            patcher = mock.patch.object(find_session, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
        )

    def claude_session(self, prompt):
        record = {
            "type": "user",
            "cwd": "/tmp/demo",
            "timestamp": "2026-11-01T10:00:00Z",
            "message": {"content": prompt},
        }
        self.write_jsonl(
            self.root / "claude" / "-tmp-demo" / "11111111-1111-4111-8111-111111111111.jsonl",
            [record],
        )

    def codex_session(self, cwd, prompt):
        session_id = "22222222-2222-4222-8222-222222222222"
        self.write_jsonl(
            self.root / "codex" / "2026" / "11" / "01" / f"rollout-2026-11-01T10-00-00-{session_id}.jsonl",
            [
                {"type": "session_meta", "payload": {"id": session_id, "cwd": cwd}},
                {
                    "type": "response_item",
                    "timestamp": "2026-11-01T10:00:00Z",
                    "payload": {"type": "message", "role": "user", "content": [{"text": prompt}]},
                },
            ],
        )

    def search(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            find_session.main(list(args))
        return output.getvalue()

    def test_text_output_drops_control_characters(self):
        self.claude_session(f"mail resend {OSC_TITLE} now {C1_CSI}")
        output = self.search("resend")
        self.assertIn("mail resend", output)
        self.assertNotRegex(output, CONTROL_CHARS)

    def test_json_output_drops_control_characters(self):
        self.claude_session(f"mail resend {OSC_TITLE} now {C1_CSI}")
        result = json.loads(self.search("resend", "--json"))[0]
        self.assertNotRegex(result["first_user_prompt"], CONTROL_CHARS)
        self.assertNotRegex(result["snippets"][0], CONTROL_CHARS)

    def test_a_codex_cwd_cannot_carry_control_characters_into_the_project(self):
        self.codex_session(f"/tmp/{OSC_TITLE}demo", "mail resend")
        self.assertNotRegex(self.search("resend"), CONTROL_CHARS)
        result = json.loads(self.search("resend", "--json"))[0]
        self.assertNotRegex(result["project_dir"], CONTROL_CHARS)

    def test_snippet_len_limits_the_prompt_in_both_formats(self):
        prompt = "resend " + "x" * 50
        self.claude_session(prompt)
        expected = prompt[:10] + "…"
        self.assertIn(f"prompt:   {expected}\n", self.search("resend", "--snippet-len", "10"))
        result = json.loads(self.search("resend", "--json", "--snippet-len", "10"))[0]
        self.assertEqual(result["first_user_prompt"], expected)

    def test_readable_text_stays_intact(self):
        self.claude_session("注文メールを再送する")
        self.assertIn("prompt:   注文メールを再送する\n", self.search("再送"))


if __name__ == "__main__":
    unittest.main()
