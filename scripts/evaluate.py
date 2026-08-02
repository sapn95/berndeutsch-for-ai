#!/usr/bin/env python3
"""evaluate — measure the detector as the classifier it is.

The thing in hooks/berndeutsch_gate.py that decides whether a prompt is
Bernese is a binary classifier. For sixteen review rounds it was assessed the
way one assesses a piece of logic: someone thinks of a sentence, runs it,
and argues about the answer. That yields an unbounded supply of individually
plausible objections and never a number, so it never converges.

This measures it instead, against corpus/labelled.tsv:

    scripts/evaluate.py              # confusion matrix, precision, recall
    scripts/evaluate.py --errors     # and every misclassified line
    scripts/evaluate.py --gate       # exit 1 if below the recorded floor

The two errors are not symmetric, so they are never averaged into one number.
A false negative leaves a Bernese turn ungoverned and is SILENT, which is the
failure this repository exists to prevent. A false positive puts a conditional
instruction into a foreign-language turn, which is visible and bounded: the
injected text says "if this message is Bärndütsch", and a supporting-only match
costs the short checklist rather than the full rulebook. Recall is therefore
the headline and precision is the constraint.

Intervals are Wilson, not the textbook normal approximation, which is badly
wrong at exactly the small n and extreme p this set operates at.

Beyond the counts it runs metamorphic checks in the CheckList sense
(Ribeiro et al., ACL 2020): INVARIANCE, where a perturbation must not change
the verdict, and DIRECTIONAL, where it may only move one way. Those catch a
class of defect no example list reaches, because they quantify over
transformations rather than over sentences.
"""

import argparse
import importlib.machinery
import importlib.util
import math
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "berndeutsch_gate.py"
LABELLED = REPO / "corpus" / "labelled.tsv"

# The floor, not the target. Recorded so a change that quietly makes the
# classifier worse fails instead of being noticed three rounds later. Raise it
# when the measurement rises; never lower it to make a run pass.
MIN_RECALL = 0.95
MIN_PRECISION = 0.97

# And the same floors as counts, because a rate over 244 rows cannot see what
# this repository actually cares about. At 130 true positives, precision stays
# above 0.97 until the FIFTH false positive, so no single-row regression and no
# two-, three- or four-row regression could fail the gate: moving bare `geit`
# into DECISIVE made three Dutch sentences pull the whole rulebook and the gate
# passed. These are the current measurement, not a margin. Raise them
# deliberately when the corpus grows, the same way the rates are raised.
MAX_FALSE_POSITIVES = 0
MAX_FALSE_NEGATIVES = 4


