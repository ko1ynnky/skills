#!/usr/bin/env python3
"""Search coding-agent session transcripts by content keywords.

Three transcript stores are covered, because the same piece of work may have
been done in any of them:

  claude  ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
          (+ <session-id>/subagents/agent-*.jsonl for delegated work)
  cursor  ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<id>/<id>.jsonl
  codex   ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<id>.jsonl

A session matches when every keyword appears at least once in that session's
prose (user prompts + assistant replies, and tool traffic with --tools).

Output is one block per matching session: session id, source, project, last
activity, the first real user prompt as a "what was this about?" preview,
context snippets around each keyword hit, the resume command for that agent,
and the transcript path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CURSOR_PROJECTS = HOME / ".cursor" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

SOURCES = ("claude", "cursor", "codex")

# Prefixes that mark a "user" record as system-injected rather than something
# the human actually typed. We skip these entirely so both the preview and the
# keyword hits reflect the real conversation.
SYSTEM_USER_PREFIXES = (
    # Claude Code
    "<task-notification>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<command-stdout>",
    "<command-stderr>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "## Your Task",
    "Base directory for this skill",
    "This session is being continued from a previous conversation",
    "[Request interrupted by user",
    "Caveat: The messages below were generated",
    # Cursor
    "Briefly inform the user about the task result",
    # Codex
    "<environment_context>",
    "<user_instructions>",
    "<recommended_plugins>",
    "<ide_context>",
    "# AGENTS.md instructions",
    "The following is the Codex agent history",
    "The Codex agent has requested the following next action",
    "Reviewed Codex session id:",
    "Some conversation entries were omitted.",
)

# Cursor stamps each human turn with its local time, e.g.
# "Friday, Aug 14, 2026, 3:25 PM (UTC+9)".
CURSOR_TS_RE = re.compile(r"<timestamp>(.*?)</timestamp>", re.S)
CURSOR_TS_FMT = "%b %d, %Y, %I:%M %p"

USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
ROLLOUT_ID_RE = re.compile(r"rollout-.*?-([0-9a-fA-F-]{36})\.jsonl$")

RESUME_TEMPLATES = {
    "claude": "claude --resume {id}",
    "cursor": "cursor-agent --resume={id}",
    "codex": "codex resume {id}",
}


def is_system_user_message(text: str) -> bool:
    return text.lstrip().startswith(SYSTEM_USER_PREFIXES)


def cwd_to_project_dir(cwd: str) -> str:
    """Match the agents' encoding: path separators become hyphens."""
    return cwd.replace(os.sep, "-")


def normalize_project(name: str) -> str:
    """Claude keeps the leading separator ("-Users-…"), Cursor drops it."""
    return name.lstrip("-")


def shorten_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(HOME))
    except ValueError:
        return str(path)


def clean_snippet(text: str) -> str:
    """Tool payloads carry escaped newlines; make one readable line of it."""
    return " ".join(text.replace("\\n", " ").replace("\\t", " ").split())


def parse_ts(source: str, raw: str | None) -> float | None:
    """Turn a transcript timestamp into an epoch, or None if unparseable."""
    if not raw:
        return None
    try:
        if source == "cursor":
            text = raw.split("(")[0].strip().rstrip(",")
            # Drop the weekday prefix; Cursor writes the user's local time.
            if "," in text:
                text = text.split(",", 1)[1].strip()
            return datetime.strptime(text, CURSOR_TS_FMT).timestamp()
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def prefilter_keys(keywords_lower: list[str]) -> list[bytes]:
    """Keywords safe to look for in raw bytes.

    bytes.lower() only folds ASCII, so a keyword whose case folding needs
    Unicode (accented Latin, full-width Latin) is left out of the prefilter
    rather than risking a false miss. Caseless scripts (Japanese, digits)
    are unaffected and stay in.
    """
    keys = []
    for kw in keywords_lower:
        if kw.isascii() or kw.lower() == kw.upper():
            keys.append(kw.encode("utf-8"))
    return keys


def file_may_match(path: Path, keys: list[bytes]) -> bool:
    """Cheap byte scan so unrelated transcripts are never JSON-parsed.

    A superset of the real search: every keyword must appear somewhere in the
    file for the session to have any chance of matching.
    """
    if not keys:
        return True
    remaining = set(range(len(keys)))
    overlap = max(len(k) for k in keys) - 1
    tail = b""
    try:
        with path.open("rb") as fh:
            while remaining:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                buf = (tail + chunk).lower()
                for i in list(remaining):
                    if keys[i] in buf:
                        remaining.discard(i)
                tail = buf[-overlap:] if overlap > 0 else b""
    except OSError:
        return False
    return not remaining


def read_jsonl(path: Path) -> Iterator[dict]:
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


