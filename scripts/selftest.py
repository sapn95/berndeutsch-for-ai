#!/usr/bin/env python3
"""selftest — the regression suite for the hook, bdw and bd-corpus.

Every check here exists because the behaviour it asserts was broken at least
once, usually by a fix for something else. Three of them were shipped and only
caught a review round later:

  * a filter meant to drop bare reflexive markers dropped the real headwords
    «öppis» and «öpper», so bdw reported two core Bernese words as nonexistent
  * score() started lowercasing its input while three markers kept their
    capitals, so those markers silently stopped counting
  * a marker list gained `gange`, which is the ordinary German "im Gange", so
    a German sentence pulled in the full rulebook

    scripts/selftest.py            # everything that needs no network
    scripts/selftest.py --online   # also: bdw against berndeutsch.ch, and the
                                   # measurements NOTICE quotes re-verified
                                   # against the source PDF (needs pdftotext)

Exit code 0 if every check passes, 1 otherwise. Network checks are opt-in
because a volunteer-run dictionary should not be hit by a test loop. A lookup
that cannot reach the site is reported as SKIPPED and does not fail the run: a
network that is down must never look like a repository that is broken.
"""

import argparse
import ast
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "berndeutsch_gate.py"

FAILURES = []
# Checks that could not be run rather than checks that failed. A network that
# is down must never look like a repository that is broken.
SKIPPED = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


def fastest(fn, rounds=5):
    """The cheapest of `rounds` runs, in milliseconds.

    A single timing measures the machine's mood as much as the code. The
    minimum is the only summary statistic here that is not inflated by another
    process getting the core, which matters because these budgets were once
    loose enough to be load-dependent and are no longer.
    """
    best = float("inf")
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - start) * 1000)
    return best


# What the calibration below measured when the per-shape costs were recorded.
CALIBRATION_MS = 10.0


def calibrate():
    """How much slower this machine is than the one the costs were recorded on.

    Deliberately NOT a call into the hook. A reference the hook can slow down
    would absorb exactly the regression it is here to expose: every shape would
    move, the ratio would not, and the budgets would follow the defect upwards.
    NFC and a word split over a fixed blob use the same two library primitives
    the window does, and no repository code at all.
    """
    import unicodedata
    blob = "a" * 400_000

    def work():
        unicodedata.normalize("NFC", blob)
        re.findall(r"[^\W\d_]+", blob)

    # Never below 1.0: on a machine faster than the recording one the budgets
    # stay as recorded rather than tightening into noise.
    return max(1.0, fastest(work) / CALIBRATION_MS)


