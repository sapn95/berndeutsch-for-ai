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
import hashlib
import json
import os
import shutil
import signal
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
             "sentence_initial", "_is_word_char",
             # Added when round 20 introduced them. They GENERATE decisive
             # markers rather than list them, which makes them the most
             # consequential code in the file, and they sat outside the
             # measured scope for a round.
             "velarised", "prefixed", "suffixed", "plural_ig",
             "looks_like_address", "strip_addresses")
# Beside the repository, not inside it. Inside, a local-directory plugin
# install copies the working tree without consulting .gitignore, and this
# directory was 68% of the shipped payload: a session database plus a complete
# second copy of the repo, __pycache__ and all. Beside it, and named after the
# repo so two clones do not share one session, it is stable across runs (which
# the system temp directory is not, on macOS) and invisible to any install.
SESSION = REPO.parent / f".{REPO.name}-mutation"
# The floor, recorded from a real run so a change cannot quietly lower it.
# 19% when this was first measured; the gap was almost entirely scan_window,
# where 75 of 75 mutations survived, and is_dialect, where 35 of 42 did.
# Raise it when the measurement rises. Never lower it to make a run pass.
#
# 87.6% on the last complete run, 490 mutants over the detection core. The 57%
# reported before that was measured with an oracle that ran only evaluate.py:
# the comment said "both commands" and the code used both only under --full, so
# fifteen new address tests moved the score by 0.2 points because the oracle
# never saw them. The floor is set below the measurement, not at it, because
# which mutants come back INCOMPETENT varies a little between runs.
MIN_SCORE = 85.0


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
    """line number -> enclosing TOP-LEVEL function name.

    Top-level, not innermost. A nested closure is part of the function that
    contains it, and attributing its lines separately quietly removed them from
    the score: refactoring the case rule in is_dialect into a `counts()` helper
    moved 18 mutants to a name that is not in DETECTION, so the `only` filter
    dropped them and the reported total shrank without anything saying so. The
    score went UP, because the mutants that disappeared were the surviving ones.

    Walked from the module body rather than with ast.walk, so "top level" means
    what it says. A function defined inside a class counts as top level too; a
    function defined inside a function does not.
    """
    spans = []

    def collect(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spans.append((node.lineno, node.end_lineno, node.name))
            elif isinstance(node, ast.ClassDef):
                collect(node.body)

    collect(ast.parse(source).body)
    spans.sort()

    def owner(line):
        for lo, hi, name in spans:
            if lo <= line <= hi:
                return name
        return "<module level>"
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


def reap():
    """Kill oracle processes THIS run left behind, and nothing else.

    A mutant can put the oracle into an infinite loop. cosmic-ray has its own
    per-mutant timeout and it did not reap them: after a run was interrupted,
    eleven evaluate.py workers stayed at 90% CPU for ninety minutes and took
    the machine with them. Killing the parent is not enough, so the strays are
    named and killed explicitly.

    Matched on the WORKING DIRECTORY, which is what actually identifies this
    run's workers.

    Two wrong versions preceded this one. The first was
    `pkill -f "scripts/evaluate.py --gate"`, which is every such process on the
    machine: a maintainer running the gate in another terminal, or a second
    mutation run in another clone. The second matched the staged path in the
    command line, which sounds narrower and is in fact empty -- cosmic-ray runs
    the test command with cwd set to the staged tree, so the workers'
    argv is relative and the staged path appears in none of it. The eleven
    strays that started all this showed up in ps as bare
    `scripts/evaluate.py --gate`.

    A cwd cannot be read portably, so it is read the two ways that exist and
    the fallback is to kill nothing rather than to kill broadly.
    """
    work = (SESSION / "tree").resolve()
    mine = os.getpid()
    try:
        listing = subprocess.run(["ps", "-eo", "pid=,command="],
                                 capture_output=True, text=True).stdout
    except OSError:
        # No ps on this machine. Killing nothing is the documented fallback,
        # and this runs from a finally: a traceback here would replace the
        # completed run's report with a stack trace about a missing binary.
        return
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        # Only ever consider this repository's own oracle commands. Reading the
        # cwd of every process on the machine is both slow and none of our
        # business.
        if not any(name in command for name in
                   ("evaluate.py", "selftest.py", "mutation_runner.py")):
            continue
        try:
            if int(pid) == mine or not inside(cwd_of(pid), work):
                continue
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, ValueError):
            continue


