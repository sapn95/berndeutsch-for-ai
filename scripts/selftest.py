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
import re
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
    # Asserted on absolute sizes, not on strong > weak: if the budget were
    # spent, the second prompt would return the CHECKLIST, which is still
    # longer than nothing, and a relative comparison can pass on both the
    # correct and the broken behaviour depending on which checklist is bigger.
    cfg2 = tmp / "budget2"
    weak, _ = run_hook("Er het das nid gha.", "budget-2", cfg2)
    strong, _ = run_hook("Chasch mer das erkläre?", "budget-2", cfg2)
    check("a supporting-only match gets the checklist", 0 < len(weak) < 2500,
          f"{len(weak)} chars")
    check("and does not consume the full injection", len(strong) > 4000,
          f"the following decisive prompt got {len(strong)} chars")

    print("hook: window")
    # The marker must STRADDLE a cut. An earlier version put it in the middle
    # of the paste, where the window discards it wholesale, so the check passed
    # with the trimming removed entirely and tested nothing at all.
    head_cut = "y" * (gate.SCAN_HEAD - 6) + "Verzeichnisch" + "z" * 4000
    tail_cut = "y" * 4000 + "Verzeichnisch" + "z" * (gate.SCAN_TAIL - 6)
    for where, big in (("head", head_cut), ("tail", tail_cut)):
        window = gate.scan_window(big)
        check(f"a token cut at the {where} boundary invents no marker",
              "isch" not in gate.word_tokens(window.lower()),
              str([t for t in gate.word_tokens(window.lower()) if len(t) < 12]))
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
        if proc.returncode != 0:
            check(f"pdf-overlap {' '.join(target) or '(rulebook)'} runs", False,
                  proc.stderr.strip().splitlines()[-1] if proc.stderr else "")
            continue
        headline = [ln.strip() for ln in proc.stdout.splitlines()
                    if ln.startswith(("illustrative words", "also occurring",
                                      "shared ", "no shared", "sequences that are",
                                      "every shared sequence"))]
        missing = [ln for ln in headline if ln not in notice]
        check(f"NOTICE quotes {' '.join(target) or 'the rulebook'} correctly",
              not missing, "; ".join(missing[:2]))


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
    check("a markdown target is measured whole", embedded is None)


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
        if "ggange" not in body:
            continue
        stray = re.findall(r"(?<!\w)gange\b", body)
        check(f"{path.name} teaches the doubled participle everywhere",
              not stray, f"{len(stray)} undoubled use(s)")


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
    if args.online:
        notice_online_checks()
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
