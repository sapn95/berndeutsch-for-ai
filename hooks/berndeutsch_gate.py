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

# Two tiers, and both of them decide something.
#
# DECISIVE: a word that cannot plausibly appear in English or German running
# text. One is enough. This is where the second-person -sch verb forms live,
# because they carry most of the signal in real conversational Bernese. A
# blanket "-sch" rule is not usable, since German is full of -isch adjectives
# (technisch, logisch, praktisch), so the productive forms are listed.
DECISIVE = frozenset("""
bärndütsch berndütsch baernduetsch bärndütschi
isch isches sisch
gsy gsii gseh gsehsch
hesch heschs chasch machsch weisch bisch wottsch chunnsch gisch nimmsch
seisch tuesch muesch gohsch blybsch luegsch sägsch findsch bruuchsch
verstahsch chöisch dörfsch söttisch wirsch wohnsch schaffsch schrybsch
redsch chouffsch heissisch wosch chunsch
öppis öpper öppe mängisch itz sött söu söue wöu gäu gäud
chli chunt chume chumme chöi göh göi
machemer hämmer gömer simer
äbe grüessech vilmal gäng geng äuä äuwä nüt nüüt gits geits
""".split())

# SUPPORTING: genuinely Bernese, but each one collides with a real word or a
# technical token somewhere, so on its own it decides nothing and two are
# needed, and they must be two DIFFERENT words: "Configure the Lustre NID and
# the nid mapping" is one word twice, not two markers.
#
# gsi is the DynamoDB Global Secondary Index. nit is the English noun in
# "nit-picking". chum is an English word. kei is Dutch. nid is French for nest
# and an HPC network identifier. Each of those fired on ordinary prompts while
# it was treated as decisive.
#
# Deliberately absent: halt, grad, wäge, sowieso, merci, säge, mer, het, hoi.
# They are ordinary German or English words or, in the case of mer and het,
# fall out of a URL path once tokens are split on non-letters. Any two of them
# reached the bar on prompts containing no dialect at all.
SUPPORTING = frozenset("""
nid nit gsi kei chum witt
hei cha wott mues gah luege scho guet öi aui nöime hüt zäme churz dänk
""".split())

# One decisive marker fires. Otherwise two DISTINCT supporting markers are
# required, so neither a single ambiguous token nor one token repeated can
# trigger on an English or German prompt.
MIN_SUPPORTING = 2
TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def scan_window(text):
    """The head and tail of the prompt, without scanning the middle of a paste."""
    if len(text) <= SCAN_HEAD + SCAN_TAIL:
        return text
    return text[:SCAN_HEAD] + "\n" + text[-SCAN_TAIL:]


def is_dialect(text):
    """True when the text should be treated as Bärndütsch."""
    tokens = TOKEN_RE.findall(scan_window(text).lower())
    if any(t in DECISIVE for t in tokens):
        return True
    return len({t for t in tokens if t in SUPPORTING}) >= MIN_SUPPORTING


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


CHECKLIST = """Quick checklist:
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

    emitted = False
    if first_time:
        for book in rulebooks(here):
            try:
                out.append(book.read_text(encoding="utf-8", errors="replace"))
                emitted = True
            except OSError:
                continue
        if emitted:
            out.append("")
    if not emitted:
        # Also the path taken when no rulebook file was found or none could be
        # read. Claiming the full rulebook "was already loaded" would be a lie
        # in that case, so the checklist stands on its own wording.
        out.append(CHECKLIST)
        if not first_time:
            out.append("(The full rulebook was loaded earlier in this session.)")
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
    return "\n".join(out), emitted


def session_marker(session_id):
    """Path of this session's state file, or None if state cannot be kept."""
    if not isinstance(session_id, str) or not session_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:128]
    state_dir = config_dir() / "cache" / "berndeutsch-gate"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return state_dir / safe


def sweep(state_dir):
    try:
        cutoff = time.time() - 14 * 86400
        for stale in state_dir.iterdir():
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink()
    except OSError:
        pass


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

    if not is_dialect(prompt):
        return 0

    here = Path(__file__).resolve().parent
    marker = session_marker(payload.get("session_id"))
    first_time = not (marker and marker.exists())
    context, emitted = build_context(here, first_time)

    # Mark the session only once the rulebook has actually been emitted. An
    # unreadable rulebook would otherwise burn the one full injection and leave
    # every later prompt in the session with the short checklist alone.
    if emitted and marker:
        try:
            marker.touch()
            sweep(marker.parent)
        except OSError:
            pass

    payload_out = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
        ensure_ascii=False,
    )
    # Writing through a buffer that is not UTF-8 would truncate the JSON
    # mid-string and hand the model a parse error instead of a rulebook.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.stdout.write(payload_out)
    return 0


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except Exception:
        # A hook that crashes disrupts every prompt the user submits.
        code = 0
    try:
        sys.stdout.flush()
    except Exception:
        pass
    # os._exit skips interpreter shutdown, which is where a closed stdout would
    # otherwise print "Exception ignored in: <_io.TextIOWrapper>" to stderr and
    # turn a harmless broken pipe into visible noise on every prompt.
    os._exit(code)
