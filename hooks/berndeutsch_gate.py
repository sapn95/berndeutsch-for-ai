#!/usr/bin/env python3
"""UserPromptSubmit hook: inject the Bärndütsch rulebook when the prompt is in
Bernese German.

The rulebook otherwise lives in a memory or preferences file that is only
sometimes in context, and when it is absent the model falls back on a generic
Swiss German prior that drifts toward Zurich forms. This makes the injection
deterministic.

Detection is never a hard gate. The injected text is conditional ("if this
message is Bärndütsch, ..."), so a false positive cannot change the language of
an English answer. Put [hd] anywhere in a prompt to suppress it for that turn.

Contract: this runs on every prompt the user submits, so it must never break
one. Any internal error exits 0 with no output.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# Scoring looks at a bounded window, never the whole prompt: a pasted file can
# be megabytes, and an unbounded scan is how a prompt-submission hook ends up
# stalling on its own timeout. The window is head AND tail, because the usual
# shape of a large prompt is a paste with the actual question either before or
# after it, and a head-only window misses the second case entirely.
SCAN_HEAD = 3000
SCAN_TAIL = 3000

# Strong markers: words that essentially cannot occur in English or German
# running text. Includes the 2nd-person-singular -sch verb forms, which are the
# highest-frequency signal in real conversational Bernese. A blanket "-sch"
# rule is not usable, because German is full of -isch adjectives (technisch,
# logisch, praktisch), so the productive forms are listed explicitly.
STRONG = frozenset("""
bärndütsch berndütsch baernduetsch bärndütschi
isch isches sisch
gsy gsi gsii gseh gsehsch
hesch heschs chasch machsch weisch bisch wottsch chunnsch gisch nimmsch
seisch tuesch muesch gohsch blybsch luegsch sägsch findsch bruuchsch
verstahsch chöisch dörfsch söttisch wirsch
nid nit itz sött söu söue wöu gäu gäud öppis öpper öppe mängisch
chli chunt chume chumme chöi
machemer hämmer gömer simer
äbe grüessech vilmal geng gäng äuä äuwä
gits geits nüt nüüt kei chum
""".split())

# Weak markers: dialect-leaning, but each is a plausible word elsewhere, so on
# their own they never fire. Deliberately excluded: git, dr, ds, chan, u, we,
# wi, no, si, di. Those turn ordinary English, German and source code into
# false hits (two mentions of git, a Go `chan` signature, .DS_Store, Dr. Smith).
WEAK = frozenset("""
mer het hei cha wott mues gah luege scho dänk guet öi aui nöime hüt
emu grad zäme wäge halt sowieso merci hoi zäme churz
""".split())

# One strong marker is decisive on its own. Weak markers only ever adjust the
# score once a strong one is already present, so no combination of ordinary
# English or German words can reach the threshold.
THRESHOLD = 2
TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def scan_window(text):
    """The head and tail of the prompt, without scanning the middle of a paste."""
    if len(text) <= SCAN_HEAD + SCAN_TAIL:
        return text
    return text[:SCAN_HEAD] + "\n" + text[-SCAN_TAIL:]


def score(text):
    """Return (score, strong_hits). Weak markers count only alongside a strong one."""
    tokens = TOKEN_RE.findall(scan_window(text).lower())
    strong = sum(1 for t in tokens if t in STRONG)
    if not strong:
        return 0, 0
    weak = sum(1 for t in tokens if t in WEAK)
    return strong * 2 + weak, strong


def config_dir():
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    if explicit:
        return Path(explicit)
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".claude"


def rulebooks(here):
    """Resolve the rulebook files, most specific first, de-duplicated.

    BERNDEUTSCH_RULES *replaces* the bundled rulebook rather than stacking on
    top of it, because the two spelling systems contradict each other and
    handing the model both produces worse output than handing it neither.
    BERNDEUTSCH_IDIOLECT is an overlay and is always appended, since personal
    corrections refine whichever system is in use.
    """
    found, seen = [], set()

    def add(path):
        if not path:
            return
        p = Path(path).expanduser()
        try:
            if not p.is_file():
                return
            key = p.resolve()
        except OSError:
            return
        if key not in seen:
            seen.add(key)
            found.append(p)

    override = os.environ.get("BERNDEUTSCH_RULES")
    if override:
        add(override)
    else:
        add(here.parent / "rules" / "schrybwys.md")
        add(here.parent.parent / "rules" / "schrybwys.md")

    add(os.environ.get("BERNDEUTSCH_IDIOLECT"))
    try:
        for overlay in sorted(config_dir().glob("projects/*/memory/berndeutsch-schrybwys.md")):
            add(overlay)
    except OSError:
        pass
    return found


def lookup_tool(here):
    for candidate in (here.parent / "scripts" / "bdw", config_dir() / "scripts" / "bdw"):
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        except OSError:
            continue
    return None


CHECKLIST = """Quick checklist (full rulebook already loaded earlier in this session):
- closed i -> y (Zyt, schrybe, gsy, blybe, wyt); open i stays i (Schritt, lige);
  long open i is ii (viil, Riis)
