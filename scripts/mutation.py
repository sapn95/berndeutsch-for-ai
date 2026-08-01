#!/usr/bin/env python3
"""mutation — measure whether the tests can fail, instead of arguing about it.

Sixteen rounds of review kept producing the same finding in different words:
"this check cannot fail". Five of ten, five of ten, six of thirteen. That is a
question about the test suite, and a reviewer answering it is guessing, however
carefully. A mutation tester answers it by construction: it changes the code
under test and checks that the suite notices. An assertion that cannot fail
kills nothing, and the number says so without anybody's opinion in it.

    scripts/mutation.py                  # detection core, against evaluate.py
    scripts/mutation.py --full           # whole hook, against selftest.py too
    scripts/mutation.py --report-only    # re-print the last run's numbers

Needs cosmic-ray (`pip install cosmic-ray`), which is the reason this is not
part of scripts/selftest.py: the rest of the repository is stdlib-only and
stays that way. cosmic-ray was chosen over mutmut because mutmut 3 is wired to
pytest and this repository has no pytest; cosmic-ray only requires a command
that exits 0 when the tests pass.

It mutates files ON DISK, so it runs against a copy of the tree in a temporary
directory and never touches the working tree.

Reading the output: a score of 100% for a function means every change this tool
knows how to make was caught. A score of 0% means the function is, as far as
the tests are concerned, not tested at all, whatever the line coverage says.
"""

import argparse
import ast
import collections
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = "hooks/berndeutsch_gate.py"
# The functions that decide whether a prompt is Bernese. The rest of the file
# is state files, injection and IO, which selftest.py covers by running the
# hook as a subprocess; --full includes those.
DETECTION = ("word_tokens", "word_matches", "scan_window", "is_dialect",
             "sentence_initial", "_is_word_char")
# Under the repository, not the system temp directory. gettempdir() on macOS
# is a private per-user path that is cleaned periodically, so --report-only
# would work or not depending on how long ago the run was. Gitignored.
SESSION = REPO / ".mutation"
# The floor, recorded from a real run so a change cannot quietly lower it.
# 19% when this was first measured; the gap was almost entirely scan_window,
# where 75 of 75 mutations survived, and is_dialect, where 35 of 42 did.
# Raise it when the measurement rises. Never lower it to make a run pass.
MIN_SCORE = 80.0


def stage():
    """A copy of the tracked tree, because cosmic-ray edits files in place."""
    SESSION.mkdir(parents=True, exist_ok=True)
    # Keep Spotlight out. This directory is a whole copy of the tree, written
    # and deleted on every run, and it lives under the user's home where macOS
    # indexes by default. Indexing it made the Finder redraw repeatedly while a
    # run was in progress. The marker file is the documented way to opt a
    # directory out, and it is inert everywhere else.
    (SESSION / ".metadata_never_index").touch(exist_ok=True)
    work = SESSION / "tree"
    if work.exists():
        shutil.rmtree(work)
    # --others --exclude-standard as well as tracked files: a new test or a new
    # fixture is exactly what one is measuring, and staging only tracked files
    # measured a tree whose tests could not even run.
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    for name in listed:
        src = REPO / name
        if not src.is_file():
            continue
        dst = work / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return work


def owner_map(source):
    """line number -> enclosing function name, innermost wins."""
    spans = sorted((n.lineno, n.end_lineno, n.name) for n in ast.walk(ast.parse(source))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))

    def owner(line):
        found = "<module level>"
        for lo, hi, name in spans:
            if lo <= line <= hi:
                found = name
        return found
    return owner


def completeness(db_path):
    """(finished, planned). A partial session is not a smaller measurement."""
    db = sqlite3.connect(db_path)
    planned = db.execute("select count(*) from work_items").fetchone()[0]
    finished = db.execute("select count(*) from work_results").fetchone()[0]
    return finished, planned


def score(db_path, source, only=None):
    """Per-function mutation score from a cosmic-ray session."""
    owner = owner_map(source)
    db = sqlite3.connect(db_path)
    rows = db.execute(
        "select m.start_pos_row, r.test_outcome from mutation_specs m "
        "join work_results r on r.job_id = m.job_id")
    total, survived = collections.Counter(), collections.Counter()
    for line, outcome in rows:
        name = owner(line)
        if only and name not in only:
            continue
        total[name] += 1
        # cosmic-ray writes SURVIVED / KILLED / INCOMPETENT. Anything that is
        # not KILLED is a mutation the suite failed to notice; INCOMPETENT
        # (the mutant does not even run) is not counted as a win either, so it
        # is left in the denominator rather than quietly dropped.
        if outcome != "KILLED":
            survived[name] += 1
    return total, survived


