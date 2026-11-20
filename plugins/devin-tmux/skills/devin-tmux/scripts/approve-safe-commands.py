#!/usr/bin/env python3
"""Devin CLI PermissionRequest hook: approve shell commands that only read, test, lint or type-check.

Devin runs this when a command would show a permission prompt. It prints {"decision": "approve"} when every
part of the command is on the list below, and prints nothing otherwise, so Devin shows its prompt as before.
It never blocks: the deny list in the Devin config does that.

Why approve-only: Devin treats a hook that fails, times out or prints something unreadable as an error and
goes on as if the hook had not run. In accept-edits mode that means the usual prompt, so a broken hook can
only cost a prompt. A hook that had to block dangerous commands in smart mode would let them through instead.

Why not just add allow rules: Devin's rules match the start of a command word for word, so they cannot say
"sed without -i", "find without -delete" or "RAILS_ENV=test bundle exec rspec".

Why the shell syntax accepted is so small: the hook has to read the command the way the shell will. Quoted
separators (find . ';' …), backslashes, globs, brace expansion, variables and the like are where a parser
and the shell disagree, so a command that uses them goes to the prompt instead.
"""
import json
import os
import re
import shlex
import sys

REFUSE_RAW = re.compile(r"\$\(|`|<\(|>\(|<<|\n|\$'")
# Variables expand outside single quotes; these few cannot carry a secret
ALLOWED_VAR = re.compile(r"\$(?:\?|\{?HOME\b\}?|\{?PWD\b\}?)")
# 2>&1, >/dev/null, 2>/dev/null and &>/dev/null write nowhere
SAFE_REDIRECT = re.compile(r"(?:[012]?>&[12]|&>\s*/dev/null|[012]?>>?\s*/dev/null)(?=$|[\s;|&])")
# Reading is not harmless when it sends a secret to the model. Devin's own rules always ask for .env and key
# files, so leave anything that names one to the prompt, at the cost of some false alarms such as grep token.
# A list of names can never be complete; the parser above refuses the tricks that spell one indirectly
SENSITIVE = re.compile(
    r"\.env|\.ssh\b|\.aws\b|\.gnupg\b|\.netrc|\.npmrc|\.pypirc|\.pem\b|\.p12\b|\.key\b|id_(?:rsa|ed25519|ecdsa)"
    r"|credential|secret|token|passw|keychain|op://|hosts\.yml|\.config/gh|_key\b|api.?key|environ|\bprintenv\b"
    r"|\.docker\b|\.dockercfg|\.kube\b|kubeconfig|_history\b",
    re.IGNORECASE,
)
SAFE_ENV = {"RAILS_ENV", "NODE_ENV", "CI", "TZ", "LANG", "LC_ALL", "NO_COLOR", "FORCE_COLOR", "DISABLE_SPRING",
            "COLUMNS", "TERM"}
READERS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "pwd", "echo", "printf", "seq", "cut", "tr",
    "diff", "cmp", "comm", "stat", "basename", "dirname", "realpath", "readlink", "which", "true",
    "false", "test", "column", "nl", "tac", "rev", "shasum", "du", "df", "pgrep", "sleep", "uname", "whoami",
    "id",
}
GIT_READERS = {
    "status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "ls-tree", "cat-file", "merge-base",
    "describe", "shortlog", "grep", "rev-list", "name-rev", "for-each-ref", "show-ref", "check-ignore",
    "count-objects",
}
# git, sort and Ruby's OptionParser accept any unambiguous abbreviation of a long option (--upload-pa=…)
GIT_DANGER = ["upload-pack", "receive-pack", "open-files-in-pager", "output", "ext-diff", "exec", "config"]
FETCH_OPTIONS = {"--all", "--prune", "-p", "--tags", "--no-tags", "-n", "--quiet", "-q", "--verbose", "-v",
                 "--dry-run", "--progress", "--no-progress"}
# format:write rewrites the sources, like prettier --write
NX_TARGETS = {"test", "lint", "typecheck", "type-check", "tsc", "format:check"}
GH_READS = {
    "pr": {"view", "checks", "diff", "list", "status"},
    "run": {"view", "list", "watch"},
    "issue": {"view", "list"},
    "repo": {"view"},
    "release": {"view", "list"},
}


