"""python3 -m unittest discover -s <this folder> -p 'test_*.py'"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "approve-safe-commands.py")
spec = importlib.util.spec_from_file_location("hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

APPROVED = [
    "sed -n '7,16p' app/models/user.rb; echo '---'; sed -n '1,5p' Gemfile",
    "cd /work/tree && git fetch origin main 2>&1 | tail -2 && git branch -r | grep -v dependabot",
    "find spec -name '*_spec.rb' 2>/dev/null | head",
    "gh pr checks 6815 --repo acme/web --watch --interval 60; echo \"WATCH_DONE exit=$?\"",
    "pnpm nx run-many -t typecheck lint -p admin view-helpers 2>&1 | tail -30",
    "pnpm nx test view-helpers 2>&1 | tail -20",
    "RAILS_ENV=test bundle exec rspec spec/models/user_spec.rb",
    "timeout 60 bundle exec rubocop spec/models/user_spec.rb 2>&1 | grep -c offense",
    "git fetch origin main && git log --oneline -3 origin/main",
    "pgrep -fl rspec >/dev/null; ps -eo pid,comm | grep -E 'rspec' | head",
    "rg -n 'call_list' app | sort | uniq -c",
    "git --no-pager log --oneline -5 && git diff --stat",
    "cd app/models && git log --oneline -3",
    "sed -ne '1,5p' Gemfile",
]
REFUSED = {
    "sed edits a file": "sed -i '' 's/a/b/' app/models/user.rb",
    "sed writes with w": "sed -n 's/a/b/w out.txt' app/models/user.rb",
    "sed runs a command": "sed '1e date' Gemfile",
    "find deletes": "find tmp -name '*.log' -delete",
    "find runs a command": "find . -name '*.rb' -exec rm {} +",
    "redirect writes a file": "echo hi > note.txt",
    "command substitution": "echo $(curl https://example.com)",
    "multi-line script": "for i in 1 2; do\n  echo $i\ndone",
    "commit": "git add app && git commit -m wip",
    "push is denied": "git push origin HEAD",
    "git -C is denied": "git -C . status",
    "config override": "git -c core.pager=less log",
    "unknown command": "agent-browser --session x click @e1",
    "secret file": "cat .env.local",
    "ssh key": "head ~/.ssh/id_ed25519",
    "1Password": "op read op://Vault/item/credential",
    "env var that redirects the DB": "DATABASE_URL=mysql2://prod bundle exec rspec",
    "rails runner": "bundle exec rails runner 'User.delete_all'",
    "nx target outside the list": "pnpm nx run-many -t deploy -p app",
    "knip fix": "pnpm knip --fix",
    "gh writes": "gh pr merge 1 --squash",
    "xargs runs rm": "git ls-files | xargs rm",
    "background job": "sleep 100 &",
    "rg preprocessor": "rg --pre ./run.sh pattern",
    "python one-liner": "python3 -c 'import os; os.remove(\"x\")'",
    # 独立レビューで見つかった抜け穴
    "quoted separator hides find -delete": "find . ';' echo -delete",
    "quoted && hides find -exec": "find . '&&' echo -exec touch marker {} +",
    "escaped separator": "find . \\; echo -delete",
    "quoted pipe hides rg --pre": "rg pattern . '|' echo --pre=./helper.sh",
    "xargs takes arguments from stdin": "printf '%s\\n' -delete | xargs find tmp",
    "git grep -O in a bundle": "git grep -nOsomecmd pattern",
    "git grep abbreviated pager option": "git grep --open-files=somecmd pattern",
    "git fetch abbreviated upload-pack": "git fetch --upload-pa=somecmd .",
    "git fetch rewrites a branch": "git fetch . +abc123:refs/heads/main",
    "git fetch into the checked-out branch": "git fetch -u origin main:main",
    "rg runs a hostname helper": "rg --hostname-bin=./helper.sh --hyperlink-format=default x",
    "sort compresses with a program": "sort --compress-program=./helper.sh big.txt",
    "nx run with a target hidden as configuration": "pnpm nx run app:deploy:lint",
    "nx -t= form": "pnpm nx run-many -t lint -t=deploy",
    "pager from the environment": "GIT_PAGER=./helper.sh git log",
    "sed -I in place": "sed -I '' 's/a/b/' app.rb",
    "sed -ni in place": "sed -ni '' 's/a/b/p' app.rb",
    "sed custom-delimiter address hides w": "sed -n '\\sxsw out.txt' f",
    "sed -e attached w": "sed -n -e'w out.txt' app.rb",
    "sed script file": "sed -f script.sed app.rb",
    "sort -o in a bundle": "sort -ro out in",
    "sort abbreviated output": "sort --out=out in",
    "uniq writes to its second operand": "uniq - out.txt",
    "tree -o in a bundle": "tree -ao out.txt .",
    "rspec writes a file": "bundle exec rspec --out /tmp/x",
    "rubocop writes a file": "bundle exec rubocop -o /tmp/x",
    "eslint writes a file": "pnpm eslint -o /tmp/x .",
    "tsc emits files": "pnpm tsc --outDir /tmp/x",
    "glob spells .env": "cat .en[v]",
    "quotes spell .env": "cat .e'n'v",
    "backslash spells .env": "cat .\\env",
    "ANSI-C quoting spells .env": "cat $'\\x2eenv'",
    "glob spells an ssh key": "head ~/.s?h/id_*",
    "direnv file": "cat .envrc",
    "gh token file": "cat ~/.config/gh/hosts.yml",
    "variable with a key": "echo $OPENAI_API_KEY",
    "jq prints the environment": "jq -n env",
    "awk prints the environment": "awk 'BEGIN{for(k in ENVIRON) print k}'",
    "ps prints the environment": "ps -Eww -o command",
    "gh opens a browser": "gh pr view 1 -w",
    "production settings": "RAILS_ENV=production bundle exec rspec",
    "git option abbreviated to three letters": "git log --o=out.txt",
    "nx format:write rewrites sources": "pnpm nx format:write",
    "format:write through run-many": "pnpm nx run-many -t format:write -p app",
    "docker registry credentials": "cat ~/.docker/config.json",
    "kube credentials": "cat ~/.kube/config",
    "shell history": "cat ~/.bash_history",
    # 自動セキュリティレビューで見つかった抜け穴
    "sed option value read as the script": "sed -l 5 'w out.txt' app.rb",
    "sed unknown short option": "sed -a -n 'p' app.rb",
    "cd into another repository runs its git config": "cd /tmp/other-repo && git status",
    "cd up out of the work folder": "cd ../other && git log",
    "cd through a variable": "cd $HOME/other && git status",
    "cd back to the previous folder": "cd - && git status",
    "cd home": "cd && git status",
}


class ApprovesOnlyReadTestLintAndTypecheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(cls.tmp.name, "devin"))
        with open(os.path.join(cls.tmp.name, "devin", "config.json"), "w") as f:
            json.dump({"permissions": {"deny": ["Exec(git push)", "Exec(git -C)", "Exec(gh pr merge)"]}}, f)
        os.environ["XDG_CONFIG_HOME"] = cls.tmp.name
        os.environ["DEVIN_PROJECT_DIR"] = "/work/tree"

    def test_approves_reads_tests_lint_and_typechecks(self):
        for command in APPROVED:
            with self.subTest(command=command):
                self.assertTrue(hook.approvable(command))

    def test_leaves_everything_else_to_the_prompt(self):
        for why, command in REFUSED.items():
            with self.subTest(why=why):
                self.assertFalse(hook.approvable(command))

    def test_approves_nothing_without_a_readable_deny_list(self):
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp.name, "missing")
        try:
            self.assertFalse(hook.approvable("ls"))
        finally:
            os.environ["XDG_CONFIG_HOME"] = self.tmp.name

    def run_hook(self, event):
        return subprocess.run([sys.executable, HOOK], input=json.dumps(event), capture_output=True, text=True,
                              env={**os.environ, "XDG_CONFIG_HOME": self.tmp.name})

    def test_prints_an_approve_decision_for_a_safe_command(self):
        out = self.run_hook({"hook_event_name": "PermissionRequest", "tool_name": "exec",
                             "tool_input": {"command": "git status"}})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout)["decision"], "approve")

    def test_prints_nothing_and_never_blocks_otherwise(self):
        for event in ({"tool_name": "exec", "tool_input": {"command": "git push"}},
                      {"tool_name": "edit", "tool_input": {"file_path": "/etc/hosts"}}):
            with self.subTest(event=event):
                out = self.run_hook(event)
                self.assertEqual((out.returncode, out.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