@dataclass
class Item:
    """One normalized piece of a transcript."""

    kind: str  # "user" | "assistant" | "tool" | "meta"
    text: str = ""
    meta: dict = field(default_factory=dict)
    ts_raw: str | None = None


def blocks_to_items(
    content, kind: str, want_tools: bool, ts_raw: str | None = None
) -> Iterator[Item]:
    """Claude and Cursor share the Anthropic content-block shape.

    Text blocks of one message are joined into a single item so that a prompt
    split across blocks counts once and its snippets keep their context.
    """
    if isinstance(content, str):
        yield Item(kind, content, ts_raw=ts_raw)
        return
    if not isinstance(content, list):
        return
    texts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text") or "")
        elif not want_tools:
            continue
        elif btype == "tool_use":
            tools.append(json.dumps(block.get("input"), ensure_ascii=False))
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                tools.append(inner)
            elif isinstance(inner, list):
                tools.extend(
                    sub.get("text") or ""
                    for sub in inner
                    if isinstance(sub, dict) and sub.get("type") == "text"
                )
    joined = "\n".join(t for t in texts if t).strip()
    if joined:
        yield Item(kind, joined, ts_raw=ts_raw)
    for tool_text in tools:
        yield Item("tool", tool_text, ts_raw=ts_raw)


def iter_claude(path: Path, want_tools: bool) -> Iterator[Item]:
    is_subagent = path.parent.name == "subagents"
    seen_cwd = False
    for rec in read_jsonl(path):
        rtype = rec.get("type")
        if rtype not in ("user", "assistant"):
            continue
        # Sidechain records duplicate subagent transcripts, which we read
        # from their own files instead.
        if rec.get("isSidechain") and not is_subagent:
            continue
        if not seen_cwd and rec.get("cwd"):
            seen_cwd = True
            yield Item("meta", meta={"cwd": rec["cwd"]})
        msg = rec.get("message") or {}
        yield from blocks_to_items(
            msg.get("content"), rtype, want_tools, rec.get("timestamp")
        )


def iter_cursor(path: Path, want_tools: bool) -> Iterator[Item]:
    for rec in read_jsonl(path):
        role = rec.get("role")
        if role not in ("user", "assistant"):
            continue
        msg = rec.get("message") or {}
        for item in blocks_to_items(msg.get("content"), role, want_tools):
            if item.kind != "user":
                yield item
                continue
            # Cursor wraps what the human typed; anything else on the user
            # side is harness plumbing.
            found = USER_QUERY_RE.search(item.text)
            if not found:
                continue
            stamped = CURSOR_TS_RE.search(item.text)
            yield Item(
                "user",
                found.group(1).strip(),
                ts_raw=stamped.group(1).strip() if stamped else None,
            )


def iter_codex(path: Path, want_tools: bool) -> Iterator[Item]:
    for rec in read_jsonl(path):
        rtype = rec.get("type")
        payload = rec.get("payload") or {}
        if rtype == "session_meta":
            yield Item(
                "meta",
                meta={
                    "cwd": payload.get("cwd"),
                    "session_id": payload.get("id") or payload.get("session_id"),
                    "parent_id": payload.get("parent_thread_id"),
                    "thread_source": payload.get("thread_source"),
                },
            )
            continue
        # event_msg records mirror response_item messages; reading both would
        # double-count every prompt.
        if rtype != "response_item":
            continue
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            texts = [
                block["text"]
                for block in payload.get("content") or []
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            ]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                yield Item(role, joined, ts_raw=rec.get("timestamp"))
        elif want_tools:
            if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                call = {
                    k: payload.get(k)
                    for k in ("name", "arguments", "input", "action")
                    if payload.get(k) is not None
                }
                yield Item("tool", json.dumps(call, ensure_ascii=False), ts_raw=rec.get("timestamp"))
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                out = payload.get("output")
                yield Item(
                    "tool",
                    out if isinstance(out, str) else json.dumps(out, ensure_ascii=False),
                    ts_raw=rec.get("timestamp"),
                )


@dataclass
class Candidate:
    source: str
    path: Path
    session_id: str
    project: str | None = None
    subagent: str | None = None


@dataclass
class Match:
    source: str
    session_id: str
    resume_id: str
    project: str
    path: Path
    updated: float
    size: int
    first_user_prompt: str
    user_prompt_count: int
    subagent: str | None = None
    extra_transcripts: int = 0
    snippets: list[str] = field(default_factory=list)


def claude_candidates(include_subagents: bool) -> Iterator[Candidate]:
    if not CLAUDE_PROJECTS.is_dir():
        return
    for project_dir in CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        for path in project_dir.glob("*.jsonl"):
            yield Candidate("claude", path, path.stem, project_dir.name)
        if include_subagents:
            for path in project_dir.glob("*/subagents/*.jsonl"):
                yield Candidate(
                    "claude", path, path.parents[1].name, project_dir.name, path.stem
                )