def load(path):
    """Import a module from a path, extension or not.

    bdw and bd-corpus are installed as extensionless commands, so the ordinary
    file-suffix import machinery does not find them.
    """
    name = "bd_" + path.name.replace("-", "_").replace(".py", "")
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run_hook(prompt, session, config_dir):
    """Feed one prompt through the hook exactly as Claude Code does.

    The two override variables are stripped from the child environment, the
    same way install.py strips them for its own self-test. A maintainer who
    actually uses this hook has BERNDEUTSCH_IDIOLECT pointing at a personal
    overlay, and a suite that inherits it measures that maintainer's machine
    rather than the repository: the injected sizes change, and a broken
    bundled rulebook can be masked by a working private one.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("BERNDEUTSCH_RULES", "BERNDEUTSCH_IDIOLECT")}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    payload = json.dumps({"session_id": session, "prompt": prompt})
    proc = subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, env=env, timeout=30)
    if proc.returncode != 0:
        return None, proc.stderr
    out = proc.stdout.strip()
    if not out:
        return "", ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"], ""


# Prompts that MUST pull in the rules, and why each one is here.
FIRES = [
    ("Chasch mer säge öb das guet isch?", "-sch verb form"),
    ("Hoi zäme, chöit dihr mir hälfe?", "2nd person plural, the polite form"),
    ("I ha das gseh u ha nüt gseit.", "everyday participles"),
    ("Mir sy geschter ggange.", "doubled participle, the prescribed spelling"),
    ("Er het das nid gha.", "three weak markers, all collision-prone"),
    ("Chum mer wei das luege.", "supporting only, one strong"),
    ("Das isch aues viu z schnäu gange.", "l-vocalisation"),
    ("Weisch no wo mir das gmacht hei?", "mixed"),
    ("I ha kei Zyt gha für das.", "3-letter markers in real prose"),
    ("Itz simer parat.", "short sentence, one decisive marker"),
    # Auxiliary plus negation is the commonest two-marker shape in the
    # language. Requiring three weak markers silenced it for a whole round.
    ("Das het nid klappt.", "het + nid, the commonest pair"),
    ("Er het nid welle.", "het + nid again"),
    ("Hallo\nChum mer wei das luege.", "weak marker capitalised after a line "
     "break, grounded by luege"),
]

# Prompts that MUST stay silent. Every one of these fired at some point.
SILENT = [
    ("Update the GHA workflow and the DynamoDB GSI projection.", "gha + gsi"),
    ("The KEI report and the Lustre NID mapping both need review.", "kei + nid"),
    ("Stop nit-picking the AUT suite configuration.", "nit + aut"),
    ("Filter rows where country is MYS and residue is Gly.", "mys + gly"),
    ("Die Migration ist noch im Gange, bitte warte damit.", "German 'im Gange'"),
    ("Es gibt zwei Modi, und die Migration ist noch im Gange.", "modi + gange"),
    ("GET /api/het/modi returns 500", "het + modi from a URL path"),
    ("Refactor het_parser and the modi enum in the Go service.", "snake_case het"),
    ("Prime Minister Modi met spokesman Geng Shuang in Beijing.", "two surnames"),
    ("kubectl logs api-gateway-7d4b9c8f5-itz9q shows a 502, what now?",
     "pod-name hash yielding itz"),
    ("Why is worker-58f7b6d4c9-zyt8k stuck in Pending?", "pod-name hash: zyt"),
    ("Pod checkout-6c8f4d9b7-viu2x keeps CrashLoopBackOff.", "pod-name hash: viu"),
    ("Please refactor this function and add a unit test.", "plain English"),
    ("Kannst du mir bitte die Konfiguration erklären?", "plain German"),
    ("git commit -m 'fix' && git push origin main", "bare git twice"),
    # The escapes must conjure enough markers to actually fire when the
    # normalisation is removed, or the case proves nothing. The first version
    # used "\\nID" and "\\nItem 3": the second tokenises to NITEM, not nit, so
    # exactly one weak marker was ever conjured and the case passed with the
    # normalisation deleted. Both of these fail without it, verified.
    ('Parse the log line "error\\nID mismatch\\nIt failed" in the modi table.',
     "escaped \\n conjuring nid + nit, three weak with modi"),
    ('Parse the TSV header "name\\tuet\\tvalue" from the export.',
     "escaped \\t conjuring the decisive marker tuet"),
    ("procs memory swap io system cpu r b swpd free buff si so bi bo",
     "vmstat column headings si/so"),
    # The base64 alphabet uses / and +, so a blob is shredded into short letter
    # runs exactly as a pod-name hash is.
    ("Decode the blob YWJj/itZ+Zm9v and tell me what it is.", "base64 / and +"),
    ("Frei-heit und Sicher-heit sind wichtig.", "hyphenated German -heit"),
    ("Der Heit-Algorithmus ist langsam.", "the surname Heit"),
    ("Modi and Geng both attended the summit.", "surname at sentence start"),
]


def hook_checks(tmp):
    print("hook: marker tiers")
    gate = load(HOOK)
    check("WEAK is a subset of SUPPORTING", gate.WEAK <= gate.SUPPORTING,
          str(sorted(gate.WEAK - gate.SUPPORTING)))
    check("the two tiers do not overlap", not (gate.DECISIVE & gate.SUPPORTING),
          str(sorted(gate.DECISIVE & gate.SUPPORTING)))
    check("every marker is lowercase",
          all(m == m.lower() for m in gate.DECISIVE | gate.SUPPORTING))
    # A marker with an uppercase letter can never match, because is_dialect()
    # lowercases before testing. bd-corpus shipped exactly that bug.
    check("no marker is a single character",
          all(len(m) > 1 for m in gate.DECISIVE | gate.SUPPORTING))
    # Leaving too few non-weak markers would make the whole supporting tier
    # need three markers, silently, which no single check would catch.
    check("the supporting tier keeps some non-weak markers",
          len(gate.SUPPORTING - gate.WEAK) >= 8,
          f"{len(gate.SUPPORTING - gate.WEAK)} non-weak")
    check("CASED contains only real markers",
          gate.CASED <= (gate.DECISIVE | gate.SUPPORTING),
          str(sorted(gate.CASED - (gate.DECISIVE | gate.SUPPORTING))))
    # A frozenset silently swallows a repeated word, so the same marker can be
    # listed twice in the source and nothing notices. Harmless in itself, but it
    # is the visible symptom of a list edited without reading it, which is how
    # `wei` came back after being removed for colliding with a name.
    source = HOOK.read_text(encoding="utf-8")
    for name in ("DECISIVE", "SUPPORTING", "WEAK"):
        listed = source.split(f'{name} = frozenset("""', 1)[1].split('"""')[0].split()
        dupes = sorted({w for w in listed if listed.count(w) > 1})
        check(f"no marker is listed twice in {name}", not dupes, str(dupes))

    # The two derivations, checked as rules rather than as examples. A list of
    # twins can forget one; these cannot, so what has to be checked is that the
    # derivation is wired in at all.
    check("velarised twins are derived, not listed",
          "mitenang" in gate.DECISIVE and "mitenand" in gate.DECISIVE,
          "nd -> ng, from the rulebook's own rule")
    check("a participle behind a separable prefix is matched",
          gate.prefixed("zuegmacht") and gate.prefixed("ufgschribe")
          and not gate.prefixed("ufxyz"),
          "the perfect is the only past tense the language has")

    # A rule that GENERATES markers has to be checked on what it generates,
    # because there is no list left to read. One combination, "agha", was an
    # English word (an Ottoman title) and was decisive, so a single occurrence
    # injected the whole rulebook.
    generated = {p + w for p in gate.PREFIXES for w in gate.PARTICIPLES
                 if gate.prefixed(p + w)}
    listed = set()
    source = HOOK.read_text(encoding="utf-8")
    for name in ("DECISIVE", "SUPPORTING"):
        listed |= set(source.split(f'{name} = frozenset("""', 1)[1]
                      .split('"""')[0].split())
    generated |= (gate.DECISIVE | gate.SUPPORTING) - listed
    dictionaries = [Path(d) for d in ("/usr/share/dict/words", "/usr/share/dict/web2")
                    if Path(d).exists()]
    if dictionaries:
        english = set()
        for path in dictionaries:
            english |= {w.strip().lower()
                        for w in path.read_text(errors="ignore").splitlines()}
        # And the one rule whose output is a predicate rather than a set.
        # suffixed() accepts anything matching -lech, so the only way to check
        # it is to run it over the dictionary rather than to intersect with it.
        collisions = sorted(generated & english)
        collisions += sorted(w for w in english if gate.suffixed(w))
        check(f"no generated marker is an English word ({len(generated)} generated)",
              not collisions, str(collisions))
    else:
        SKIPPED.append("generated markers vs an English dictionary: none installed")
        print("  skip  no generated marker is an English word  (no system dictionary)")

    # The apostrophe marks an elision and the elided spelling is what the lists
    # carry. "Wie geit's", the commonest greeting in the language, tokenised to
    # geit + s and matched nothing.
    check("an elision apostrophe joins the word",
          gate.is_dialect("Wie geit's?")[0] and gate.is_dialect("Hesch's gseh?")[0])

    # A generated marker is only safe if the PARTS are safe. German's
    # inseparable prefixes are where the one confirmed collision came from
    # (über + bracht = überbracht), and a participle stem that is itself German
    # is the other half of the same mistake: Bernese writes g-, German ge-.
    bad_prefix = sorted(set(gate.PREFIXES) & gate.GERMAN_INSEPARABLE)
    check("no separable prefix is one of German's inseparable ones",
          not bad_prefix, str(bad_prefix))
    german_shaped = sorted(p for p in gate.PARTICIPLES if p.startswith("ge"))
    check("no participle stem is written the German way",
          not german_shaped, str(german_shaped))

    # Every function that GENERATES markers must be inside the mutation scope,
    # or the most consequential code in the file is measured by nothing.
    mutation_scope = load(REPO / "scripts" / "mutation.py").DETECTION
    # The list here said ("velarised", "prefixed") and stayed green when
    # plural_ig was added to the hook and left out of DETECTION. A hand-written
    # list of what must be covered is one more thing to forget, so the hook now
    # names its own generators and this reads that tuple.
    generators = sorted(gate.GENERATORS)
    missing = [g for g in generators if g not in mutation_scope]
    check("every marker generator is inside the measured scope", not missing,
          f"{len(generators)} generators, missing {missing}")
    absent = [g for g in generators if not callable(getattr(gate, g, None))]
    check("and every one of them is a function in the hook", not absent,
          str(absent))
    # And the tuple has to be COMPLETE, or it is the same hand-written list one
    # file further along. Derived from the hook's syntax tree: a generator is a
    # module-level function called either in a `SET |= f(SET)` at import or in
    # the token test that decides whether a word is a marker at all.
    tree = ast.parse(HOOK.read_text(encoding="utf-8"))
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    derived = set()
    for node in tree.body:
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.BitOr):
            derived |= {c.func.id for c in ast.walk(node.value)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                        and c.func.id in defined}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "is_marker":
            derived |= {c.func.id for c in ast.walk(node)
                        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                        and c.func.id in defined}
    # Guard the guard: an empty derivation would satisfy any subset test.
    if check("the hook's syntax tree yields its generators", len(derived) >= 4,
             str(sorted(derived))):
        check("the hook's GENERATORS tuple names exactly them",
              derived == set(generators),
              f"tree {sorted(derived)} vs tuple {generators}")

    check("the -lech adjective class is derived",
          gate.suffixed("möglech") and gate.suffixed("härzlechi")
          and not gate.suffixed("lech") and not gate.suffixed("blech"),
          "German spells it -lich, so the suffix decides on its own")
    # The second half used to be `is_dialect("Wo-n-i das gseh ha ...")`, which
    # contains the decisive marker gseh and therefore fired however the hyphen
    # rule behaved. Asserted on what strip_addresses actually leaves behind.
    check("an address is removed, a linking hyphen is not",
          not gate.is_dialect("The host itz-prod-01 is unreachable.")[0]
          and "Wo-n-i" in gate.strip_addresses("Wo-n-i das gseh ha bini erchlüpft."),
          "GLUE cannot do this: '.' would eat the marker before a full stop")

    # COST, asserted rather than hoped for. Three times now a change has made
    # the hook quadratic and shipped: 54 seconds of CPU on a 280 KB paste in
    # round 1, an outright hang on 60 KB without whitespace in round 23, and
    # 471 ms on 6000 hyphens in round 24. Every one was found by a reviewer
    # thinking to time it, because nothing here ever did. These shapes are the
    # ones that broke it, kept so the next author does not have to guess them.
    # looks_like_address and the window cut, checked directly rather than
    # through the corpus. The mutation score said 40% and 54% for these two,
    # the lowest in the file, and the reason is arithmetic: with 104 negative
    # rows a single false positive leaves precision at 99.2%, comfortably above
    # the 97% floor, so a broken address rule does not fail the gate. Precision
    # over a corpus is the wrong instrument for one function.
    print("hook: address detection")
    addresses = [
        # each of the three ways in, on its own, so "or" cannot become "and"
        ("scheme only", "https://example", True),
        ("at-sign only", "user@host", True),
        ("dot between word characters", "viu.com", True),
        ("hyphen then a digit", "itz-prod-01", True),
        # and the near misses that must NOT be blanked
        ("a hyphen with no digit", "Wo-n-i", False),
        ("a hyphen with no digit, longer", "Änis-Chräbeli", False),
        ("a digit before the hyphen only", "01-guet", False),
        ("a trailing full stop", "guet.", False),
        ("a leading full stop", ".guet", False),
        ("a lone full stop", ".", False),
        ("two full stops", "..", False),
        ("an empty chunk", "", False),
        ("one character", "a", False),
        ("a decimal number", "3.5", True),
        ("an ordinary word", "Bärndütsch", False),
        # The FIRST hyphen decides, not the last. `chunk.find("-")` ->
        # `chunk.rfind("-")` left all fifteen probes above green while
        # api-2-prod silently stopped being an address.
        ("a digit after the first hyphen only", "api-2-prod", True),
        # And the dot test needs a word character on BOTH sides. Turning its
        # `and` into an `or` was caught only by a metamorphic relation in
        # evaluate.py, which is the corpus instrument this block replaced.
        ("a dot before a closing quote", "guet.»", False),
    ]
    for name, chunk, want in addresses:
        got = gate.looks_like_address(chunk)
        check(f"address: {name}", got == want, f"{chunk!r} -> {got}, wanted {want}")

    # The three examples the README names, held to what the tokeniser really
    # does. The README described them as if the glued runs survived, because
    # the paragraph explains the problem and the rule in the same breath and
    # nothing checked which of the two it was describing.
    for chunk, want in (("api-gateway-7d4b9c8f5-itz9q", ["api", "gateway"]),
                        ("GET /api/het/modi", ["GET"]),
                        ("YWJj/itZ+Zm9v", [])):
        got = gate.word_tokens(chunk)
        check(f"the README's example survives as documented: {chunk}",
              got == want, f"{got} wanted {want}")

    # And that stripping actually removes them from the window while leaving
    # the sentence around them.
    stripped = gate.strip_addresses("Chasch das aaluege? viu.com isch kaputt.")
    check("an address is removed and its sentence is not",
          "viu.com" not in stripped and "Chasch" in stripped and "kaputt" in stripped,
          repr(stripped))

    print("hook: window boundaries")
    # The PRECUT threshold and the head/tail cut, at their exact edges. A
    # marker is planted so a wrong offset is visible rather than merely
    # plausible.
    filler = "lorem ipsum dolor sit amet "
    def pad(n):
        return (filler * (n // len(filler) + 2))[:n]
    # Absolute sizes, not gate.PRECUT * (SCAN_HEAD + SCAN_TAIL). Deriving the
    # fixture from the constant made both of these move with any change to it:
    # deleting the pre-cut entirely and setting PRECUT = 80 both left them
    # green, because the prompt was under the threshold by construction either
    # way. 200 KB is a real thing to paste into a prompt; the numbers below are
    # what the hook has to cope with, whatever the constants say this week.
    for size in (50_000, 200_000, 1_000_000):
        check(f"a marker at the very end of a {size:,}-character paste is seen",
              "isch" in gate.word_tokens(gate.scan_window(pad(size) + " isch")))
        check(f"a marker at the very start of a {size:,}-character paste is seen",
              "isch" in gate.word_tokens(gate.scan_window("isch " + pad(size))))
    # And the pre-cut must actually cut, or strip_addresses and NFC see the
    # whole paste. Asserted on what the pre-cut DOES rather than on a constant.
    check("a 1 MB paste is reduced before the window is built",
          len(gate.scan_window(pad(1_000_000))) <= gate.SCAN_HEAD + gate.SCAN_TAIL + 1,
          f"{len(gate.scan_window(pad(1_000_000))):,} chars out")
    window = gate.scan_window(pad(200_000))
    check("the window is bounded by head plus tail",
          len(window) <= gate.SCAN_HEAD + gate.SCAN_TAIL + 1, f"{len(window)} chars")
    marked = pad(100_000) + " Das isch guet."
    check("a marker in the tail survives a large paste",
          gate.is_dialect(marked)[0])
    marked = "Das isch guet. " + pad(100_000)
    check("a marker in the head survives a large paste",
          gate.is_dialect(marked)[0])
    check("a marker only in the discarded middle does not",
          not gate.is_dialect(pad(100_000) + " Das isch guet. " + pad(100_000))[0])

    print("hook: cost")
    shapes = [
        ("6000 hyphens", "-" * 6000),
        ("alternating dot and dash", "-." * 3000),
        ("one 6000-character word", "a" * 6000),
        ("6000 escape sequences", "\\n" * 6000),
        ("2 MB of apostrophes", "a'b" * 700_000),
        ("1 MB of one umlaut", "ä" * 1_048_576),
        ("1 MB of combining marks", "a" + "\u0316" * 500_000 + "\u0334" * 500_000),
        ("2 MB of ordinary prose", "Please review the log. " * 91_000),
        # NFC-expanding codepoints: U+1D161 decomposes recursively into three
        # astral ones, two of them combining. This shape cost 70-79 ms when
        # the window normalised a slice eight times its own size.
        ("500k recursively-decomposing codepoints", "\U0001D161" * 500_000),
    ]
    # Each shape is held to what it actually costs, not to one flat ceiling.
    # A single 50 ms budget over shapes ranging from 0.4 ms to 12 ms gave the
    # cheapest of them 130x of headroom, and a budget a change has to be 130x
    # worse to trip is not a budget: PRECUT = 8 -> 40 passed it untouched. At
    # that distance the verdict also started depending on what else was
    # running, 31 ms idle against 57 ms with eleven cosmic-ray workers busy.
    # These are milliseconds measured on this repository, re-scaled at run time
    # by calibrate() so a slower or busier machine does not fail on speed alone.
    recorded = {
        "6000 hyphens": 0.6,
        "alternating dot and dash": 1.2,
        "one 6000-character word": 0.6,
        "6000 escape sequences": 0.7,
        "2 MB of apostrophes": 3.1,
        "1 MB of one umlaut": 2.3,
        "1 MB of combining marks": 0.5,
        "2 MB of ordinary prose": 3.4,
        "500k recursively-decomposing codepoints": 11.9,
    }
    # A shape with no recorded cost would silently fall back to the floor, so
    # the two lists are held to each other rather than zipped and hoped for.
    check("every cost shape has a recorded cost",
          sorted(recorded) == sorted(n for n, _ in shapes),
          str(set(recorded) ^ {n for n, _ in shapes}))
    speed = calibrate()
    for name, blob in shapes:
        spent = fastest(lambda b=blob: gate.is_dialect(b))
        # 4x the recorded cost, never below 1.5 ms, which is where timing noise
        # stops being smaller than the measurement. The 50 ms line stays as the
        # promise made to a reader: a prompt hook runs before every message,
        # and a tenth of a second of it would be felt. It is scaled too, so a
        # slow runner fails on a regression and not on being a slow runner.
        budget = max(1.5, 4 * recorded.get(name, 0) * speed)
        check(f"is_dialect stays near its recorded cost: {name}",
              spent < budget and spent < 50 * speed,
              f"{spent:.2f} ms over {len(blob):,} chars, "
              f"recorded {recorded.get(name)} ms x{speed:.2f} "
              f"-> budget {budget:.2f} ms")

    # And the shape of the curve, not only its height. All three cost defects
    # in this history were superlinear: 54 s on a 280 KB paste, an outright
    # hang on 60 KB, 471 ms on 6000 hyphens. Everything past the window is
    # sliced away without being examined, so quadrupling the input must not
    # cost four times as much. A ratio needs no recorded number and does not
    # care how fast the machine is.
    for name, unit in (("hyphens", "-" * 100_000),
                       ("apostrophes", "a'b" * 200_000),
                       ("prose", "Please review the log. " * 30_000)):
        small = fastest(lambda u=unit: gate.is_dialect(u))
        large = fastest(lambda u=unit: gate.is_dialect(u * 4))
        check(f"quadrupling the input does not quadruple the cost: {name}",
              large < max(1.5, 2.5 * small), f"{small:.2f} ms -> {large:.2f} ms")

    # Every rule that GENERATES markers must have a negative case in the
    # labelled set. The dictionary sweep is English and every collision that
    # has reached main was German: überbracht, zäh, zwo, meinige, Geflechte,
    # grottenschlechte. No word list on this machine can see those, but the
    # corpus can, and this makes adding the rule and adding its counter-example
    # one action instead of two.
    corpus = (REPO / "corpus" / "labelled.tsv").read_text(encoding="utf-8")
    negatives = [ln.split("\t", 2)[2] for ln in corpus.splitlines()
                 if ln.startswith("xx\t") and ln.count("\t") >= 2]
    # The boundary, not the acceptance. A negative row that the rule ACCEPTS
    # would be a live false positive; what pins a guard is a row whose token
    # the rule's PATTERN matches and whose guard then rejects. That is the
    # German word sitting one character away from a Bernese one.
    near_miss = [t.lower() for line in negatives
                 for t in gate.TOKEN_RE.findall(line)
                 if gate.LECH_RE.match(t.lower())]
    check("the corpus holds a non-Bernese word the -lech guard must reject",
          bool(near_miss), str(sorted(set(near_miss))[:6]) or
          "add one: a guard with no counter-example is a guard nobody checks")
    check("and the guard does reject every one of them",
          not any(gate.suffixed(t) for t in near_miss),
          str(sorted({t for t in near_miss if gate.suffixed(t)})))
    for rule, derived in (("velarised", gate.velarised(gate.DECISIVE | gate.SUPPORTING)),
                          ("plural_ig", gate.plural_ig(gate.DECISIVE | gate.SUPPORTING))):
        check(f"{rule}() produces something", bool(derived), "empty rule")

    print("hook: prompts that must load the rules")
    for i, (prompt, why) in enumerate(FIRES):
        ctx, err = run_hook(prompt, f"fire-{i}", tmp / f"fire{i}")
        check(f"fires: {why}", bool(ctx), err or f"{len(ctx or '')} chars")

    print("hook: prompts that must stay silent")
    for i, (prompt, why) in enumerate(SILENT):
        ctx, err = run_hook(prompt, f"silent-{i}", tmp / f"silent{i}")
        check(f"silent: {why}", ctx == "", err or f"injected {len(ctx or '')} chars")

    print("hook: session budget")
    cfg = tmp / "budget"
    first, err_a = run_hook("Chasch mer das erkläre?", "budget-1", cfg)
    second, err_b = run_hook("U chasch mer no säge werum?", "budget-1", cfg)
    # run_hook returns None when the hook exits non-zero, len(None) raises
    # TypeError, and the whole suite then stopped before the packaging,
    # citation and classifier checks ran: one crash hid every later result.
    # The stderr is carried into the detail rather than swallowed, so a hook
    # that will not start says why instead of reporting a mysterious 0 chars.
    if first is None or second is None:
        check("the hook runs at all for the session-budget checks", False,
              (err_a or err_b or "").strip().splitlines()[-1][:120])
    first, second = first or "", second or ""
    check("first decisive prompt gets the full rulebook", len(first) > 4000,
          f"{len(first)} chars")
    check("second gets the short checklist instead", 0 < len(second) < len(first),
          f"{len(second)} chars")
    # A supporting-only match must not spend the session's one full injection.
    # Asserted on absolute sizes, not on strong > weak: if the budget were
    # spent, the second prompt would return the CHECKLIST, which is still
    # longer than nothing, and a relative comparison can pass on both the
    # correct and the broken behaviour depending on which checklist is bigger.
    cfg2 = tmp / "budget2"
    weak, err_c = run_hook("Er het das nid gha.", "budget-2", cfg2)
    strong, err_d = run_hook("Chasch mer das erkläre?", "budget-2", cfg2)
    if weak is None or strong is None:
        check("the hook runs at all for the injection-budget checks", False,
              (err_c or err_d or "").strip().splitlines()[-1][:120])
    weak, strong = weak or "", strong or ""
    check("a supporting-only match gets the checklist", 0 < len(weak) < 2500,
          f"{len(weak)} chars")
    check("and does not consume the full injection", len(strong) > 4000,
          f"the following decisive prompt got {len(strong)} chars")

    print("hook: window")
    # The marker must STRADDLE a cut, and the filler around it must be ORDINARY
    # WORDS. Two earlier versions of this check proved nothing. The first put
    # the marker in the middle of the paste, where the window discards it
    # wholesale. The second used an unbroken run of letters as filler, so the
    # trim ate the entire window and returned a single character: the assertion
    # held with the trimming deleted, because there was nothing left to inspect
    # either way. Separated words leave a real window with a real fragment in it.
    filler = "lorem ipsum dolor sit amet "
    def pad(target):
        return (filler * (target // len(filler) + 2))[:target]
    # Two German words, because the two cuts leave opposite halves behind. A
    # head cut keeps a PREFIX, so "Verzeichnisch" cut there leaves "Verz" and
    # never "isch": the head probe held by construction and stayed green with
    # the head trim deleted, printing the invented token in its own detail
    # field. "Ischiasnerv" begins with the marker, so a wrong head trim leaves
    # exactly the fragment this is looking for.
    head_cut = pad(gate.SCAN_HEAD - 5) + " Ischiasnerv " + pad(4000)
    tail_cut = pad(4000) + " Verzeichnisch" + pad(gate.SCAN_TAIL - 9)
    for where, big in (("head", head_cut), ("tail", tail_cut)):
        window = gate.scan_window(big)
        tokens = gate.word_tokens(window.lower())
        # Guard the guard: an empty window would satisfy any "not in" assertion.
        if not check(f"the {where} window is non-empty", len(tokens) > 50,
                     f"{len(tokens)} tokens"):
            continue
        check(f"a token cut at the {where} boundary invents no marker",
              "isch" not in tokens,
              str([t for t in tokens if t not in filler.split()]))
    check("a short prompt passes through untouched",
          gate.scan_window("Chum mer luege") == "Chum mer luege")


def bdw_offline_checks():
    print("bdw: parsing")
    bdw = load(REPO / "scripts" / "bdw")

    # Entry 14099 writes a bare qualifier with a colon. The colon was not in the
    # stripped set, so the variant stayed glued to it and «Gspändleni» came back
    # as having no entry at all.
    heads = bdw.heads_of({"word": "Gspaane", "alt": "Schreibweisen: Gpsane, "
                          "Gspänli, Gschpändli, Oberland: Gspändleni",
                          "pos": "", "gloss": "", "url": ""})
    check("a colon-punctuated qualifier is stripped", "gspändleni" in heads,
          str(sorted(heads)))

    # Entry "äne, däne" continues in prose after a spaced dash, commas and all.
    ane = {"word": "äne, däne", "alt": "Schreibweisen: ääne - damit ändert nicht "
           "nur die Schreibung, auch die Aussprache! Im Raum Solothurn / "
           "Oberaargau und weiter östlich im Gebrauch.",
           "pos": "", "gloss": "", "url": ""}
    heads = bdw.heads_of(ane)
    check("the variant before a prose tail is found", "ääne" in heads)
    # Compared as an exact set, not by asking whether a few prose words are
    # absent: before the fix the prose survived as multi-word heads such as
    # "auch die aussprache! im raum solothurn", so a membership test on single
    # words passed while the parse was still wrong.
    check("the prose tail yields no headword at all",
          heads == {"äne, däne", "äne", "däne", "ääne"}, str(sorted(heads)))

    # An unspaced hyphen is part of the variant and must survive.
    heads = bdw.heads_of({"word": "Änischräbeli", "alt": "Schreibweisen: "
                          "Änis-Chräbeli", "pos": "", "gloss": "", "url": ""})
    check("a hyphenated variant is kept whole", "änis-chräbeli" in heads)

    # Entry 17410 carries a stray period between two variants, so the fragment
    # arrives as ". verworgle" and a whitespace-only strip leaves it unmatchable.
    heads = bdw.heads_of({"word": "verwörgge", "alt": "Schreibweisen: verworge, "
                          "verworgge,. verworgle", "pos": "", "gloss": "",
                          "url": ""})
    check("a stray mark at a fragment edge is removed", "verworgle" in heads,
          str(sorted(heads)))

    # Both directions of the head split, in one place, because fixing either
    # one alone has now broken the other twice.
    stolz = {"word": "stolz", "alt": "Schreibweisen: stouz, schtouz, etc.",
             "pos": "", "gloss": "", "url": ""}
    heads = bdw.heads_of(stolz)
    check("a bare abbreviation in a spelling list is not a headword",
          "etc" not in heads and "stouz" in heads, str(sorted(heads)))

    # A Schreibweisen field that holds PROSE must yield no variants at all.
    # Entry 13962 fills it with a German sentence about compound spelling, and
    # every clause of it became an EXACT headword: the query "Nomen werden
    # meist zusammen geschrieben" was certified as a Bernese word.
    prose = ("Schreibweisen: Nomen werden meist zusammen geschrieben "
             '("Huereseich"), doch trifft man auch die getrennte Schreibung '
             'an: "Das isch e huere Seich".')
    heads = bdw.heads_of({"word": "huere-", "alt": prose, "pos": "", "gloss": "",
                          "url": ""})
    check("a prose Schreibweisen field yields no spelling variant",
          not any(len(h.split()) > 4 for h in heads), str(sorted(heads)))

    # A two-token region label. The qualifier loop strips one known word at a
    # time, so "Stadt Bern: neime" never produced the bare word and bdw called
    # a listed variant nonexistent.
    heads = bdw.heads_of({"word": "nöime", "alt": "Schreibweisen: Stadt Bern: "
                          "neime, neimedüre", "pos": "", "gloss": "", "url": ""})
    check("a multi-token region label is stripped", "neime" in heads,
          str(sorted(heads)))

    # A grammatical label in front of a variant. plausible() rejected the whole
    # fragment because a lower-case word follows the full stop, so the variant
    # never reached the qualifier loop that exists to strip that very label.
    heads = bdw.heads_of({"word": "abhälfe", "alt": "Schreibweisen: abhäufe, "
                          "PP. abghoufe", "pos": "", "gloss": "", "url": ""})
    check("a variant behind a grammatical label survives", "abghoufe" in heads,
          str(sorted(heads)))

    # Sentence punctuation at the END of a fragment. tidy() stripped it off the
    # edges before plausible() looked for it, so the veto could never fire in
    # the position sentence punctuation actually occupies.
    tag = {"word": "Tag", "pos": "m., Pl. Tage, Dim. Tägli n.",
           "alt": 'Schreibweisen: Taag, das allgemein schweizerische "Tääg" '
                  'hört man oft, wir empfehlen es nicht!',
           "gloss": "", "url": ""}
    heads = bdw.heads_of(tag)
    check("a clause ending in sentence punctuation is not a headword",
          "wir empfehlen es nicht" not in heads and "taag" in heads,
          str(sorted(heads)))
    # And the same entry proves the pos bracket is harvested: Tägli is listed
    # only there, never in the Schreibweisen line.
    check("forms listed in the grammar bracket are heads", "tägli" in heads,
          str(sorted(heads)))

    # An unbracketed note continuation, with no dash to cut at.
    heads = bdw.heads_of({"word": "Brosme", "pos": "m., Pl. unverändert, "
                          "Dim. Brösmeli, Bröseli, Brösi n.",
                          "alt": "Schreibweisen: Broosme, Bröösmeli Aussprache: "
                                 "langes o/ö in Broosme/Bröösmeli/Brööseli, "
                                 "kurzes ö in Bröseli/Brösi",
                          "gloss": "", "url": ""})
    check("a named note label is cut like a dash", "langes o" not in heads,
          str(sorted(h for h in heads if " " in h)))
    check("both diminutives of the same entry are heads",
          {"bröseli", "brösi"} <= heads, str(sorted(heads)))
    check("a grammar placeholder is not a head", "unverändert" not in heads)

    # The grammar bracket. Reading it recovers the inflected forms the site
    # lists nowhere else, and it brought the LABELS with it: Adj., Adv., m., f.
    # and n. became heads of their own, so `bdw adj` answered EXACT for ten
    # unrelated entries. Twenty-three of fifty-six real entries did it.
    heads = bdw.heads_of({"word": "suber", "pos": "Adj./Adv.", "alt": "",
                          "gloss": "", "url": ""})
    check("a bare grammar label is not a headword", heads == {"suber"},
          str(sorted(heads)))
    heads = bdw.heads_of({"word": "Brosme", "alt": "",
                          "pos": "m., Pl. unverändert, Dim. Brösmeli, Bröseli, "
                                 "Brösi n.", "gloss": "", "url": ""})
    check("the forms in the bracket are still harvested",
          {"brösmeli", "bröseli", "brösi"} <= heads, str(sorted(heads)))
    check("and no fragment carries a label with it",
          not any("." in h or h.split()[-1] in bdw.QUALIFIERS for h in heads),
          str(sorted(h for h in heads if " " in h or "." in h)))

    # The QUERY goes through the same cleaning as the heads. It did not, so a
    # word typed or pasted with punctuation on it could never equal any head,
    # and the answer was the flat "no entry" this tool must not give.
    for raw, want in (("«suber»", "suber"), ("gäng,", "gäng"),
                      ('"öppis"', "öppis"), ("verworgle.", "verworgle")):
        check(f"the query {raw} is cleaned to {want}", bdw.tidy(raw) == want,
              repr(bdw.tidy(raw)))

    # The headword itself is never filtered, whatever it looks like. Filtering
    # it made bdw answer "this word does not exist" for öppis and öpper.
    for word in ("öppis", "öpper", "sech"):
        heads = bdw.heads_of({"word": word, "alt": "", "pos": "", "gloss": "",
                              "url": ""})
        check(f"the headword «{word}» is its own match", word in heads)

    # But a bare reflexive split off a composite headword is not a form of it.
    heads = bdw.heads_of({"word": "gfreue, sech", "alt": "", "pos": "",
                          "gloss": "", "url": ""})
    check("a split-off bare «sech» is not a headword", "sech" not in heads,
          str(sorted(heads)))

    # A multi-word idiom must not register its last word as a headword.
    heads = bdw.heads_of({"word": "es git ke Zwyfu", "alt": "", "pos": "",
                          "gloss": "", "url": ""})
    check("the last word of an idiom is not a headword", "zwyfu" not in heads,
          str(sorted(heads)))


def notice_online_checks():
    """NOTICE quotes pdf-overlap. Check that the quote is still the output.

    Needs the PDF and pdftotext, so it lives with the network checks. Every
    headline line the script prints for each of the three files must appear in
    NOTICE verbatim. Four rounds running, a hand-maintained sentence in NOTICE
    disagreed with the block printed directly above it.
    """
    print("NOTICE: quoted measurements are current")
    notice = (REPO / "NOTICE").read_text(encoding="utf-8")
    targets = [[], ["--file", "rules/schrybwys-compact.md"],
               ["--file", "hooks/berndeutsch_gate.py"]]
    for target in targets:
        proc = subprocess.run([sys.executable, str(REPO / "scripts" / "pdf-overlap")]
                              + target, capture_output=True, text=True,
                              cwd=str(REPO), timeout=300)
        label = " ".join(target) or "the rulebook"
        if proc.returncode != 0:
            # pdf-overlap needs the network and pdftotext. Neither is a property
            # of this repository, and exit 3 is the script itself refusing to
            # guess without poppler. Whether NOTICE is current is then genuinely
            # unknown, and unknown is reported as skipped, not as failed. The
            # extraction logic that actually broke is covered offline in
            # overlap_checks().
            why = proc.stderr.strip().splitlines()[-1] if proc.stderr else \
                f"exit {proc.returncode}"
            SKIPPED.append(f"NOTICE vs pdf-overlap for {label}: {why}")
            print(f"  skip  NOTICE vs {label}  {why}")
            continue
        headline = [ln.strip() for ln in proc.stdout.splitlines()
                    if ln.startswith(("illustrative words", "also occurring",
                                      "shared ", "no shared", "sequences that are",
                                      "every shared sequence"))]
        # A subset test passes trivially on an empty subset. If the prefixes
        # ever stop matching the script's output, this check would go green on
        # a NOTICE quoting nothing at all, which is the failure it exists to
        # catch. Every target prints at least the two count lines plus a verdict.
        if not check(f"pdf-overlap output for {label} was read",
                     len(headline) >= 3, f"{len(headline)} headline lines"):
            continue
        missing = [ln for ln in headline if ln not in notice]
        check(f"NOTICE quotes {label} correctly", not missing,
              "; ".join(missing[:2]))


def bdw_online_checks():
    print("bdw: against berndeutsch.ch")
    cases = [
        ("öppis", 0), ("öpper", 0), ("gäng", 0),      # real headwords
        ("ääne", 0), ("Gspändleni", 0),               # variants that once failed
        ("Änis-Chräbeli", 0),                         # hyphen kept whole
        ("xyzzyfoobar", 1),                           # genuinely absent
    ]
    for word, expected in cases:
        proc = subprocess.run([sys.executable, str(REPO / "scripts" / "bdw"),
                               "-q", word], capture_output=True, text=True,
                              timeout=180)
        if proc.returncode == 2:
            # Exit 2 is bdw correctly saying "unknown", which is what it emits
            # when the network is down or the page cap truncated the walk. It is
            # not a repository failure, and recording it as one made an offline
            # machine look like a broken repository, which is exactly what the
            # module docstring promises will not happen. Counted and reported,
            # not failed.
            SKIPPED.append(f"«{word}»: lookup unknown, not disproven")
            print(f"  skip  «{word}»  lookup unknown (transport or page cap)")
            continue
        check(f"«{word}» exits {expected}", proc.returncode == expected,
              f"got {proc.returncode}")


def corpus_checks():
    print("bd-corpus: scoring")
    corpus = load(REPO / "scripts" / "bd-corpus")
    for name in ("L_VOC", "BERN", "OTHER"):
        pattern = getattr(corpus, name)
        alts = pattern.pattern.split("(", 1)[1].rsplit(")", 1)[0].split("|")
        # On the SOURCE alternation, not through the compiled pattern. The
        # patterns carry re.IGNORECASE, so searching them for a capitalised
        # alternative always succeeds and the check could no longer see the
        # regression it exists for: score() lowercases its input, and a marker
        # written Wäut stopped counting while still looking present.
        upper = sorted(a for a in alts if a != a.lower())
        check(f"every {name} alternative is written lower case", not upper,
              str(upper))
        dead = [a for a in alts if not pattern.search(a.lower())]
        check(f"every {name} marker can match lowercased text", not dead, str(dead))
    # Article-sized fixtures, not one-liners. score() normalises to markers per
    # 1000 characters, so a 48-character snippet is amplified about twentyfold
    # and lands at 333 against a threshold of 1.0. At that distance the marker
    # WEIGHTS are untested: changing them from 4 and -6 to 1 and -1 left the
    # suite green. A realistic paragraph puts the result in the range the
    # threshold actually discriminates in.
    padding = ("Die Sitzig het am Nomittag im ne chlyne Saal aagfange u het "
               "meh weder zwo Stund duuret, wil no viu z bespräche gsy isch. ")
    neutral = ("Der Text beschreibt ein Verfahren, das in mehreren Schritten "
               "abläuft und dabei verschiedene Aspekte gleichzeitig behandelt. ")
    bernese = ("Es isch aut u viu Gäud gsy, si hei nid gwüsst was mache. "
               + padding * 6)
    other = ("Es isch bikannt gsii, si hän nöd gwüsst, mit viel Ziit. "
             + neutral * 6)
    for label, text, ok in (
            ("Bernese text scores above the keep threshold", bernese,
             lambda s: s > corpus.KEEP_THRESHOLD),
            ("another Alemannic variant scores below zero", other,
             lambda s: s < 0)):
        score = corpus.score(text)
        check(label, ok(score), f"{score:.2f} over {len(text)} chars")
    # And the two must be far enough apart that the threshold is doing work
    # rather than sitting in the noise.
    check("the two are separated by more than the threshold",
          corpus.score(bernese) - corpus.score(other) > corpus.KEEP_THRESHOLD,
          f"{corpus.score(bernese) - corpus.score(other):.2f} apart")

    # The WEIGHTS themselves. The paragraphs above land about 47x above the
    # threshold, so changing 4 and -6 to 1 and -1 left every check up to here
    # green: the comment eight lines up claimed the article-sized fixture had
    # fixed that and it had not. A ratio between three texts of the SAME length
    # measures the weights and nothing else, and does not care how far from the
    # threshold any of them sits.
    pad = "und dann noch etwas Text zum Auffuellen der Zeile hier. " * 8
    only_bern = ("isch nid het hei " + pad)[:300]
    only_voc = ("viu wöu gäud schnäu " + pad)[:300]
    only_other = ("nöd gsii deet hän " + pad)[:300]
    # Same length and the same number of markers, or the ratio measures the
    # fixture. The first version of this got the padding wrong, all three came
    # out at a different length, and 4.0 arrived as 3.95.
    counts = (len(corpus.BERN.findall(only_bern)),
              len(corpus.L_VOC.findall(only_voc)),
              len(corpus.OTHER.findall(only_other)))
    if check("the three weight fixtures are comparable",
             len({len(only_bern), len(only_voc), len(only_other)}) == 1
             and len(set(counts)) == 1 and counts[0] == 4,
             f"lengths {len(only_bern)}/{len(only_voc)}/{len(only_other)}, "
             f"markers {counts}"):
        b, v, o = (corpus.score(t) for t in (only_bern, only_voc, only_other))
        check("a Lötschberg-vocabulary marker counts four times a common one",
              abs(v - 4 * b) < 0.01, f"{v:.2f} against 4 x {b:.2f}")
        check("a foreign-variant marker counts minus six",
              abs(o + 6 * b) < 0.01, f"{o:.2f} against -6 x {b:.2f}")


def packaging_checks():
    """Check the manifest that `claude plugin validate` does not look at.

    At the repo root the marketplace manifest shadows the plugin: validate
    reports only "Validating marketplace manifest" and exits 0 with
    hooks/hooks.json corrupted four different ways. That file is the entire
    wiring for a plugin install, so nothing was checking the thing most likely
    to break silently.
    """
    print("packaging: hooks.json")
    try:
        wiring = json.loads((REPO / "hooks" / "hooks.json").read_text())
    except Exception as exc:
        check("hooks.json parses", False, f"{type(exc).__name__}: {exc}")
        return
    check("hooks.json parses", True)
    entries = wiring.get("hooks", {}).get("UserPromptSubmit")
    if not check("it registers a UserPromptSubmit hook", bool(entries),
                 str(sorted(wiring.get("hooks", {})))):
        return
    commands = [h for entry in entries for h in entry.get("hooks", [])]
    check("exactly one command is registered", len(commands) == 1,
          str(len(commands)))
    for command in commands:
        check("the command is the exec form with args",
              command.get("type") == "command" and isinstance(command.get("args"), list),
              str(command))
        args = command.get("args") or []
        target = args[0] if args else ""
        check("its script path uses ${CLAUDE_PLUGIN_ROOT}",
              "${CLAUDE_PLUGIN_ROOT}" in target, target)
        resolved = REPO / target.replace("${CLAUDE_PLUGIN_ROOT}/", "")
        check("its script path resolves inside the repo", resolved.is_file(),
              str(resolved))
        check("the script is executable", os.access(resolved, os.X_OK), str(resolved))

    # Nothing outside the known top-level names. Two artefacts of the relative
    # CLAUDE_CONFIG_DIR bug were committed before it was fixed: relcfg/ and a
    # directory whose name is a single space.
    listing = subprocess.run(["git", "ls-files"], cwd=str(REPO),
                             capture_output=True, text=True)
    tracked = [p for p in listing.stdout.split("\n") if p.strip()]
    if listing.returncode != 0 or not tracked:
        # Without this, an absent git leaves stdout empty, tracked is [""] and
        # the check reports ok having verified nothing at all.
        SKIPPED.append("tracked-path check: git ls-files gave nothing")
        print("  skip  no tracked path outside the known set  (git unavailable)")
    else:
        allowed = {"hooks", "scripts", "rules", "corpus", ".claude-plugin",
                   ".github", "README.md", "NOTICE", "LICENSE", ".gitignore"}
        stray = sorted({p.split("/")[0] for p in tracked} - allowed)
        check("no tracked top-level name outside the known set", not stray,
              str(stray))
        # And every segment, not only the first. The guard inspected
        # path.split("/")[0], so scripts/relcfg/... and hooks/ /... would both
        # have passed: the two artefacts it was written for, moved one level
        # down. A blank or whitespace segment is never intentional.
        buried = sorted(p for p in tracked
                        for seg in p.split("/")
                        if not seg.strip() or seg == "relcfg")
        check("no tracked path has a blank or stray segment", not buried,
              str(buried))

    plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    names = [p.get("name") for p in market.get("plugins", [])]
    check("the marketplace lists the plugin by its own name",
          plugin.get("name") in names, f"{plugin.get('name')} vs {names}")
    versions = [p.get("version") for p in market.get("plugins", [])
                if p.get("name") == plugin.get("name")]
    check("both manifests agree on the version",
          versions == [plugin.get("version")],
          f"{plugin.get('version')} vs {versions}")


def overlap_checks():
    """Check that pdf-overlap still measures the text it claims to measure.

    Round 13 had it counting 943 words of Python source instead of the 101-word
    CHECKLIST embedded in it, and reporting a comfortable percentage for the
    wrong text. No network and no pdftotext needed: the extraction is the part
    that broke, so that is the part checked.
    """
    print("pdf-overlap: what it measures")
    overlap = load(REPO / "scripts" / "pdf-overlap")
    hook_source = HOOK.read_text(encoding="utf-8")
    text, embedded = overlap.rules_text(HOOK, hook_source)
    check("a .py target measures its embedded constant, not the source",
          embedded == "CHECKLIST" and len(text) < len(hook_source) / 4,
          f"{embedded}, {len(text)} of {len(hook_source)} chars")
    check("the embedded text is the checklist itself",
          "Quick checklist" in text and "def " not in text)
    book = REPO / "rules" / "schrybwys.md"
    text, embedded = overlap.rules_text(book, book.read_text(encoding="utf-8"))
    # Both halves. `embedded is None` alone was satisfied by returning "" for
    # every non-.py file, which would have measured the overlap of an empty
    # rulebook against the PDF and reported a reassuring 0%.
    check("a markdown target is measured whole",
          embedded is None and text == book.read_text(encoding="utf-8"),
          f"{embedded}, {len(text)} chars")


def mutation_order_checks():
    """The measured source must be read AFTER the tree is staged.

    Read before, and a live run attributes its mutants using the PREVIOUS
    run's copy: every line number still resolves to some function, so the
    report looks entirely normal and is wrong by however many lines the file
    has moved. That shipped, and the wrong numbers were quoted to a user
    before anything noticed. Checked as source ORDER because that is exactly
    what the invariant is.
    """
    print("mutation.py: reads the source it measured")
    tree = ast.parse((REPO / "scripts" / "mutation.py").read_text(encoding="utf-8"))
    main = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if not check("mutation.py has a main()", main is not None):
        return
    stage_line = source_line = None
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "stage":
            stage_line = node.lineno
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", "") == "source"):
            source_line = node.lineno
    if not check("it stages and it reads a source", stage_line and source_line,
                 f"stage at {stage_line}, source at {source_line}"):
        return
    check("the source is read after staging", source_line > stage_line,
          f"stage() on line {stage_line}, source read on line {source_line}")

    # Mutants inside a nested closure belong to the function that contains it.
    # Attributed to the closure's own name they fall outside DETECTION and are
    # dropped from the score, so refactoring a branch into a helper RAISES the
    # number by deleting the mutants it was failing.
    mutation = load(REPO / "scripts" / "mutation.py")
    # Against the real hook, not a toy. A two-line sample produces one span, so
    # owner() went untested; and with ast.walk recording nested defs the
    # sort-then-first-match still returned "outer", so collect() went untested
    # too. Either half could be broken alone and this check stayed green. The
    # hook has a genuine nested closure, counts() inside is_dialect, which is
    # the exact case that once removed 18 mutants from the score.
    gate_source = HOOK.read_text(encoding="utf-8")
    owner = mutation.owner_map(gate_source)
    inside = [n.lineno for n in ast.walk(ast.parse(gate_source))
              if isinstance(n, ast.FunctionDef) and n.name == "counts"]
    if check("the hook still has the nested closure this pins", bool(inside)):
        check("a nested closure is attributed to its enclosing function",
              owner(inside[0] + 1) == "is_dialect",
              f"line {inside[0] + 1} -> {owner(inside[0] + 1)}")
    total = len(gate_source.splitlines())
    check("the closure is never an owner in its own right",
          "counts" not in {owner(n) for n in range(1, total + 1)})

    # Every name the score filters on must still exist in the hook. Renaming
    # word_matches raised the reported figure from 83.1% to 84.4% by removing
    # the 25 mutants that function was doing worst on.
    gate_src = HOOK.read_text(encoding="utf-8")
    defined = {n.name for n in ast.parse(gate_src).body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    absent = sorted(set(mutation.DETECTION) - defined)
    check("every DETECTION name still exists in the hook", not absent, str(absent))


def config_dir_checks():
    """An unresolvable ~user must yield nowhere, not a directory called "~user".

    expand() falls back to the literal string so the hook survives, but
    mkdir(parents=True) on a name starting with a tilde does not fail: it
    creates that directory wherever the hook was started, which for a prompt
    hook is the user's project, on every prompt.
    """
    print("hook: config directory")
    gate = load(HOOK)
    saved = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = "~nosuchuser-" + "x" * 12 + "/.claude"
        got = gate.config_dir()
        check("an unresolvable ~user gives no config directory", got is None,
              str(got))
        # A RELATIVE value did the same damage without a tilde: the hook built
        # its state tree in whatever directory it was started in, which for a
        # prompt hook is the user's project. It happened: a review agent's
        # probe left relcfg/cache/berndeutsch-gate/env-test in this repository
        # and it was committed.
        for bad in ("relcfg", "./x/y", " ", "cache"):
            os.environ["CLAUDE_CONFIG_DIR"] = bad
            check(f"a relative config dir gives nowhere: {bad!r}",
                  gate.config_dir() is None, str(gate.config_dir()))
        os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/bd-cfg-check"
        check("an ordinary path still resolves",
              gate.config_dir() == Path("/tmp/bd-cfg-check"))
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved


def installer_checks():
    """The installer must never unlink its own source.

    With CLAUDE_CONFIG_DIR resolving to the repository, link() unlinked the
    destination and symlinked it to the source, which were the same path: the
    hook and bdw became self-referential symlinks and 32 KB of working code was
    gone.
    """
    print("install.py: does not eat the repository")
    install = load(REPO / "scripts" / "install.py")
    with tempfile.TemporaryDirectory(prefix="bd-install-") as tmp:
        src = Path(tmp) / "thing.py"
        src.write_text("# content\n")
        before = src.read_text()
        install.link(src, src)
        # And under a differently-cased path, because macOS is case-insensitive
        # and the first version of the guard compared resolve() STRINGS: /x/Dir
        # and /x/DIR are one file and two strings, so it destroyed the source
        # exactly as before.
        cased = Path(tmp) / "sub"
        cased.mkdir()
        real = cased / "thing.py"
        real.write_text("# content\n")
        install.link(real, Path(str(cased).replace("sub", "SUB")) / "thing.py")
        check("linking through a differently-cased path leaves it intact",
              real.is_file() and not real.is_symlink(),
              "symlink" if real.is_symlink() else "ok")
        check("linking a file onto itself leaves it intact",
              src.is_file() and not src.is_symlink() and src.read_text() == before,
              "symlink" if src.is_symlink() else "ok")
        # Both checks above assert that the SOURCE survived, which an installer
        # that installs nothing satisfies perfectly: replacing link()'s body
        # with `return dst` left this whole function green. The guard against
        # eating the repository has to be paired with the thing it guards, or
        # the safest possible installer is one that does not work.
        target = Path(tmp) / "elsewhere" / "thing.py"
        install.link(src, target)
        check("linking to a distinct path creates the link",
              target.is_symlink() and target.resolve() == src.resolve(),
              f"symlink={target.is_symlink()}, "
              f"{target.resolve() if target.exists() else 'missing'}")


def citation_checks():
    """The cited titles must agree with each other across files.

    Zingg's document was corrected in NOTICE and left wrong in rules/, so the
    repository cited one document under two titles for a whole round.
    """
    print("citations: consistency across files")
    titles = {
        "usschpraach-naach": "Zingg, the aussprach-nah document",
        "schriftsprach-nach": "Pinheiro-Weber, the schriftsprach-nah document",
    }
    wrong = "uussprach-nach"
    for path in (REPO / "NOTICE", REPO / "rules" / "schrybwys.md",
                 REPO / "rules" / "schrybwys-compact.md", REPO / "README.md"):
        body = path.read_text(encoding="utf-8")
        check(f"{path.name} does not use the old wrong Zingg title",
              wrong not in body)
        if "Zingg" in body:
            check(f"{path.name} cites Zingg by the document's own title",
                  "usschpraach-naach" in body)
    check("the correct title is recorded somewhere",
          any(t in (REPO / "NOTICE").read_text(encoding="utf-8") for t in titles))

    # The rulebook prescribed the doubled participle and then, ten lines later,
    # taught the undoubled one in its own grammar example, in all three copies
    # at once. A rulebook whose stated golden rule is "be consistent" cannot
    # contradict itself in its own examples.
    print("rulebook: does not contradict itself")
    overlap = load(REPO / "scripts" / "pdf-overlap")
    for path in (REPO / "rules" / "schrybwys.md",
                 REPO / "rules" / "schrybwys-compact.md", HOOK):
        # The rules TEXT only. For the hook that is the embedded CHECKLIST: its
        # surrounding comments discuss the German "im Gange" on purpose, and
        # matching those would be a check that can only be satisfied by
        # deleting the explanation of why the marker was demoted.
        body, _ = overlap.rules_text(path, path.read_text(encoding="utf-8"))
        # Assert the prescription is present rather than using it as a guard.
        # As a guard, deleting `ggange` altogether SKIPPED the check instead of
        # failing it, so the way to make this pass was to remove the rule.
        if not check(f"{path.name} prescribes the doubled participle",
                     "ggange" in body):
            continue
        stray = re.findall(r"(?<!\w)gange\b", body)
        check(f"{path.name} teaches the doubled participle everywhere",
              not stray, f"{len(stray)} undoubled use(s)")


def classifier_checks():
    """The detector's measured recall and precision, not an opinion about it.

    scripts/evaluate.py scores the hook against corpus/labelled.tsv. It is
    invoked here so a change that quietly makes the classifier worse fails the
    ordinary suite rather than being noticed three review rounds later, which
    is exactly what happened: sixteen rounds of review chased false positives
    until precision reached 100%, while recall sat at 82.7% and nobody had
    measured it. Every miss is silent, so nothing was ever going to report it.
    """
    print("classifier: measured against the labelled set")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate.py"),
                           "--gate"], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300)
    for line in proc.stdout.splitlines():
        if line.strip().startswith(("recall", "precision", "GATE FAILED")):
            print("  " + line.strip())
    check("the classifier meets its recorded floor", proc.returncode == 0,
          proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else
          proc.stderr.strip()[:120])


def readme_number_checks():
    """Numbers the README states about files must be recomputed, not trusted.

    The compact block grew by two characters in an ordinary edit and the README
    kept quoting the old figure, in two places. A number about a file is the
    same kind of claim as NOTICE's quoted measurement: it rots silently, and the
    only defence is to compute it here.
    """
    print("README: stated numbers")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    compact = (REPO / "rules" / "schrybwys-compact.md").read_text(encoding="utf-8")
    # What the README tells a reader to paste: everything below the `---`.
    block = compact.split("---", 1)[1].strip()
    check(f"the compact block is described as {len(block)} characters",
          f"{len(block)}-character" in readme
          and f"in {len(block)} characters" in readme,
          f"actually {len(block)}")
    stale = sorted({n for n in re.findall(r"\b(1[0-9]{3})[- ]char", readme)
                    if n != str(len(block))})
    check("no other character count is quoted for it", not stale, str(stale))

    # The two injection sizes the diagram and the cost paragraph quote. These
    # are what a reader is deciding on, and they move whenever the rulebook or
    # the preamble is edited: the compact block already drifted by two
    # characters and was quoted stale in two places. Measured through
    # build_context, so what is checked is what is actually sent.
    gate = load(HOOK)
    # In an empty config directory, or the machine's own overlays join in: this
    # measured 12,165 characters on the author's laptop against the 9,243 a
    # fresh install sends, because build_context appends every personal
    # berndeutsch-schrybwys.md it finds. The README documents what the package
    # does, not what one machine does with it.
    with tempfile.TemporaryDirectory(prefix="bd-sizes-") as empty:
        saved = {k: os.environ.get(k) for k in
                 ("CLAUDE_CONFIG_DIR", "BERNDEUTSCH_RULES", "BERNDEUTSCH_IDIOLECT")}
        try:
            os.environ["CLAUDE_CONFIG_DIR"] = empty
            os.environ.pop("BERNDEUTSCH_RULES", None)
            os.environ.pop("BERNDEUTSCH_IDIOLECT", None)
            # HOOK.parent, which is what main() passes: the hook's DIRECTORY,
            # not the file. Passing the file found the rulebook but not bdw,
            # and quietly measured 311 characters short.
            full, emitted = gate.build_context(HOOK.parent, True, None)
            short, _ = gate.build_context(HOOK.parent, False, None)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    # build_context returns (text, emitted). Taking len() of the tuple gave a
    # confident 2 characters for both, which is the shape of mistake this whole
    # section exists to catch, so it is asserted rather than assumed.
    if check("the full injection really carries the rulebook", emitted
             and len(full) > 4000, f"{len(full):,} chars, emitted={emitted}"):
        pairs = ((len(full), "about 9 KB"), (len(short), "about 1.5 KB"))
        for size, quoted in pairs:
            check(f"the README quotes {quoted} and it is {size / 1024:.1f} KB",
                  quoted in readme
                  and abs(size / 1024 - float(quoted.split()[1])) < 0.05,
                  f"{size:,} chars")
        cost = (f"costs {len(short) / 1024:.1f} KB rather than "
                f"{len(full) / 1024:.0f} KB")
        check("and the cost paragraph quotes the same pair", cost in readme, cost)
    # And the window size, which the diagram states twice in words.
    check("the README states the window the hook actually uses",
          readme.count(f"first {gate.SCAN_HEAD} + last {gate.SCAN_TAIL}") == 1
          and f"first and last {gate.SCAN_HEAD} characters" in readme,
          f"{gate.SCAN_HEAD}/{gate.SCAN_TAIL}")

    # The classifier's scores must not be stated as a current fact anywhere the
    # tool is described, because they move on every corpus edit. The README
    # published "100% and 100%" while evaluate.py printed 93.0% and 95.7%, and
    # four independent reviewers reported it in one round. Historical figures
    # are fine when they are marked as history; a bare percentage next to the
    # word precision or recall is not.
    #
    # Guard the guard, twice over. A loop over no matching lines checks nothing
    # and says nothing: rewording the README to "recall and precision" and
    # "mutation-testing score" made all four lines vanish from the output and
    # let it publish an invented 100%. So the phrase has to be PRESENT, and
    # then every line carrying it has to be clean.
    for phrase, what in (("precision and recall", "the score in the diagram"),
                         ("mutation score", "the mutation score")):
        lines = [(n, ln) for n, ln in enumerate(readme.splitlines(), 1)
                 if phrase in ln.lower()]
        if not check(f"the README still describes {what}", bool(lines),
                     f"no line says {phrase!r}"):
            continue
        for line_no, line in lines:
            check(f"README:{line_no} does not fix {what}",
                  not re.search(r"\d+(\.\d+)?%", line), line.strip()[:70])


def main():
    ap = argparse.ArgumentParser(prog="selftest", description=__doc__.splitlines()[0])
    ap.add_argument("--online", action="store_true",
                    help="also check bdw against berndeutsch.ch")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="bd-selftest-") as tmp:
        hook_checks(Path(tmp))
    bdw_offline_checks()
    corpus_checks()
    packaging_checks()
    overlap_checks()
    citation_checks()
    installer_checks()
    config_dir_checks()
    mutation_order_checks()
    readme_number_checks()
    classifier_checks()
    if args.online:
        notice_online_checks()
        bdw_online_checks()
    else:
        print("bdw: skipping the network checks (pass --online to run them)")

    print()
    if SKIPPED:
        # Reported, never fatal. These are checks that could not run, not
        # checks that found something.
        print(f"{len(SKIPPED)} check(s) skipped:")
        for name in SKIPPED:
            print(f"  - {name}")
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed" + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