def split_command(raw):
    """Split at unquoted ;, &&, || and |, dropping the safe redirections. None when the shell could read the
    command differently from shlex, or when it runs, writes or expands something the hook cannot judge."""
    segs, cur, i, quote = [], [], 0, None

    def cut():
        segs.append("".join(cur))
        cur.clear()

    while i < len(raw):
        c = raw[i]
        if quote == "'":
            quote = None if c == "'" else quote
            cur.append(c)
            i += 1
            continue
        if c == "$" and not ALLOWED_VAR.match(raw, i):
            return None
        if quote == '"':
            if c == "\\":
                cur.append(raw[i:i + 2])
                i += 2
                continue
            quote = None if c == '"' else quote
            cur.append(c)
            i += 1
            continue
        at_word_start = not cur or cur[-1].isspace()
        # A backslash can spell a separator that shlex and the shell read differently (find . \; …); globs
        # and braces reach files the hook never sees named; ( ) { } group and ! negates; # starts a comment
        if c == "\\" or c in "*?[]{}()!" or (c == "#" and at_word_start):
            return None
        if c in "<>&" or (c in "012" and at_word_start and raw[i + 1:i + 2] == ">"):
            m = SAFE_REDIRECT.match(raw, i)
            if m:
                cur.append(" ")
                i = m.end()
                continue
        if raw.startswith(("&&", "||"), i):
            cut()
            i += 2
        elif raw.startswith("|&", i) or c in "<>&":
            return None  # other redirections write files; a lone & starts a background job
        elif c in ";|":
            cut()
            i += 1
        else:
            if c in "'\"":
                quote = c
            cur.append(c)
            i += 1
    if quote:
        return None
    cut()
    return segs


def long_matches(arg, names):
    """True when arg is --<name> or an abbreviation of it, which git, sort and Ruby tools all accept"""
    if not arg.startswith("--") or len(arg) < 3:
        return False
    given = arg[2:].split("=", 1)[0]
    return any(n.startswith(given) for n in names)


def short_has(arg, letters):
    return bool(re.match(r"-[A-Za-z0-9]", arg)) and any(ch in arg[1:] for ch in letters)


def opts(args):
    return [a for a in args if a.startswith("-") and a != "-"]


def positional(args):
    return [a for a in args if not a.startswith("-") or a == "-"]


# Short options that take no value. Any other one may take the next word as its value (GNU -l N), and the
# hook would then read the value as the script and miss the real one
SED_FLAGS = set("nEruz")


def sed_ok(args):
    scripts, values = [], set()
    for i, a in enumerate(args):
        if i in values:
            continue
        if a.startswith("--"):
            if a.startswith("--expression="):
                scripts.append(a.split("=", 1)[1])
            elif a == "--expression" and i + 1 < len(args):
                scripts.append(args[i + 1])
                values.add(i + 1)
            elif a not in ("--quiet", "--silent", "--regexp-extended", "--posix", "--debug"):
                return False  # GNU sed takes abbreviations (--expr=, --in=), so list the full names only
        elif a.startswith("-") and a != "-":
            body = a[1:]
            k = body.find("e")
            if not set(body if k < 0 else body[:k]) <= SED_FLAGS:
                return False
            if k >= 0:
                if body[k + 1:]:
                    scripts.append(body[k + 1:])
                elif i + 1 < len(args):
                    scripts.append(args[i + 1])
                    values.add(i + 1)
                else:
                    return False
    if not scripts:
        scripts = [a for j, a in enumerate(args) if j not in values and (not a.startswith("-") or a == "-")][:1]
    return bool(scripts) and all(sed_script_ok(s) for s in scripts)


def sed_script_ok(script):
    """False when the script may run a command (e) or write a file (w, W), as a command or an s/// flag."""
    i = 0
    while i < len(script):
        c = script[i]
        if c in "sy" and i + 1 < len(script) and script[i + 1] not in " ;\n}":
            delim, parts, i = script[i + 1], 0, i + 2
            while i < len(script) and parts < 2:
                if script[i] == "\\":
                    i += 1
                elif script[i] == delim:
                    parts += 1
                i += 1
            flags = re.match(r"[A-Za-z0-9]*", script[i:]).group(0)
            if c == "s" and re.search(r"[ewW]", flags):
                return False
            i += len(flags)
            continue
        if c in "/\\":
            # /regex/ and \cregexc addresses
            delim = "/" if c == "/" else script[i + 1:i + 2]
            i += 1 if c == "/" else 2
            while i < len(script) and script[i] != delim:
                i += 2 if script[i] == "\\" else 1
        elif c in "ewW":
            return False
        i += 1
    return True