- Zwielaut ie/ue/üe, never iä/uä/üä (Bier, guet, wüescht, Bueb, müed)
- unstressed e stays e, no Ä-inflation (Gipfeli, Meiteli, Bibeli)
- eu/äu becomes öi (Fröid, Höi, nöi, tröime)
- ds = article "das" (ds Modul); z = preposition "zu" (z Bärn, z tüe)
- l-vocalisation: aut, viu, wöu, Gäud, schnäu. But a single l between vowels
  stays: hole, Zahle, male. A double ll still vocalises: alli -> aui.
- sp/st in Anlaut stay sp/st (Stadt, Stei, verstecke); inside a word scht/schp
  (Poscht, luschtig, Wäschpi)
- no preterite, use the perfect (mir sy gange); pluperfect is a double perfect
- negation nid/nit, NEVER nöd, and no other Zurich or Basel forms
- itz (not jetz), louf (not Lauf), chlepfe (not chlöpfe), suber (not sufer)
- be CONSISTENT within one text, that is the golden rule"""


def build_context(here, first_time):
    out = [
        "<berndeutsch-schrybwys>",
        "The user's message appears to be Bärndütsch (Bernese German). If it is,",
        "reply in Bärndütsch using the house style below, NOT a generic Swiss",
        "German. If the message is actually German or English, ignore this block.",
        "",
    ]

    books = rulebooks(here)
    if first_time and books:
        for book in books:
            try:
                out.append(book.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        out.append("")
    else:
        # Also the path taken when no rulebook file was found at all. Saying
        # "already loaded" would be a lie in that case, so the wording is
        # neutral and the rules still stand on their own.
        out.append(CHECKLIST)
        out.append("")

    bdw = lookup_tool(here)
    if bdw:
        out += [
            "Word choice: when unsure whether a word or idiom is genuinely",
            f"Bärndütsch, run `{bdw} <wort>` (berndeutsch.ch lookup) instead of",
            "improvising. No exact entry means it is not a dialect headword, so a",
            "High-German loanword or a rephrase is more honest than an invented form.",
        ]
    out.append("</berndeutsch-schrybwys>")
    return "\n".join(out)


def session_seen(session_id):
    """True if this session already got the full rulebook. Marks it as seen."""
    if not session_id:
        return False
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:128]
    state_dir = config_dir() / "cache" / "berndeutsch-gate"
    marker = state_dir / safe
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        if marker.exists():
            return True
        marker.touch()
    except OSError:
        return False
    try:
        cutoff = time.time() - 14 * 86400
        for stale in state_dir.iterdir():
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink()
    except OSError:
        pass
    return False


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if "[hd]" in prompt.lower():
        return 0

    total, _ = score(prompt)
    if total < THRESHOLD:
        return 0

    here = Path(__file__).resolve().parent
    context = build_context(here, first_time=not session_seen(payload.get("session_id")))
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A hook that crashes disrupts every prompt the user submits.
        sys.exit(0)
