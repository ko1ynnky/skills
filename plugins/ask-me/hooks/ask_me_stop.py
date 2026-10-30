#!/usr/bin/env python3
"""
Stop hook (Claude Code and Codex): turn prose decision lists into question-tool calls.

Background:
  Agents often end a turn with "please confirm these 4 points". The user then has
  to type every answer by hand, or type "use AskUserQuestion" to get a form. This
  hook notices such a message and sends the agent back once to ask with its
  question tool.

Behaviour:
  1. stop_hook_active is true: do nothing, so a continuation can end.
  2. Non-interactive run (`claude -p`, Agent SDK, `codex exec`, Codex subagents):
     do nothing, so scripted output is never rewritten.
  3. find_open_questions() finds a decision list in the tail of the message:
     print the output that continues the turn with REASON as the next instruction.
  Every other path prints nothing and exits 0.

Detection:
  Fenced code, inline code and blockquotes are removed, and each table shrinks to a
  two-item placeholder. The last TAIL_LINES non-empty lines are classified into Line
  records, and RULES decide. A message is blocked when it has several question lines,
  an ask beside a list, a which-one question right after a list, or an ask that counts
  its own points ("3 points to confirm").

What it intentionally does not do:
  - Block on one lone question such as "Shall I proceed?", even right after a list.
    A one-line answer is cheaper to type than a form is to open.
  - Count an ask inside a list item. Reports quote phrases like "points to confirm"
    in their bullets far more often than they ask anything there.
  - Treat "tell me when it is done" as an ask. That requests an action, not a decision.
  - Read the transcript for the message. Both harnesses send last_assistant_message,
    and the Claude Code transcript can lag behind it.
  - Treat an unknown entrypoint or session source as non-interactive. A wrong block
    costs one extra model step. A wrong pass silently disables the hook.
  - Call a model to classify the message. The hook runs on every turn and has to
    stay cheap, offline and free of dependencies.
"""

import json
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

REASON = (
    "Your last message leaves decisions for the user in prose. Ask them now with your "
    "question tool (Claude Code: AskUserQuestion, Codex: request_user_input). Give each "
    "question enough context to decide on its own, put the recommended option first, and "
    "write in the user's language. If no question tool is available, or the message holds "
    "no real decision for the user, end the turn without repeating the message."
)

TAIL_LINES = 15
MIN_LIST_ITEMS = 2
MIN_QUESTION_LINES = 2

NON_INTERACTIVE_ENTRYPOINT = re.compile(r"^(?:sdk-|mcp$|claude-code-github-action$)")
NON_INTERACTIVE_CODEX_SOURCE = "exec"

NOISE = (
    (re.compile(r"^ {0,3}(```+|~~~+).*?(?:^ {0,3}\1[^\n]*$|\Z)", re.M | re.S), ""),
    (re.compile(r"`[^`\n]*`"), " "),
    (re.compile(r"^\s*>.*$", re.M), ""),
    # A table stays visible as a two-item list: its cells are noise, but an ask beside it
    # counts. Two placeholders, however long the table, so it cannot flood the tail.
    (re.compile(r"(?:^[ \t]*\|.*(?:\n|\Z))+", re.M), "- (table)\n- (table)\n"),
)

MARKUP = re.compile(r"\*\*|__|\*+\s*$")
HEADING = re.compile(r"^#{1,6}\s")
NUMBERED_ITEM = re.compile(r"^\s*(?:\d{1,2}[.)．、]\s*(?!\d)|\(\d{1,2}\)\s*|（\d{1,2}）\s*|[①-⑳]\s*)")
BULLET_ITEM = re.compile(r"^\s*(?:[-*+•]\s+|・\s*)")
QUESTION_MARK = re.compile(r"？(?![」』）)])|\?(?=\s|$)")
INLINE_QUESTION = re.compile(r"(?:ますか|ですか|でしょうか)[。.]")
INDIRECT_QUESTION_END = re.compile(r"(?<!いくつ)(?<!何)(?<!なに)(?<!誰)(?<!ほ)(?<!し)(?<!わず)(?<!確)か[。.]?\s*$")
CHOICE_WORD = re.compile(r"どれ|どちら|どっち|どの|いずれ|\bwhich\b|\bwhat would you\b|\bhow would you\b", re.I)
COUNT = re.compile(
    r"(?<![\d.])(?:[2-9２-９]|[1-9１-９][\d０-９])\s*(?:点|つ|件|項目)"
    r"|\b(?:[2-9]|two|three|four|five|six|seven|eight)\s+(?:\w+\s+)?(?:questions|decisions|points|items)\b",
    re.I,
)

# Reported speech and conditions: 「確認してほしい」と伝える, 確認してほしい場合は.
NOT_REPORTED = r"(?![」』]|と伝|場合|とき)"
# Completion notices are requests to act, not decisions: 終わったら教えてください.
NOT_CONDITIONAL = r"(?<!たら)(?<!たら、)(?<!なら)(?<!なら、)(?<!れば)(?<!れば、)(?<!場合は)(?<!場合は、)"

