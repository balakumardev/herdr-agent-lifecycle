"""Synthetic fixtures only; the native corpus was observed on Claude 2.1.291."""
import importlib
import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RULES = (
    "git push:*", "git -C * push*", "git commit:*", "gt modify:*",
    "git -C * commit *", "gt create:*", "gt submit:*", "git *", "rm *",
)
REVIEWS = frozenset(range(7))
ALL_RULES = frozenset(range(9))

# (label, input, native matched rule indexes, supported by this closed parser)
NATIVE_CORPUS = (
    ("plain", "pwd", set(), True),
    ("multiline", "pwd\nprintf ok", set(), True),
    ("compound", "printf ok && pwd", set(), True),
    ("literal-mention", "printf '%s' 'git push'", set(), True),
    ("quoted-heredoc", "python3 - <<'PY'\nprint('git push')\nPY", set(), True),
    ("dollar-substitution", "echo $(date)", REVIEWS, False),
    ("dollar-variable", 'echo "$HOME"', set(), False),
    ("literal-dollar", "echo '$HOME'", set(), True),
    ("simple-push", "git push", {0, 7}, True),
    ("compound-push", "pwd && git push", {0, 7}, True),
    ("multiline-push", "pwd\ngit push", {0, 7}, True),
    ("environment-push", "FOO=bar git push", {0, 7}, True),
    ("git-c-push", "git -C . push", {1, 7}, True),
    ("redirect-nongit", "printf ok >/dev/null", set(), True),
    ("loop-nongit", "for x in a b; do printf ok; done", set(), False),
    ("comment-nongit", "# git push\npwd", set(), True),
    ("quoted-substitution", 'echo "$(date)"', REVIEWS, False),
    ("simple-commit", "git commit -m test", {2, 7}, True),
    ("git-c-commit", "git -C . commit -m test", {4, 7}, True),
    ("quoted-subcommand", "git 'push'", {0, 7}, True),
    ("unknown-variable", 'echo "$UNDEFINED_PROBE"', ALL_RULES, False),
    ("command-wrapper", "command git push", set(), True),
    ("timeout-wrapper", "timeout 1 git push", set(), True),
    ("pipeline-nongit", "printf ok | cat", set(), True),
    ("semicolon-nongit", "printf ok; pwd", set(), True),
    ("function-nongit", "probe_fn() { printf ok; }; probe_fn", ALL_RULES, False),
    ("settle", "sleep 1", set(), True),
)


class BashConditionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("bash_conditions"),
                             "the conservative Bash matcher has not been implemented")
        self.match = importlib.import_module("bash_conditions").match_bash_condition

    def check_cases(self, cases, pattern="git push:*"):
        for command, expected in cases:
            with self.subTest(command=command, pattern=pattern):
                self.assertIs(self.match(pattern, command), expected)

    def test_native_corpus_never_resolves_against_observed_dispatch(self):
        self.assertEqual(len(NATIVE_CORPUS), 27)
        for label, command, native_matches, supported in NATIVE_CORPUS:
            for index, pattern in enumerate(RULES):
                with self.subTest(case=label, pattern=pattern):
                    expected = index in native_matches if supported else None
                    actual = self.match(pattern, command)
                    self.assertIs(actual, expected)
                    if actual is not None:
                        self.assertIs(actual, index in native_matches)

    def test_any_literal_subcommand_can_match(self):
        self.check_cases([
            ("printf ok; git push", True),
            ("printf ok || git push", True),
            ("printf ok | git push", True),
            ("printf ok |& git push", True),
            ("printf ok &&\n# explanatory comment\ngit push", True),
            ("git push && printf ok", True),
            ("\n\tpwd\n\nprintf ok;\n", False),
            ("pwd &&\n\nprintf ok", False),
            ("echo git push; printf ok | cat", False),
        ])

    def test_quotes_and_literal_escapes_keep_shell_boundaries(self):
        self.check_cases([
            ("printf '%s\\n' 'finished'", False),
            ("echo 'ok; git push'", False),
            ('echo "ok && git push"', False),
            (r"echo ok\;git", False),
            (r"git pu\sh", True),
            ('git pu"sh"', True),
            ("git 'push now'", True),  # Native joins argv, even this one argument.
            (r'echo "\$HOME"', False),
            (r'echo "a\"; git push"', False),
            ("echo '# git push'", False),
            ("echo ok # git push\nprintf done", False),
            ("echo foo#bar\ngit push", True),
            ("echo 'git push\nmore text'", False),
        ])

    def test_literal_assignments_are_stripped_only_before_the_command(self):
        self.check_cases([
            ("FIRST=one SECOND='two words' git push", True),
            ('FIRST="two words" git push', True),
            ("FIRST='$(date)' git push", True),
            ("FIRST=one\ngit push", True),
            ("FIRST=one", False),
            ("echo FIRST=one git push", False),
            ("FIRST=one pwd; SECOND=two printf ok", False),
        ])

    def test_redirects_never_turn_adjacent_fd_numbers_into_command_names(self):
        self.check_cases([
            (">out git push", True),
            ("2>out git push", True),
            ("git push 2>out", True),
            ("2>out <in git push", True),
            ("git 2>out push", None),
            ("git 12>out push", None),
            ("git 2>>out push", None),
            ("git 2>&1 push", None),
            ("git <in push >out", None),
            ('git >"two words" push', None),
            ("FIRST=one 2>out git push", True),
            ("2>out FIRST=one git push", None),
            ("echo 2>out\ngit push", True),
            ("printf ok >git; printf ok >>push", False),
            ("2 >out git push", None),
            ("git 2 >out push", None),
            ("echo2>out git push", None),
        ])

    def test_quoted_heredoc_bodies_are_not_shell_commands(self):
        self.check_cases([
            ("cat <<'END'\ngit push\nEND\n", False),
            ('cat <<"END"\n$VAR $(git push)\nEND\n', False),
            ("cat <<'END'\n' ) && ${X}\nEND\n", False),
            ("cat <<'END'\ngit push\nEND\ngit push", True),
            ("cat <<-'END'\n\tgit push\n\tEND\npwd", False),
            ("cat <<-'END'\n\tignored\n\tEND\ngit push", True),
            ("cat <<'END' # comment\ngit push\nEND", False),
            ("cat 3<<'END'\nignored\nEND\ngit push", True),
        ])

    def test_sequential_heredocs_are_supported_and_shared_line_ones_are_unknown(self):
        self.check_cases([
            ("cat <<'A'\ngit push\nA\ncat <<'B'\ngit push\nB\npwd", False),
            ("cat <<'A'\ntext\nA\ncat <<'B'\ntext\nB\ngit push", True),
            ("cat <<'A' <<'B'\ngit push\nA\ngit commit\nB\npwd", None),
            ("cat <<'A' <<-'B'\nB\nA\n\tgit push\n\tB\ngit push", None),
            ("cat <<'A' | cat <<'B'\ngit push\nA\ngit push\nB\n", None),
            ("cat <<'A'; cat <<'B'\ngit push\nA\ngit push\nB\ngit push", None),
            ("cat <<'A' <<'B'\ntext\nA\n", None),
            ("cat <<'A' <<'B'\ntext\nB\nA\n", None),
        ])

    def test_native_literal_tildes_and_between_word_continuations(self):
        self.check_cases([
            ("cd ~/folder\nprintf ok", False),
            ("cd ~", False),
            ("cd ~other/folder", False),
            ("printf ok \\\n more", False),
            ("pwd | \\\n cat", False),
            ("pwd && \\\n git push", True),
            ("git \\\n push", True),
            ("git pu\\\nsh", None),
            ("\\\ngit push", None),
        ])
        self.check_cases([("git -C ~/folder push", True)], "git -C * push*")
        self.check_cases([("cd ~/folder", True)], "cd ~/*")
        self.check_cases([("cd ~/folder", False)], "cd /tmp/*")

    def test_native_run_all_cases_cannot_return_false_for_other_review_patterns(self):
        commands = ["printf >out ok", "git 2>out push", "git 12>out push", "git 2>&1 push",
                    "2 >out git push", "2>out FIRST=one git push", "git pu\\\nsh",
                    "cat <<'A' <<'B'\ngit push\nA\ngit commit\nB\npwd"]
        for pattern in RULES:
            self.check_cases([(command, None) for command in commands], pattern)

    def test_missing_or_unproven_heredoc_terminators_stay_unknown(self):
        self.check_cases([
            ("cat <<'END'\nbody\n", None),
            ("cat <<'END'\nbody\nEND ", None),
            ("cat <<'END'\nbody\n END\ngit push", None),
            ("cat <<-'END'\nbody\n END\n", None),
            ("cat <<END\nliteral today\nEND", None),
            ("cat <<'END' &&\nbody\nEND\npwd", None),
        ])

    def test_unsupported_syntax_anywhere_overrides_earlier_matches(self):
        tails = [
            "echo $NAME", 'echo "$HOME"', "echo $(date)", "echo `date`",
            "echo ${NAME}", "echo $((1 + 2))", "echo $'text'",
            "probe_fn() { printf ok; }; probe_fn", "(pwd)", "{ pwd; }",
            "for x in a; do pwd; done", "if true; then pwd; fi",
            "echo *.py", "echo file[12]", "echo ?", "echo ~[name]",
            "cat <(printf ok)", "cat <<<text", "{fd}>out pwd", "! pwd",
            "eval 'git push'", "export NAME=value", r"echo two\ words",
        ]
        for head in ["pwd", "git push"]:
            self.check_cases([(head + "; " + tail, None) for tail in tails])

    def test_malformed_input_never_becomes_a_false_skip(self):
        self.check_cases([
            ("echo 'unfinished", None), ('echo "unfinished', None),
            ("echo trailing\\", None), ("pwd &&", None), ("pwd |", None),
            ("pwd ||\n# no right side", None), ("; pwd", None),
            ("pwd;; git push", None), ("pwd && ; git push", None),
            ("git 2>", None), ("git >; push", None), ("pwd\x00", None),
            ("pwd\r\nprintf ok", None), ("echo\u00a0word", None),
            ("echo\ud800", None), ("echo " + "x" * 10001, None),
            (None, None), ([], None),
            ("", False), (" \t\n# comment only\n", False),
        ])

    def test_unproven_shell_dialect_forms_and_byte_limits_stay_unknown(self):
        self.check_cases([
            ("''", None), ('""', None), ("echo =git", None),
            ("echo '<1-9>'", None), ("echo '~[name]'", None),
            ("echo " + "\U0001f600" * 5000, None),
        ])

    def test_prefix_boundaries_and_xargs_variant(self):
        self.check_cases([
            ("git push", True), ("git pushed", False), ("gitpush", False),
            ("xargs git push", True), ("xargs git push origin main", True),
            ("xargs -n1 git push", False),
            ("command git push", False), ("timeout 1 git push", False),
        ])

    def test_star_matching_is_anchored_and_normalizes_spaces_and_tabs(self):
        for pattern, command, expected in [
            ("git push", "xargs git push", True),
            ("git push *", "git push", True),
            ("git push *", "git pushed", False),
            ("git push*", "git pushed", True),
            ("git -C * commit *", "git -C . commit", False),
            ("git -C * commit *", "git -C . commit -m x", True),
            ("git\t push *", "git\tpush", True),
            ("  git  push *  ", "git push origin", True),
            ("git log * main", "git log main", False),
            ("git log * main", "git log --oneline main", True),
            ("git *", "echo git push", False),
            ("*", "pwd", True),
            ("echo *", "echo 'one\ntwo'", True),
            ("git commit:*", "git 'commit -m text'", True),
        ]:
            self.check_cases([(command, expected)], pattern)

    def test_unproven_patterns_stay_unknown(self):
        for pattern in [None, [], "", "git ?", "git [pc]*", "git \\*", "git\npush", "git\x00*",
                        'git "push"', "git $NAME", "git *:*", "*" * 10001]:
            with self.subTest(pattern=pattern):
                self.assertIsNone(self.match(pattern, "pwd"))

    def test_native_raw_refusals_apply_before_discarding_comments_and_heredocs(self):
        commands = [
            "python3 - <<'PY'\nprint({'status': 'complete'})\nPY",
            'python3 - <<\'PY\'\nprint({"status": "complete"})\nPY',
            "pwd # =git",
            "printf '%s\\n' 'x =git'",
            "pwd # {'status': 'complete'}",
            "cat <<'END'\nhello\\\nworld\nEND",
            "pwd # comment\\\nprintf ok",
        ]
        for pattern in RULES:
            self.check_cases([(command, None) for command in commands], pattern)

    def test_raw_refusal_guards_keep_native_narrow_controls_precise(self):
        self.check_cases([
            ("FOO=bar git push", True),
            ("git \\\n push", True),
            ("printf '%s\\n' 'x=git'", False),
            ("printf '%s\\n' '=git'", False),
            ('printf \'%s\\n\' \'{"status":"complete"}\'', False),
            ("cat <<'END'\nhello \\\nworld\nEND", False),
        ])

    def test_native_invisible_whitespace_refusals_remain_unknown_inside_quotes(self):
        for character in ("\u200b", "\ufeff"):
            self.check_cases([("printf '%s' '" + character + "'", None)])


if __name__ == "__main__":
    unittest.main()
