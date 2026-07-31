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
    scripts/selftest.py --online   # also check bdw against berndeutsch.ch

Exit code 0 if every check passes, 1 otherwise. Network checks are opt-in
because a volunteer-run dictionary should not be hit by a test loop, and
because a failing network must never look like a failing repository.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "berndeutsch_gate.py"

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


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
    """Feed one prompt through the hook exactly as Claude Code does."""
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config_dir))
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
    ("Er het das nid gha.", "supporting only, incl. two acronym-shaped"),
    ("Chum mer wei das luege.", "supporting only, no acronym"),
    ("Das isch aues viu z schnäu gange.", "l-vocalisation"),
    ("Weisch no wo mir das gmacht hei?", "mixed"),
]

# Prompts that MUST stay silent. Every one of these fired at some point.
SILENT = [
    ("Update the GHA workflow and the DynamoDB GSI projection.", "gha + gsi"),
    ("The KEI report and the Lustre NID mapping both need review.", "kei + nid"),
    ("Stop nit-picking the AUT suite configuration.", "nit + aut"),
    ("Filter rows where country is MYS and residue is Gly.", "mys + gly"),
    ("Die Migration ist noch im Gange, bitte warte damit.", "German 'im Gange'"),
    ("Please refactor this function and add a unit test.", "plain English"),
    ("Kannst du mir bitte die Konfiguration erklären?", "plain German"),
    ("git commit -m 'fix' && git push origin main", "bare git twice"),
    ('Parse the log line "error\\nID mismatch\\nItem 3" please.',
     "escaped \\n forming nid/nit"),
    ("procs memory swap io system cpu r b swpd free buff si so bi bo",
     "vmstat column headings si/so"),
]


def hook_checks(tmp):
    print("hook: marker tiers")
    gate = load(HOOK)
    check("ACRONYMISH is a subset of SUPPORTING", gate.ACRONYMISH <= gate.SUPPORTING,
          str(sorted(gate.ACRONYMISH - gate.SUPPORTING)))
    check("the two tiers do not overlap", not (gate.DECISIVE & gate.SUPPORTING),
          str(sorted(gate.DECISIVE & gate.SUPPORTING)))
    check("every marker is lowercase",
          all(m == m.lower() for m in gate.DECISIVE | gate.SUPPORTING))
    # A marker with an uppercase letter can never match, because is_dialect()
    # lowercases before testing. bd-corpus shipped exactly that bug.
    check("no marker is a single character",
          all(len(m) > 1 for m in gate.DECISIVE | gate.SUPPORTING))

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
    first, _ = run_hook("Chasch mer das erkläre?", "budget-1", cfg)
    second, _ = run_hook("U chasch mer no säge werum?", "budget-1", cfg)
    check("first decisive prompt gets the full rulebook", len(first) > 4000,
          f"{len(first)} chars")
    check("second gets the short checklist instead", 0 < len(second) < len(first),
          f"{len(second)} chars")
    # A supporting-only match must not spend the session's one full injection.
    cfg2 = tmp / "budget2"
    weak, _ = run_hook("Er het das nid gha.", "budget-2", cfg2)
    strong, _ = run_hook("Chasch mer das erkläre?", "budget-2", cfg2)
    check("supporting-only match does not consume the full injection",
          len(strong) > len(weak), f"{len(weak)} then {len(strong)} chars")

    print("hook: window")
    big = "x" * 4000 + " Verzeichnisch " + "y" * 4000
    window = gate.scan_window(big)
    check("cutting a long paste invents no marker",
          "isch" not in gate.TOKEN_RE.findall(window.lower()))
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
            check(f"«{word}»", False, "lookup unknown (transport or page cap), "
                  "not a repository failure")
            continue
        check(f"«{word}» exits {expected}", proc.returncode == expected,
              f"got {proc.returncode}")


def corpus_checks():
    print("bd-corpus: scoring")
    corpus = load(REPO / "scripts" / "bd-corpus")
    for name in ("L_VOC", "BERN", "OTHER"):
        pattern = getattr(corpus, name)
        alts = pattern.pattern.split("(", 1)[1].rsplit(")", 1)[0].split("|")
        dead = [a for a in alts if not pattern.search(a.lower())]
        check(f"every {name} marker can match lowercased text", not dead, str(dead))
    bernese = "Es isch aut u viu Gäud, si hei gsy u nid gwüsst."
    other = "Es isch bikannt gsii, si hän nöd gwüsst, mit viel Ziit."
    check("Bernese text scores above the keep threshold",
          corpus.score(bernese) > corpus.KEEP_THRESHOLD, f"{corpus.score(bernese):.1f}")
    check("another Alemannic variant scores below zero",
          corpus.score(other) < 0, f"{corpus.score(other):.1f}")


def main():
    ap = argparse.ArgumentParser(prog="selftest", description=__doc__.splitlines()[0])
    ap.add_argument("--online", action="store_true",
                    help="also check bdw against berndeutsch.ch")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="bd-selftest-") as tmp:
        hook_checks(Path(tmp))
    bdw_offline_checks()
    corpus_checks()
    if args.online:
        bdw_online_checks()
    else:
        print("bdw: skipping the network checks (pass --online to run them)")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