def find_ok(args):
    return not any(a in {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls"} or a.startswith("-fprint")
                   for a in args)


def awk_ok(args):
    if any(a == "-f" or a.startswith("--") for a in args):
        return False
    return not any(re.search(r"system|getline|ENVIRON|@|[|>]", a) for a in args)


def ps_ok(args):
    for i, a in enumerate(args):
        if a.startswith("-"):
            if short_has(a, "E"):
                return False  # -E prints each process's environment
        elif not (i and re.search(r"[opUuGgt]$", args[i - 1]) and args[i - 1].startswith("-")):
            return False  # BSD keywords such as eww also print the environment
    return True


def git_ok(args):
    if args[:1] == ["--no-pager"]:
        args = args[1:]
    if not args or args[0].startswith("-"):
        return False  # -C, -c, --git-dir and the like change where or how git runs
    sub, rest = args[0], args[1:]
    if any(long_matches(a, GIT_DANGER) for a in rest):
        return False
    if sub == "grep" and any(short_has(a, "O") for a in rest):
        return False  # -O<command> opens the matches in that command
    if sub in GIT_READERS:
        return True
    if sub == "fetch":
        # Only a remote and branch names: a refspec (+sha:refs/heads/main) or a path rewrites local branches
        return all(a in FETCH_OPTIONS for a in opts(rest)) and all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", a) for a in positional(rest))
    if sub == "branch":
        listing = {"--list", "-l", "--contains", "--merged", "--no-merged", "--points-at"}
        safe = listing | {"--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose"}
        return all(a in safe or a.startswith(("--sort=", "--format=")) for a in opts(rest)) and (
            not positional(rest) or bool(listing & set(rest)))
    if sub == "remote":
        return rest in ([], ["-v"], ["--verbose"]) or rest[:1] == ["get-url"]
    if sub in ("worktree", "stash"):
        return rest[:1] == ["list"] or (sub == "stash" and rest[:1] == ["show"])
    if sub == "tag":
        return not positional(rest) or bool({"-l", "--list", "--contains", "--points-at"} & set(rest))
    if sub == "reflog":
        return rest[:1] in ([], ["show"])
    return False


def gh_ok(args):
    return (len(args) >= 2 and args[1] in GH_READS.get(args[0], set())
            and not any(a in ("-w", "--web") for a in args))


def bundle_ok(args):
    if args[:1] in (["info"], ["list"]):
        return True
    if args[:1] != ["exec"] or len(args) < 2:
        return False
    tool, rest = args[1], args[2:]
    if tool == "rspec":
        # -o/--out writes a file, -r/--require and -O/--options load other code, --init writes files
        return not any(short_has(a, "orO") or long_matches(a, ["out", "require", "options", "init"]) for a in rest)
    if tool == "rubocop":
        return not any(short_has(a, "or") or long_matches(a, ["out", "require", "plugin", "init", "auto-gen-config"])
                       for a in rest)
    return False


def nx_targets(rest):
    targets = []
    for i, a in enumerate(rest):
        if a in ("-t", "--target", "--targets"):
            for t in rest[i + 1:]:
                if t.startswith("-"):
                    break
                targets += t.split(",")
        elif a.startswith(("-t=", "--target=", "--targets=")):
            targets += a.split("=", 1)[1].split(",")
        elif a.startswith("-t") or (a.startswith("--t") and long_matches(a, ["targets"])):
            return None  # -tdeploy and abbreviations: unclear how nx reads them
    return targets


def pnpm_ok(args):
    if args[:1] == ["exec"]:
        args = args[1:]
    if not args:
        return False
    tool, rest = args[0], args[1:]
    if tool == "jest":
        return not any(a.startswith("--outputFile") for a in rest)
    if tool == "tsc":
        return "--noEmit" in rest and not any(a.startswith(("--out", "--build", "-b", "--generateTrace")) for a in rest)
    if tool == "eslint":
        return not any(short_has(a, "o") or a.startswith("--output-file") for a in rest)
    if tool == "prettier":
        return bool({"--check", "-c", "--list-different", "-l"} & set(rest)) and not any(
            a in ("--write", "-w") for a in rest)
    if tool == "knip":
        return not any(a.startswith("--fix") for a in rest)  # deletes files and exports
    if tool in NX_TARGETS:
        return True  # package.json scripts of the repository, as for rspec
    if tool != "nx" or not rest:
        return False
    sub, rest = rest[0], rest[1:]
    if sub in NX_TARGETS:
        return True
    if sub == "run":
        # project:target[:configuration]
        parts = rest[0].split(":") if rest else []
        return len(parts) in (2, 3) and parts[1] in NX_TARGETS
    if sub in ("run-many", "affected"):
        targets = nx_targets(rest)
        return bool(targets) and all(t in NX_TARGETS for t in targets)
    return False


def cd_ok(args):
    """Only into the folder Devin works in. git, rspec and pnpm run code named in the repository's config
    (core.fsmonitor, a pager, .rubocop.yml, package.json), so moving to another repository first is a way to
    run that repository's code, as git -C would be."""
    if len(args) != 1 or "$" in args[0] or args[0] == "-":
        return False
    target = args[0]
    if not target.startswith(("/", "~")):
        return ".." not in target.split("/")
    root = os.environ.get("DEVIN_PROJECT_DIR")
    if not root:
        return False
    root = os.path.realpath(root)
    if root in ("/", os.path.realpath(os.path.expanduser("~"))):
        return False
    path = os.path.realpath(os.path.expanduser(target))
    return path == root or path.startswith(root + os.sep)


CHECKS = {
    "cd": cd_ok,
    "rg": lambda a: not any(x.startswith(("--pre", "--hostname-bin")) for x in a),
    "fd": lambda a: not any(short_has(x, "xX") or x.startswith("--exec") for x in a),
    "sort": lambda a: not any(short_has(x, "o") or long_matches(x, ["output", "compress-program"]) for x in a),
    "uniq": lambda a: len(positional(a)) <= 1,
    "tree": lambda a: not any(short_has(x, "o") for x in a),
    "date": lambda a: not any(x.startswith(("-s", "--s", "-f")) for x in a),
    "jq": lambda a: not any(re.search(r"\benv\b|\$ENV|input_filename", x) for x in a),
    "sed": sed_ok, "find": find_ok, "awk": awk_ok, "ps": ps_ok,
    "git": git_ok, "gh": gh_ok, "bundle": bundle_ok, "pnpm": pnpm_ok,
}


def unwrap(seg):
    """The command itself, without leading variable assignments and timeout; None if an assignment is not safe."""
    while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0]):
        name, value = seg[0].split("=", 1)
        if name not in SAFE_ENV or (name in ("RAILS_ENV", "NODE_ENV") and value not in ("test", "development")):
            return None
        seg = seg[1:]
    if seg[:1] == ["timeout"] and len(seg) > 2 and re.fullmatch(r"\d+[smh]?", seg[1]):
        seg = seg[2:]
    return seg


