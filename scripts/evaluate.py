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
        yield label, kind, text


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
        ("bd", "append more dialect", lambda t: t + " U das isch guet gsy.",
         "adding Bernese cannot make a Bernese prompt less Bernese"),
        ("bd", "prepend more dialect", lambda t: "Lueg mau. " + t, "same"),
        ("xx", "append neutral English", lambda t: t + " Thanks in advance.",
         "adding English cannot make a non-Bernese prompt Bernese"),
        ("xx", "append a file path",
         lambda t: t + " See src/main/java/com/example/Service.java",
         "a path is not dialect"),
    ]


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
    # MIN_SUPPORTING: two distinct non-weak markers fire, one does not.
    (True, "two non-weak supporting markers", "Das het nid so."),
    (False, "one non-weak supporting marker", "Das het so."),
    # MIN_WEAK_ONLY: three lower-case weak markers fire, two do not.
    (True, "three weak markers", "gsi gha nit"),
    (False, "two weak markers", "gsi gha"),
    # The lower-case rule: the same three, capitalised mid-sentence, must not.
    (False, "three weak markers, capitalised", "the GSI the GHA the NIT"),
    # Sentence-initial capital is orthography, not evidence.
    (True, "weak marker capitalised at a sentence start", "Chum. Das isch nid."),
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
    for want_label, name, transform, _why in directionals():
        subset = [r for r in rows if r[0] == want_label]
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
    if broken:
        failed.append(f"{broken} metamorphic violation(s)")
    if failed:
        print("GATE FAILED: " + "; ".join(failed))
        return 1
    print("gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