def cursor_candidates() -> Iterator[Candidate]:
    if not CURSOR_PROJECTS.is_dir():
        return
    for project_dir in CURSOR_PROJECTS.iterdir():
        transcripts = project_dir / "agent-transcripts"
        if not transcripts.is_dir():
            continue
        for path in transcripts.glob("*/*.jsonl"):
            yield Candidate("cursor", path, path.stem, project_dir.name)


def codex_candidates() -> Iterator[Candidate]:
    if not CODEX_SESSIONS.is_dir():
        return
    for path in CODEX_SESSIONS.rglob("rollout-*.jsonl"):
        found = ROLLOUT_ID_RE.search(path.name)
        yield Candidate("codex", path, found.group(1) if found else path.stem)


def iter_candidates(sources: list[str], include_subagents: bool) -> Iterator[Candidate]:
    if "claude" in sources:
        yield from claude_candidates(include_subagents)
    if "cursor" in sources:
        yield from cursor_candidates()
    if "codex" in sources:
        yield from codex_candidates()


ITERATORS = {"claude": iter_claude, "cursor": iter_cursor, "codex": iter_codex}


def search_session(
    cand: Candidate,
    keywords_lower: list[str],
    search_assistant: bool,
    search_tools: bool,
    include_subagents: bool,
) -> Match | None:
    matched: dict[int, str] = {}  # keyword index -> first context snippet
    first_user_prompt: str | None = None
    user_prompt_count = 0
    meta: dict = {}
    last_ts_raw: str | None = None

    for item in ITERATORS[cand.source](cand.path, search_tools):
        if item.kind == "meta":
            meta.update({k: v for k, v in item.meta.items() if v})
            continue
        if item.ts_raw:
            last_ts_raw = item.ts_raw
        text = item.text.strip() if isinstance(item.text, str) else ""
        if not text:
            continue

        if item.kind == "user":
            if is_system_user_message(text):
                continue
            user_prompt_count += 1
            if first_user_prompt is None:
                first_user_prompt = text
        elif item.kind == "assistant":
            if not search_assistant:
                continue
        elif item.kind == "tool":
            if not search_tools:
                continue

        lowered = text.lower()
        for i, kw in enumerate(keywords_lower):
            if i in matched:
                continue
            idx = lowered.find(kw)
            if idx < 0:
                continue
            start = max(0, idx - 60)
            end = min(len(text), idx + len(kw) + 60)
            matched[i] = clean_snippet(text[start:end])

    if len(matched) != len(keywords_lower):
        return None

    # Codex records subagent threads (guardian, approval assessors) as their own
    # rollout files; report the parent thread so the resume command is usable.
    parent_id = meta.get("parent_id")
    subagent = cand.subagent
    if cand.source == "codex":
        session_id = meta.get("session_id") or cand.session_id
        if meta.get("thread_source") == "subagent" and parent_id:
            if not include_subagents:
                return None
            subagent = f"codex {session_id}"
            session_id = parent_id
    else:
        session_id = cand.session_id

    project = cand.project
    if not project:
        cwd = meta.get("cwd")
        project = cwd_to_project_dir(cwd) if cwd else "(unknown)"

    try:
        st = cand.path.stat()
    except OSError:
        return None

    # Cursor rewrites transcript files when the IDE reopens them, so the file
    # mtime can be days off; the conversation's own timestamps are the truth.
    return Match(
        source=cand.source,
        session_id=session_id,
        resume_id=session_id,
        project=project,
        path=cand.path,
        updated=parse_ts(cand.source, last_ts_raw) or st.st_mtime,
        size=st.st_size,
        first_user_prompt=first_user_prompt or "",
        user_prompt_count=user_prompt_count,
        subagent=subagent,
        snippets=[matched[i] for i in sorted(matched)],
    )


def in_scope(m: Match, project: str | None) -> bool:
    if not project:
        return True
    return normalize_project(m.project) == normalize_project(project)


def resume_command(m: Match) -> str:
    return RESUME_TEMPLATES[m.source].format(id=m.resume_id)