ASK_PHRASES = tuple(
    re.compile("(?:" + pattern + ")" + NOT_REPORTED, re.I)
    for pattern in (
        r"確認(?:して)?(?:ほしい|いただきたい)",
        r"(?<!さんに)確認したい(?:点|こと|事項)",
        r"確認事項[:：]?$",
        r"決めて(?:ほしい|いただきたい|いただけ|もらえ|ください)",
        r"判断(?:して)?(?:ほしい|いただきたい|いただけ|もらえ|ください|をお願い)",
        r"判断が(?:要る|必要な)(?:もの|点|こと|事項|論点|項目)",
        r"^要判断|要判断[:：]",
        r"選んで(?:ほしい|いただきたい|いただけ|ください)",
        NOT_CONDITIONAL + r"教えて(?:ほしい|いただきたい|いただけ|ください)",
        r"(?:回答|返事|返答)を?(?:ください|お願い|いただきたい|いただけ|いただいていません|もらえ|もらったら)",
        r"指示(?:して|を)(?:ください|お願い)",
        r"(?:ご|の)判断[^。]{0,60}待",
        r"(?:どれ|どちら|どっち)(?:を|に)[^。]*(?:ますか|ですか|ください)",
        r"\bplease (?:confirm|decide|choose|pick|select)\b",
        r"\blet me know (?:which|what|whether|how)\b",
        r"\bwhich (?:\w+ )?(?:would|do|should) you\b",
        r"\b(?:would|do) you prefer\b",
        r"\byour (?:call|decision|input|preference)\b",
        r"\bneeds? (?:a |your )?decisions?\b",
        r"\bdecisions? (?:for you|needed|required)\b",
        r"\bquestions? for you\b",
        r"\bopen questions\b",
    )
)


@dataclass(frozen=True)
class Line:
    text: str
    is_heading: bool
    is_item: bool
    is_question: bool
    is_ask: bool


def visible_text(message: str) -> str:
    for pattern, replacement in NOISE:
        message = pattern.sub(replacement, message)
    return message


def classify(raw: str) -> Line:
    text = MARKUP.sub("", raw).strip()
    numbered = bool(NUMBERED_ITEM.match(text))
    is_heading = bool(HEADING.match(text))
    return Line(
        text=text,
        is_heading=is_heading,
        is_item=numbered or bool(BULLET_ITEM.match(text)),
        is_question=not is_heading
        and bool(
            QUESTION_MARK.search(text)
            or (numbered and (INLINE_QUESTION.search(text) or INDIRECT_QUESTION_END.search(text)))
        ),
        is_ask=any(pattern.search(text) for pattern in ASK_PHRASES),
    )


def tail_lines(message: str) -> List[Line]:
    raws = [raw for raw in visible_text(message).splitlines() if raw.strip()]
    return [classify(raw) for raw in raws[-TAIL_LINES:]]


def items_before(lines: List[Line], index: int) -> List[Line]:
    start = index
    while start > 0 and lines[start - 1].is_item:
        start -= 1
    return lines[start:index]


def items_after(lines: List[Line], index: int) -> List[Line]:
    end = index + 1
    while end < len(lines) and lines[end].is_item:
        end += 1
    return lines[index + 1 : end]


def several_questions(lines: List[Line]) -> List[Line]:
    questions = [line for line in lines if line.is_question]
    return questions if len(questions) >= MIN_QUESTION_LINES else []


def ask_beside_list(lines: List[Line]) -> List[Line]:
    for index, line in enumerate(lines):
        if not line.is_ask or line.is_item:
            continue
        before = [] if line.is_heading else items_before(lines, index)
        items = before + items_after(lines, index)
        if len(items) >= MIN_LIST_ITEMS:
            return [line] + items
    return []


def choice_after_list(lines: List[Line]) -> List[Line]:
    for index, line in enumerate(lines):
        if not line.is_question or line.is_item or not CHOICE_WORD.search(line.text):
            continue
        items = items_before(lines, index)
        if len(items) >= MIN_LIST_ITEMS:
            return items + [line]
    return []


def counted_ask(lines: List[Line]) -> List[Line]:
    return [line for line in lines if line.is_ask and not line.is_item and COUNT.search(line.text)]


RULES = (several_questions, ask_beside_list, choice_after_list, counted_ask)


def find_open_questions(message: str) -> List[str]:
    lines = tail_lines(message)
    for rule in RULES:
        evidence = rule(lines)
        if evidence:
            return [line.text for line in evidence]
    return []


def codex_session_source(transcript_path: str) -> Optional[object]:
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as transcript:
            record = json.loads(transcript.readline())
        return record["payload"]["source"] if record.get("type") == "session_meta" else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def is_non_interactive(entrypoint: str, codex_source: Optional[object]) -> bool:
    if NON_INTERACTIVE_ENTRYPOINT.match(entrypoint):
        return True
    # Codex subagents carry a dict here. They have no user to ask.
    return codex_source == NON_INTERACTIVE_CODEX_SOURCE or isinstance(codex_source, dict)


def continue_turn(entrypoint: str) -> dict:
    # Claude Code shows decision:block as a hook error, and this hook has not failed.
    if entrypoint:
        return {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": REASON}}
    # Codex sets no entrypoint and documents only decision:block for continuing a turn.
    return {"decision": "block", "reason": REASON}


def parse_payload(stream) -> Tuple[str, bool, str, bool]:
    payload = json.loads(stream.read())
    if not isinstance(payload, dict):
        raise ValueError("Stop hook input is not an object")
    message = payload.get("last_assistant_message")
    transcript_path = payload.get("transcript_path")
    return (
        message if isinstance(message, str) else "",
        payload.get("stop_hook_active") is True,
        transcript_path if isinstance(transcript_path, str) else "",
        "turn_id" in payload,
    )


def main() -> int:
    try:
        message, stop_hook_active, transcript_path, is_codex = parse_payload(sys.stdin.buffer)
    except ValueError:
        return 0
    if stop_hook_active:
        return 0
    # Codex started from a Claude Code shell inherits this variable, so it says nothing there.
    entrypoint = "" if is_codex else os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if is_non_interactive(entrypoint, codex_session_source(transcript_path)):
        return 0
    if find_open_questions(message):
        print(json.dumps(continue_turn(entrypoint)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