def show(total, survived, label):
    if not total:
        print("no mutants recorded")
        return 0.0
    print(f"\n{label}")
    print(f"  {'function':24} {'caught':>7} {'total':>6} {'score':>7}")
    for name in sorted(total, key=lambda n: (survived[n] / total[n], -total[n])):
        t, s = total[name], survived[name]
        print(f"  {name:24} {t - s:7} {t:6} {100 * (t - s) / t:6.0f}%")
    t, s = sum(total.values()), sum(survived.values())
    overall = 100 * (t - s) / t
    print(f"  {'':24} {'':7} {'':6} {'-' * 7}")
    print(f"  {'overall':24} {t - s:7} {t:6} {overall:6.0f}%")
    return overall


def main():
    ap = argparse.ArgumentParser(prog="mutation", description=__doc__.splitlines()[0])
    ap.add_argument("--full", action="store_true",
                    help="score the whole hook, not only the detection core")
    ap.add_argument("--report-only", action="store_true",
                    help="re-print the previous run without re-running it")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    if not shutil.which("cosmic-ray"):
        print("cosmic-ray not found: pip install cosmic-ray", file=sys.stderr)
        print("Refusing to report a mutation score without measuring one.",
              file=sys.stderr)
        return 3

    db = SESSION / "session.sqlite"
    # The measured copy, not the working tree. Line numbers in the session
    # belong to the source cosmic-ray actually mutated; read them against a
    # file that has been edited since and every mutant is attributed to the
    # wrong function, silently and plausibly.
    measured = SESSION / "tree" / TARGET
    source = (measured if measured.is_file() else REPO / TARGET).read_text(encoding="utf-8")

    if not args.report_only:
        SESSION.mkdir(parents=True, exist_ok=True)
        work = stage()
        # Both commands, so a mutation is only "caught" if something actually
        # noticed. evaluate.py is 70 ms and pins the classifier against the
        # labelled set; selftest.py is slower and pins everything else.
        command = "python3 scripts/evaluate.py --gate"
        if args.full:
            command = "python3 scripts/mutation_runner.py"
            (work / "scripts" / "mutation_runner.py").write_text(
                "import subprocess, sys\n"
                "for c in (['scripts/evaluate.py', '--gate'], ['scripts/selftest.py']):\n"
                "    if subprocess.run([sys.executable] + c, capture_output=True).returncode:\n"
                "        sys.exit(1)\n", encoding="utf-8")
        (work / "cr.toml").write_text(
            f'[cosmic-ray]\nmodule-path = "{TARGET}"\n'
            f'timeout = {args.timeout}\ntest-command = "{command}"\n\n'
            '[cosmic-ray.distributor]\nname = "local"\n', encoding="utf-8")

        baseline = subprocess.run(command.split(), cwd=work, capture_output=True)
        if baseline.returncode:
            print("the unmutated tree already fails its own tests; "
                  "a mutation score would be meaningless", file=sys.stderr)
            print(baseline.stdout.decode()[-800:], file=sys.stderr)
            return 2

        if db.exists():
            db.unlink()
        subprocess.run(["cosmic-ray", "init", "cr.toml", str(db)], cwd=work, check=True)
        print("running mutants, this takes a few minutes", flush=True)
        subprocess.run(["cosmic-ray", "exec", "cr.toml", str(db)], cwd=work, check=True)

    if not db.exists():
        print("no previous session to report", file=sys.stderr)
        return 2

    finished, planned = completeness(db)
    if finished < planned:
        print(f"\nINCOMPLETE: {finished} of {planned} mutants ran. A partial "
              f"session is not a smaller measurement of the same thing, it is a "
              f"measurement of an arbitrary subset.", file=sys.stderr)
        print("Re-run without a timeout, or pass --report-only knowing this.",
              file=sys.stderr)

    only = None if args.full else set(DETECTION)
    total, survived = score(db, source, only)
    label = "whole hook" if args.full else "detection core"
    overall = show(total, survived, label)

    dead = [n for n in total if survived[n] == total[n]]
    if dead:
        print(f"\nnot tested at all ({len(dead)}): {', '.join(sorted(dead))}")
        print("A function every mutation survives is untested, whatever its")
        print("line coverage says. That is the honest reading.")
    print(json.dumps({"scope": label, "score": round(overall, 1),
                      "mutants": sum(total.values()),
                      "survived": sum(survived.values()),
                      "complete": finished >= planned,
                      "ran": finished, "planned": planned}))
    if finished < planned:
        return 2
    if not args.full and overall < MIN_SCORE:
        print(f"\nBELOW FLOOR: {overall:.0f}% < {MIN_SCORE:.0f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