def command_ok(seg):
    seg = unwrap(seg)
    if not seg:
        return False
    cmd, args = seg[0], seg[1:]
    if cmd in READERS:
        return True
    return cmd in CHECKS and CHECKS[cmd](args)


def deny_prefixes():
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "devin", "config.json")
    try:
        with open(path) as f:
            rules = json.load(f).get("permissions", {}).get("deny", [])
    except (OSError, ValueError):
        return None
    return [r[5:-1].split() for r in rules if r.startswith("Exec(") and r.endswith(")")]


def approvable(command):
    if not command.strip() or REFUSE_RAW.search(command) or SENSITIVE.search(command):
        return False
    raw_segments = split_command(command)
    denied = deny_prefixes()
    if raw_segments is None or denied is None:
        return False
    for raw in raw_segments:
        try:
            seg = shlex.split(raw)
        except ValueError:
            return False
        if not seg or any(SENSITIVE.search(t) for t in seg) or not command_ok(seg):
            return False
        # Devin applies a deny rule before asking, but a hook approval must never be what lets one through
        if any(unwrap(seg)[:len(d)] == d for d in denied):
            return False
    return True


def main():
    event = json.load(sys.stdin)
    if event.get("tool_name") != "exec":
        return
    command = (event.get("tool_input") or {}).get("command") or ""
    if approvable(command):
        print(json.dumps({"decision": "approve", "reason": "read, test, lint or type-check only (devin-tmux hook)"}))


if __name__ == "__main__":
    main()