def format_result(m: Match, snippet_len: int) -> str:
    when = datetime.fromtimestamp(m.updated).strftime("%Y-%m-%d %H:%M")
    prompt = m.first_user_prompt.replace("\n", " ").strip()
    if len(prompt) > snippet_len:
        prompt = prompt[:snippet_len] + "…"
    lines = [
        f"sessionId: {m.session_id}",
        f"  source:   {m.source}    project: {m.project}",
        f"  updated:  {when}    size: {m.size:,} bytes    user prompts: {m.user_prompt_count}",
        f"  prompt:   {prompt or '(no non-system user prompt found)'}",
    ]
    if m.subagent:
        lines.append(f"  via:      subagent {m.subagent}")
    if m.extra_transcripts:
        lines.append(
            f"  also:     {m.extra_transcripts} more transcript(s) of this session matched"
        )
    for s in m.snippets[:3]:
        lines.append(f"  match:    …{s}…")
    lines.append(f"  resume:   {resume_command(m)}")
    lines.append(f"  file:     {shorten_path(m.path)}")
    return "\n".join(lines)


def to_dict(m: Match) -> dict:
    return {
        "source": m.source,
        "session_id": m.session_id,
        "project_dir": m.project,
        "path": str(m.path),
        "updated": datetime.fromtimestamp(m.updated).isoformat(timespec="seconds"),
        "size": m.size,
        "first_user_prompt": m.first_user_prompt,
        "user_prompt_count": m.user_prompt_count,
        "subagent": m.subagent,
        "extra_transcripts": m.extra_transcripts,
        "snippets": m.snippets,
        "resume_command": resume_command(m),
    }


def parse_sources(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(SOURCES)
    picked = []
    for raw in value.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in SOURCES:
            raise argparse.ArgumentTypeError(
                f"unknown source: {name} (choose from {', '.join(SOURCES)}, all)"
            )
        if name not in picked:
            picked.append(name)
    if not picked:
        raise argparse.ArgumentTypeError("no source selected")
    return picked


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Find session IDs by content keywords (AND match, case-insensitive) "
            "across Claude Code, Cursor and Codex transcripts."
        ),
    )
    p.add_argument(
        "keywords",
        nargs="+",
        help="Keywords to search. All must appear in the same session (AND).",
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument(
        "--project",
        help=(
            "Limit search to a single encoded project directory name "
            "(e.g. -Users-foo-dev-bar). The leading hyphen is optional."
        ),
    )
    scope.add_argument(
        "--cwd",
        action="store_true",
        help="Limit search to the project matching the current working directory.",
    )
    p.add_argument(
        "--source",
        type=parse_sources,
        default=list(SOURCES),
        help="Comma-separated transcript stores to search: claude, cursor, codex, all (default: all).",
    )
    p.add_argument(
        "--no-assistant",
        action="store_true",
        help="Search only user prompts, not assistant responses.",
    )
    p.add_argument(
        "--tools",
        action="store_true",
        help=(
            "Also search tool calls and their output (file paths, branch names, "
            "commands). Noisier, but finds what was never said in prose."
        ),
    )
    p.add_argument(
        "--no-subagents",
        action="store_true",
        help="Skip delegated subagent transcripts (searched by default).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of results to display (default: 20).",
    )
    p.add_argument(
        "--snippet-len",
        type=int,
        default=300,
        help="Length of the first-prompt preview in characters (default: 300).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit results as a JSON array instead of human-readable text.",
    )
    return p.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)

    project = args.project
    if args.cwd:
        project = cwd_to_project_dir(os.getcwd())

    keywords_lower = [k.lower() for k in args.keywords]
    include_subagents = not args.no_subagents

    keys = prefilter_keys(keywords_lower)

    matches: list[Match] = []
    for cand in iter_candidates(args.source, include_subagents):
        if not file_may_match(cand.path, keys):
            continue
        m = search_session(
            cand,
            keywords_lower,
            search_assistant=not args.no_assistant,
            search_tools=args.tools,
            include_subagents=include_subagents,
        )
        if m is not None and in_scope(m, project):
            matches.append(m)

    matches.sort(key=lambda m: m.updated, reverse=True)
    unique: dict[tuple[str, str], Match] = {}
    deduped: list[Match] = []
    for m in matches:
        key = (m.source, m.session_id)
        if key in unique:
            unique[key].extra_transcripts += 1
            continue
        unique[key] = m
        deduped.append(m)
    matches = deduped
    truncated = len(matches) > args.limit
    matches = matches[: args.limit]

    if args.json:
        print(json.dumps([to_dict(m) for m in matches], ensure_ascii=False, indent=2))
        return 0 if matches else 1

    if not matches:
        scope_note = f" (project: {project})" if project else ""
        print(
            f"No sessions matched: {' '.join(args.keywords)}"
            f"{scope_note} in {', '.join(args.source)}",
            file=sys.stderr,
        )
        return 1

    for i, m in enumerate(matches):
        print(format_result(m, snippet_len=args.snippet_len))
        if i < len(matches) - 1:
            print()
    if truncated:
        print(
            f"\n... showing top {args.limit}; pass --limit to expand.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
