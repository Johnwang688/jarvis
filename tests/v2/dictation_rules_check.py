"""Free rules checks, synthetic inputs only. No runtime configuration imports."""
from __future__ import annotations

from pathlib import Path
import random
import subprocess
import sys
import tempfile

from jarvis.v2 import dictation_guard as guard, dictation_rules as rules
from voiceclean import corrupt, templates

ROOT = Path(__file__).resolve().parents[2]
PASSED = FAILED = 0


def check(name, ok):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print('  ok  ' + name)
    else:
        FAILED += 1
        print(' FAIL ' + name)


def hand_cases():
    cases = [
        ('um list  the files uh', 'terminal', 'list the files'),
        ('echo hello comma world period', 'terminal', 'echo hello comma world period'),
        ('git status', 'terminal', 'git status'),
        ('echo $HOME | wc -l', 'terminal', 'echo $HOME | wc -l'),
        ('i want I want to test', 'other', 'I want to test.'),
        ('the the robot is ready', 'other', 'The robot is ready.'),
        ('i knew that that was true', 'other', 'I knew that that was true.'),
        ('we had had enough', 'other', 'We had had enough.'),
        ('um uh hello there friend', 'other', 'Hello there friend.'),
        ('aluminum album hum', 'other', 'Aluminum album hum.'),
        ('send this comma then ask question mark is it ready exclamation point', 'other',
         'Send this, then ask? Is it ready!'),
        ('well done exclamation mark full stop', 'other', 'Well done!.'),
        ('this is done period', 'other', 'This is done.'),
        ('the period is over', 'other', 'The period is over.'),
        ('it costs a grace period period', 'other', 'It costs a grace period.'),
        ('use colon and semicolon here', 'other', 'Use colon and semicolon here.'),
        ("i'm ready full stop i've tested it full stop i'll go full stop i'd wait", 'other',
         "I'm ready. I've tested it. I'll go. I'd wait."),
        ('meet tuesday no wait wednesday at five', 'other', 'Meet tuesday no wait wednesday at five.'),
        ('we need twenty five parts', 'other', 'We need twenty five parts.'),
        ('hello there', 'other', 'Hello there'),
        ('one two three', 'other', 'One two three.'),
        ('', 'other', ''),
        ('um uh', 'other', ''),
        ('  um\tgit   status uh ', 'terminal', 'git status'),
        ('hello\x00world', 'other', 'hello\x00world'),
    ]
    for i, (raw, app, expected) in enumerate(cases):
        check(f'hand case {i}', rules.clean(raw, app) == expected)
    check('spoken-punctuation words pass as themselves', guard.check('echo period', 'echo period').ok)
    check('guard identity conflict: control characters', not guard.check('a\x00b', 'a\x00b').ok)


def sweep(label, inputs):
    invalid = {'terminal': 0, 'other': 0}
    violations = 0
    for raw in inputs:
        for app in invalid:
            cleaned = rules.clean(raw, app)
            if not guard.check(raw, cleaned).ok:
                invalid[app] += 1
                # The brief's absolute invariant is impossible for some raw
                # inputs. Enforce its explicit unchanged-input fallback, and
                # require that the identity itself really fails the guard.
                violations += cleaned != raw or guard.check(raw, raw).ok
    check(label + ': every result passes or is the required raw fallback', violations == 0)
    print(f"  {label}: raw guard conflicts terminal={invalid['terminal']} other={invalid['other']}")


def isolation():
    code = ("import sys; import jarvis.v2.dictation_rules; "
            "print(sorted(m for m in sys.modules if m.startswith('jarvis')))")
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True,
                             text=True, env={'HOME': home, 'PYTHONPATH': str(ROOT), 'PATH': '/usr/bin:/bin'})
    check('rules import only guard from Jarvis', out.returncode == 0 and out.stdout.strip() ==
          "['jarvis', 'jarvis.v2', 'jarvis.v2.dictation_guard', 'jarvis.v2.dictation_rules']")


def main():
    hand_cases()
    sentences = list(templates.generate(17, 200))
    sweep('3000 corruption seeds', (corrupt.corrupt(sentences[seed % len(sentences)]['text'],
          random.Random(seed)).messy for seed in range(3000)))
    rng = random.Random(29)
    words = ('we test the robot I want that had one twenty five percent ' +
             ' '.join(sorted(guard.FILLERS))).split() + [
                 'comma', 'question mark', 'period', 'full stop', 'exclamation mark',
                 'colon', 'semicolon', '$', '|', '>', '#', '_', '!', '?', "i'm", '\x00', '\n']
    sweep('3000 random strings', (' '.join(rng.choice(words) for _ in range(rng.randrange(0, 25)))
                                 for _ in range(3000)))
    isolation()
    print(f'dictation_rules_check: {PASSED} passed, {FAILED} failed')
    return bool(FAILED)


if __name__ == '__main__':
    raise SystemExit(main())