def cwd_of(pid):
    """The working directory of a process, or None if it cannot be read."""
    link = Path("/proc") / str(pid) / "cwd"
    try:                                          # Linux
        return Path(os.readlink(link))
    except OSError:
        pass
    # macOS and the BSDs have no /proc. lsof is the documented way, and its
    # -F output is one field per line: n<path> for the name of the cwd entry.
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)],
                             capture_output=True, text=True)
    except OSError:
        return None
    for entry in out.stdout.splitlines():
        if entry.startswith("n"):
            return Path(entry[1:])
    return None


def inside(child, parent):
    """Whether `child` is `parent` or below it, on a path-segment boundary.

    A string prefix test would treat a sibling directory named tree-old as
    being inside tree.
    """
    if child is None:
        return False
    try:
        child.resolve().relative_to(parent)
    except (OSError, ValueError):
        return False
    return True


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

    if not args.report_only:
        SESSION.mkdir(parents=True, exist_ok=True)
        work = stage()
        # BOTH suites, always. The comment here used to say both and the code
        # used both only under --full, so every check in selftest.py was
        # invisible to the reported score: fifteen direct tests of
        # looks_like_address moved it from 57.0% to 57.2%, because the oracle
        # never ran them. A measurement that quietly measures less than it says
        # is the failure this whole script exists to catch, and it was in the
        # script itself.
        #
        # The cost is real and worth it: evaluate.py is 0.2 s and selftest.py is
        # 1.9 s, so a full sweep goes from minutes to tens of minutes. A score
        # you can trust once an hour beats one you cannot trust in two minutes.
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
        try:
            subprocess.run(["cosmic-ray", "exec", "cr.toml", str(db)], cwd=work,
                           check=True)
        finally:
            reap()

    if not db.exists():
        print("no previous session to report", file=sys.stderr)
        return 2

    # After staging, so this is the source cosmic-ray actually mutated. Read the
    # working tree instead and every mutant is attributed to the wrong function
    # as soon as the file has been edited by as little as one inserted line.
    measured = SESSION / "tree" / TARGET
    if not measured.is_file():
        print(f"\nNo measured copy at {measured}. A score computed against the "
              f"working tree is a score of a file that was never mutated: the "
              f"same session gave 82%, 72% and 52% that way, decided only by "
              f"which file happened to be on disk. Re-run.", file=sys.stderr)
        return 2
    source = measured.read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    stamp = SESSION / "source.sha256"
    if not args.report_only:
        stamp.write_text(digest)
    elif stamp.is_file() and stamp.read_text().strip() != digest:
        print("\nThe staged copy is not the one this session was recorded "
              "against. Re-run.", file=sys.stderr)
        return 2
    live = (REPO / TARGET).read_text(encoding="utf-8")
    if args.report_only and live != source:
        print(f"\nMEASURED AGAINST A SOURCE THAT HAS SINCE CHANGED. "
              f"{TARGET} has been edited since this session was recorded, so "
              f"the score below describes the previous version. Re-run.",
              file=sys.stderr)
        return 2

    # Every name in DETECTION must exist in the source that was measured. The
    # filter drops mutants whose function is not listed, so a rename silently
    # removes that function from the score: renaming word_matches raised the
    # reported figure from 83.1% to 84.4% by deleting the 25 mutants it was
    # doing worst on. A scope that quietly shrinks is not a scope.
    defined = set()

    def names(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.ClassDef):
                names(node.body)

    # Against the LIVE file, which is what a maintainer would go and edit. The
    # first version read the staged copy and named the live file, so it
    # reported two functions as gone that were present, and advised deleting
    # them from the scope: the exact silent shrink this guard exists to stop.
    names(ast.parse(live).body)
    missing = sorted(set(DETECTION) - defined)
    if missing and not args.full:
        print(f"\nDETECTION names no longer in {TARGET}: {', '.join(missing)}",
              file=sys.stderr)
        print("Renamed or deleted? Either way the score would silently exclude "
              "them. Update DETECTION.", file=sys.stderr)
        return 2

    # The scope belongs to the run, not to the flag. --report-only --full
    # re-labelled a detection-core session "whole hook" and printed a number
    # for functions that were never mutated with that oracle.
    # Recorded beside the session, not inside the staged tree: the tree is
    # rebuilt on every run and cr.toml did not survive, so the guard that read
    # it never fired.
    scope_file = SESSION / "scope"
    if not args.report_only:
        scope_file.write_text("full" if args.full else "detection")
    elif scope_file.is_file():
        was = scope_file.read_text().strip()
        want = "full" if args.full else "detection"
        if was != want:
            print(f"\nThis session was recorded with scope '{was}', and you asked "
                  f"for '{want}'. The scope belongs to the run, not to the flag: "
                  f"re-run without --report-only.", file=sys.stderr)
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