def load_hook():
    loader = importlib.machinery.SourceFileLoader("bd_gate", str(HOOK))
    spec = importlib.util.spec_from_loader("bd_gate", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def cases():
    for line in LABELLED.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # maxsplit=2: a tab inside the TEXT is part of the text, not a fourth
        # field. And nothing is dropped silently: a row that does not parse, or
        # a label that is not one of the two, is a mistake in the data and has
        # to be loud. Discarding it quietly moves the metric in whichever
        # direction the mistake happened to point.
        parts = line.split("\t", 2)
        if len(parts) != 3:
            raise SystemExit(f"corpus/labelled.tsv: not three fields: {line!r}")
        label, kind, text = parts[0].strip(), parts[1].strip(), parts[2]
        if label not in ("bd", "xx"):
            raise SystemExit(f"corpus/labelled.tsv: label is not bd or xx: {label!r}")
        # One row is one line, so a real newline needs an escape, and it has to
        # be a DIFFERENT escape from the lower-case one: a row already in this
        # set is a pasted JSON log whose text contains the two characters
        # backslash-n literally, and the hook is supposed to treat those as
        # whitespace rather than as a token boundary. \N (capital) is the real
        # line break; \n stays exactly the two characters it is. Until this
        # existed no row in the set could contain a line break at all, so the
        # branch in sentence_initial that decides what a line break means was
        # measured by nothing.
        yield label, kind, text.replace("\\N", "\n")


def wilson(hits, total, z=1.96):
    """Wilson score interval for a binomial proportion.

    Brown, Cai & DasGupta (2001): the normal approximation is unusable near 0
    and 1 and at small n, which is where every number here lives.
    """
    if not total:
        return 0.0, 0.0, 0.0
    p = hits / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return p, max(0.0, centre - margin), min(1.0, centre + margin)


def classify(gate, text):
    return gate.is_dialect(text)[0]


# INVARIANCE: the verdict must not move. Each is a transformation of the text
# plus a reason, and the reason matters: a relation nobody can justify produces
# arguable findings rather than defects.
def invariances():
    return [
        ("trailing whitespace", lambda t: t + "   \n",
         "whitespace is not evidence about a language"),
        ("leading whitespace", lambda t: "\n  " + t, "same"),
        ("wrapped in quotes", lambda t: f'"{t}"', "quoting is not evidence"),
        ("NFD instead of NFC", lambda t: unicodedata.normalize("NFD", t),
         "the same text, decomposed; scan_window normalises for this reason"),
        ("trailing emoji", lambda t: t + " 🙂", "not evidence about a language"),
        ("doubled", lambda t: t + " " + t,
         "saying the same thing twice cannot change which language it is in"),
    ]


# DIRECTIONAL: the verdict may only move one way, and for a bounded-window
# classifier that direction has to be stated rather than assumed. Appending a
# huge paste MAY legitimately push the sentence out of the window, so the
# relation is one-sided on purpose: adding dialect must never turn a positive
# into a negative, and adding foreign text must never turn a negative positive.
def directionals():
    return [
        # The appended fragment must NOT fire on its own, or the relation is
        # satisfied by the fragment rather than by the original text and holds
        # for every row unconditionally: with " U das isch guet gsy." appended,
        # deleting the entire tail half of the window still reported 134/134.
        # " u de." is two weak markers, which is one short of the bar.
        ("bd", "append more dialect", lambda t: t + " u de.",
         "adding Bernese cannot make a Bernese prompt less Bernese"),
        # With a full stop. Prepending "u de " without one moved the row's own
        # first word out of sentence-initial position, where a capitalised weak
        # marker is allowed, and three rows went silent: the relation was
        # measuring the case rule rather than the marker count.
        ("bd", "prepend more dialect", lambda t: "u de. " + t, "same"),
        ("xx", "append neutral English", lambda t: t + " Thanks in advance.",
         "adding English cannot make a non-Bernese prompt Bernese"),
        # The path needs marker-shaped components or it probes nothing: with an
        # ordinary com/example path there was no letter run for either rule to
        # have an opinion about, and removing / and + from GLUE reported a
        # clean 104/104. This one is handled by strip_addresses, which sees the
        # slashes and blanks the whole chunk before GLUE is ever consulted.
        ("xx", "append a file path",
         lambda t: t + " See src/main/java/ch/isch/gsi/Service.java",
         "a path is not dialect"),
        # And GLUE itself, which needs something that is NOT an address: no
        # dot, no slash, no at-sign, nothing for the address rule to catch. A
        # pod name is the real case that fired -- itz9q read as itz.
        ("xx", "append a pod name", lambda t: t + " Pod itz9q_gsi3 restarted.",
         "letters glued to digits are an identifier, not a word"),
        # These two are the only relations that reach the window: the longest
        # corpus row is 4757 characters against a 6000-character window, so
        # every other relation here is decided long before the cut. A marker in
        # the head survives any paste after it, and one in the tail survives
        # any paste before it, which is the entire point of keeping both ends.
        ("bd", "followed by a 200 KB log",
         lambda t: t + "\n" + LOG_LINE * 5000,
         "a marker at the start of a huge paste is still in the head window",
         fits_in_half),
        ("bd", "preceded by a 200 KB log",
         lambda t: LOG_LINE * 5000 + "\n" + t,
         "a marker at the end of a huge paste is still in the tail window",
         fits_in_half),
    ]


LOG_LINE = "2026-08-01 INFO request handled in 4ms\n"


def fits_in_half(text):
    """Whether one half of the window can hold this row whole.

    The two window relations are claimed only for rows this is true of. For a
    longer row the classifier's own contract permits the flip -- a big enough
    paste may push the evidence out -- and asserting otherwise would be
    asserting something the hook never promised.
    """
    global _HALF
    if _HALF is None:
        gate = load_hook()
        _HALF = min(gate.SCAN_HEAD, gate.SCAN_TAIL)
    return len(text) <= _HALF


# Read once. The predicate runs for every row of every relation, and re-execing
# the hook module 134 times to read two constants took longer than the whole
# rest of the evaluation.
_HALF = None


# Boundary probes: synthetic on purpose, and deliberately NOT part of the
# labelled set. The corpus is meant to look like what a person types, so that
# precision and recall mean something; sentences built to sit exactly on a
# threshold would flatter or damage those numbers without telling you anything
# about real use. But the thresholds still have to be pinned, because mutation
# testing showed 35 of 42 mutations to is_dialect surviving: the decision logic
# was, in effect, untested, since every realistic sentence is far from the line.
#
# Each pair differs by exactly one marker across the threshold it probes.
BOUNDARY = [
    # MIN_SUPPORTING: two distinct markers that are not BOTH weak fire, one does
    # not. Both names said "non-weak" of het, which stopped being true when het
    # moved into WEAK; nid is the non-weak half of the pair and always was.
    (True, "two supporting markers, not both weak", "Das het nid so."),
    # The negative half has to be NON-weak too, or it probes MIN_WEAK_ONLY
    # instead and MIN_SUPPORTING has no negative case at all: `het` is weak, so
    # "Das het so." needed three markers whatever MIN_SUPPORTING said, and
    # 2 -> 1 left both halves of this pair green. `luege` is supporting and not
    # weak, and it is the only marker in the sentence.
    (False, "one supporting marker, not weak", "mir luege das."),
    # MIN_WEAK_ONLY: three lower-case weak markers fire, two do not.
    (True, "three weak markers", "gsi gha nit"),
    (False, "two weak markers", "gsi gha"),
    # The lower-case rule: the same three, capitalised mid-sentence, must not.
    (False, "three weak markers, capitalised", "the GSI the GHA the NIT"),
    # Sentence-initial capital is orthography, not evidence.
    # No decisive marker in either half, or is_dialect returns on the decisive
    # branch and the probe measures nothing. "Chum. Das isch nid." contained
    # isch and stayed green however the case rule behaved.
    # The pair has to TURN on the capitalised marker: with het and nid in the
    # sentence it fires either way and the probe measures nothing. Here chum is
    # the second marker, so rejecting it drops the count to one.
    (True, "weak marker capitalised at a sentence start", "Chum, mir luege."),
    (False, "the same weak marker capitalised mid-sentence", "mir luege dr Chum."),
    # "Guete Tag" was the greeting here, and guete is DECISIVE, so is_dialect
    # returned on the decisive branch and this row stayed green with the whole
    # case rescue deleted. That is the failure the note nine lines above warns
    # about, in the row added to demonstrate it. Hallo is a marker in no tier.
    (True, "weak marker capitalised after a line break", "Hallo\nChum, mir luege."),
    # And the other half of the same rule, which is what a line break means in a
    # table, a list, a log or a pasted chat transcript rather than in prose:
    # line-initial capitals with no lower-case marker anywhere near them.
    (False, "Title-case weak markers alone on their own lines", "Cha\nModi\nWitt"),
    # One decisive marker is enough, and only one is needed.
    (True, "a single decisive marker", "Das isch so."),
    # A repeated marker is one marker, not two.
    (False, "the same supporting marker twice", "het het het"),
    # GLUE: the same letters, once as prose and once inside an identifier.
    (True, "markers as prose", "das isch guet"),
    (False, "the same letters inside identifiers", "das_isch_guet x1isch2 a/isch+b"),
    # The four below were written from a list of surviving mutants rather than
    # from imagination, which is the point of having the list: each one is the
    # smallest input that notices a specific change nothing else noticed.
    # None of them contains a decisive marker, or the supporting logic would
    # never be reached and the probe would prove nothing.
    #
    # Kills: sentence_initial's walk-back offset. If it inspects the token's own
    # first character instead of the one before it, "Chum" stops counting.
    (True, "a weak marker capitalised at position 0", "Chum, mir luege das aa."),
    # Kills: the `continue` that skips a rejected weak marker. Turned into a
    # `break` it abandons the whole token stream, so the two good markers after
    # the rejected one vanish.
    (True, "markers after a rejected weak marker still count",
     "es Modi, mir luege scho lang."),
    # Kills: the capitalisation test. Inverted, three Title-case weak markers
    # would clear the weak-only bar.
    (False, "three Title-case weak markers mid-sentence",
     "We use the Modi, the Gits and the Cha here."),
    # Kills: >= turned into ==. Four markers is above the threshold, not at it.
    (True, "four markers, above the threshold rather than on it",
     "Hei si scho nid dänk?"),
]

# The second half of the return value, which decides FULL rulebook versus short
# checklist and whether the session's one full injection is spent. evaluate.py
# looked only at the first half, so every mutation to the flag survived.
CERTAINTY = [
    (True, "a decisive marker is certain", "Das isch so."),
    (False, "supporting markers alone are not certain", "Das het nid so."),
]


def window_properties(gate, show_errors):
    """Invariants of scan_window, quantified over inputs rather than examples.

    An example test asserts one outcome for one string. These assert what must
    hold for ANY string, which is the only way to pin a function whose whole
    job is to behave the same at every length. Mutation testing found 37 of 75
    changes to scan_window going unnoticed while the example tests were green.
    """
    filler = "lorem ipsum dolor sit amet consectetur adipiscing elit "
    marker = "Verzeichnisch"          # ends in the decisive marker "isch"
    checks = []

    short = "Chum mer luege"
    checks.append(("a prompt shorter than the window is returned unchanged",
                   gate.scan_window(short) == short))

    big = filler * 400
    win = gate.scan_window(big)
    checks.append(("the window is bounded",
                   len(win) <= gate.SCAN_HEAD + gate.SCAN_TAIL + 1))
    checks.append(("the window is not empty for a large input", len(win) > 100))

    # Every token the window yields must be a token the input actually had.
    # This is the property the "invents no marker" example was reaching for,
    # and unlike the example it holds the function to it at every offset.
    source_tokens = set(gate.word_tokens(big.lower()))
    invented = [t for t in gate.word_tokens(win.lower()) if t not in source_tokens]
    checks.append(("the window invents no token that was not in the input",
                   not invented))

    # Slide the marker across every offset near both cuts. If the trim is wrong
    # by one anywhere, a cut lands inside "Verzeichnisch" and leaves "isch".
    bad_offsets = []
    for delta in range(-len(marker) - 2, len(marker) + 3):
        head = gate.SCAN_HEAD + delta
        if head < 1:
            continue
        probe = (filler * 200)[:head] + marker + " " + filler * 200
        if "isch" in gate.word_tokens(gate.scan_window(probe).lower()):
            bad_offsets.append(("head", delta))
        tail = gate.SCAN_TAIL + delta
        if tail < 1:
            continue
        probe = filler * 200 + " " + marker + (filler * 200)[:tail]
        if "isch" in gate.word_tokens(gate.scan_window(probe).lower()):
            bad_offsets.append(("tail", delta))
    checks.append(("no cut offset leaves a marker-shaped fragment", not bad_offsets))

    print()
    print("window properties (quantified over inputs, not examples)")
    wrong = 0
    for name, ok in checks:
        if not ok:
            wrong += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if wrong and show_errors and bad_offsets:
        print(f"          offsets that leak: {bad_offsets[:8]}")
    return wrong


def boundary(gate, show_errors):
    """Probe each threshold from both sides. Reported separately from the metrics."""
    print()
    print("boundary probes (synthetic; not counted in precision or recall)")
    wrong = 0
    for want, name, text in BOUNDARY:
        got = classify(gate, text)
        if got != want:
            wrong += 1
        print(f"  {'ok  ' if got == want else 'FAIL'}  {name:44} "
              f"{'fires' if want else 'silent'}")
        if got != want and show_errors:
            print(f"          {text}")
    for want, name, text in CERTAINTY:
        got = gate.is_dialect(text)[1]
        if got != want:
            wrong += 1
        print(f"  {'ok  ' if got == want else 'FAIL'}  {name:44} "
              f"{'full rulebook' if want else 'checklist'}")
    return wrong


def report(gate, show_errors):
    rows = list(cases())
    if not rows:
        print("corpus/labelled.tsv has no usable rows", file=sys.stderr)
        return None

    tp = fp = tn = fn = 0
    by_kind = defaultdict(lambda: [0, 0])   # kind -> [wrong, total]
    errors = []
    for label, kind, text in rows:
        fired = classify(gate, text)
        want = label == "bd"
        by_kind[kind][1] += 1
        if fired and want:
            tp += 1
        elif fired and not want:
            fp += 1
            by_kind[kind][0] += 1
            errors.append(("FALSE POSITIVE", kind, text))
        elif not fired and want:
            fn += 1
            by_kind[kind][0] += 1
            errors.append(("FALSE NEGATIVE", kind, text))
        else:
            tn += 1

    print(f"labelled cases: {len(rows)}  ({tp + fn} Bernese, {fp + tn} not)")
    print()
    print("                 fired    silent")
    print(f"  Bernese      {tp:7}   {fn:7}")
    print(f"  not Bernese  {fp:7}   {tn:7}")
    print()
    recall, r_lo, r_hi = wilson(tp, tp + fn)
    prec, p_lo, p_hi = wilson(tp, tp + fp) if tp + fp else (0.0, 0.0, 0.0)
    print(f"  recall     {recall:6.1%}   [{r_lo:.1%}, {r_hi:.1%}]   "
          f"(a miss is silent, so this is the headline)")
    print(f"  precision  {prec:6.1%}   [{p_lo:.1%}, {p_hi:.1%}]   "
          f"(a false fire is visible and bounded)")
    print()
    print("  95% Wilson intervals. With this many cases they are wide; that is")
    print("  the honest width, not a reason to quote the point estimate alone.")

    bad = {k: v for k, v in by_kind.items() if v[0]}
    if bad:
        print()
        print("misclassified by kind:")
        for kind, (wrong, total) in sorted(bad.items(), key=lambda kv: -kv[1][0]):
            print(f"  {kind:12} {wrong}/{total}")

    if show_errors and errors:
        print()
        for what, kind, text in errors:
            print(f"  {what:15} [{kind}] {text}")

    return {"recall": recall, "precision": prec,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn}


def metamorphic(gate, show_errors):
    """CheckList-style INV and DIR relations over the whole labelled set."""
    rows = list(cases())
    print()
    print("metamorphic: invariance (the verdict must not move)")
    broken = 0
    for name, transform, _why in invariances():
        changed = []
        for label, kind, text in rows:
            before = classify(gate, text)
            try:
                after = classify(gate, transform(text))
            except Exception as exc:              # a crash is a worse failure
                changed.append((text, f"raised {type(exc).__name__}: {exc}"))
                continue
            if before != after:
                changed.append((text, f"{before} then {after}"))
        broken += len(changed)
        mark = "ok  " if not changed else "FAIL"
        print(f"  {mark}  {name:24} {len(rows) - len(changed)}/{len(rows)}")
        if show_errors:
            for text, detail in changed[:4]:
                print(f"          {detail}  |  {text[:60]}")

    print("metamorphic: directional (the verdict may only move one way)")
    for want_label, name, transform, _why, *rest in directionals():
        # An optional predicate, for the two relations that are only claimed
        # for rows the window can hold whole. Without it they would be claimed
        # for a 4757-character log line too, where a 200 KB paste legitimately
        # pushes the marker out of the window and the docstring above says so.
        applies = rest[0] if rest else (lambda _t: True)
        subset = [r for r in rows if r[0] == want_label and applies(r[2])]
        # A filter that excludes everything would report a perfect 0/0. The
        # relation has to be claimed for something.
        if len(subset) < 20:
            print(f"  FAIL  {name:24} in scope for only {len(subset)} row(s)")
            broken += 1
            continue
        wrong = []
        for label, kind, text in subset:
            before = classify(gate, text)
            after = classify(gate, transform(text))
            # For bd: adding dialect must not turn a fire into silence.
            # For xx: adding foreign text must not turn silence into a fire.
            if want_label == "bd" and before and not after:
                wrong.append(text)
            if want_label == "xx" and not before and after:
                wrong.append(text)
        broken += len(wrong)
        mark = "ok  " if not wrong else "FAIL"
        print(f"  {mark}  {name:24} {len(subset) - len(wrong)}/{len(subset)}")
        if show_errors:
            for text in wrong[:4]:
                print(f"          {text[:70]}")
    return broken


def main():
    ap = argparse.ArgumentParser(prog="evaluate", description=__doc__.splitlines()[0])
    ap.add_argument("--errors", action="store_true", help="list every misclassified case")
    ap.add_argument("--gate", action="store_true",
                    help="exit 1 below the recorded recall and precision floor")
    args = ap.parse_args()

    gate = load_hook()
    scores = report(gate, args.errors)
    if scores is None:
        return 2
    broken = metamorphic(gate, args.errors)
    broken += boundary(gate, args.errors)
    broken += window_properties(gate, args.errors)

    if not args.gate:
        return 0
    print()
    failed = []
    if scores["recall"] < MIN_RECALL:
        failed.append(f"recall {scores['recall']:.1%} < {MIN_RECALL:.0%}")
    if scores["precision"] < MIN_PRECISION:
        failed.append(f"precision {scores['precision']:.1%} < {MIN_PRECISION:.0%}")
    # The counts as well as the rates. See MAX_FALSE_POSITIVES: at this corpus
    # size the rates alone hand out four free false fires and two free misses,
    # which is most of the regressions this gate exists to stop.
    if scores["fp"] > MAX_FALSE_POSITIVES:
        failed.append(f"{scores['fp']} false positive(s) > {MAX_FALSE_POSITIVES}")
    if scores["fn"] > MAX_FALSE_NEGATIVES:
        failed.append(f"{scores['fn']} miss(es) > {MAX_FALSE_NEGATIVES}")
    if broken:
        failed.append(f"{broken} metamorphic violation(s)")
    if failed:
        print("GATE FAILED: " + "; ".join(failed))
        return 1
    print("gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
