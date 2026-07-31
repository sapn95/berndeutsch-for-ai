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
import unicodedata
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
#
# The -sch forms are not enough on their own, though: an imperative, a
# first-person statement or a bare greeting contains none of them. Nor is the
# singular enough: the polite form in Bernese is the second person PLURAL
# (heit, chömet, dihr), which is exactly what a stranger writes first. So the list
# also carries everyday nouns and adverbs, and the l-vocalised spellings this
# repository's own rulebook prescribes (aues, viu, schnäu, chüngu), which
# were conspicuously missing while the non-vocalised vilmal was present.
DECISIVE = frozenset("""
bärndütsch berndütsch baernduetsch bärndütschi
isch isches sisch
gsy gsii gseh gsehsch
hesch heschs chasch machsch weisch bisch wottsch chunnsch gisch nimmsch
seisch tuesch muesch gohsch blybsch luegsch sägsch findsch bruuchsch
verstahsch chöisch dörfsch söttisch wirsch wohnsch schaffsch schrybsch
redsch chouffsch heissisch wosch chunsch meinsch gasch gahsch
heit chömet chöit gö dihr
öppis öpper öppe mängisch itz sött söu söue wöu gäud niemer
chli chunt chunnt chume chumme chöme chömme chöi göh göi
nüme nümme müed gärn üs üsi üse
zyt schrybe schrybt blybe blybt zäme wyt myni gsäh wotsch
gseht gschribe gange verstande chönnt chönnte tuet gnoh gläse
machemer gömer simer gmacht gseit gwüss mitenand sälber eifach gäbig
öbe äbe grüessech vilmal gäng äuä äuwä nüt nüüt geits geit gaht gahts goht gohts
znüni zmorge zobe zvieri zäme meitschi meiteli gieu hegu bueb
aues viu viumau viumou schnäu chüngu wüescht gnue luschtig
ahnig louf louft chlepfe chlepft poschte poschtet guete
""".split())

# SUPPORTING: genuinely Bernese, but each one collides with a real word or a
# technical token somewhere, so on its own it decides nothing and two are
# needed, and they must be two DIFFERENT words: "Configure the Lustre NID and
# the nid mapping" is one word twice, not two markers.
#
# gsi is the DynamoDB Global Secondary Index, gha is GitHub Actions, nit is the
# English noun in "nit-picking", chum is an English word, kei is Dutch, nid is
# French for nest and an HPC network identifier. Each of those fired on ordinary
# prompts while it was treated as decisive; gha did it on every CI question.
#
# Also demoted rather than dropped: hämmer is the German plural of Hammer,
# geng is a common Chinese surname, gits is an English plural, and gäu is a
# German toponym (das Gäu, Bezirk Gäu SO, Gäuboden). Each is real Bernese, and
# none of them should decide a prompt on its own.
#
# Removed outright from both tiers: ig, aui, ke, wei. IG, AUI and KE are
# ordinary acronyms and Wei is a common Chinese given name, so two of them in
# one English engineering prompt cleared the supporting bar between them. aui
# is the l-vocalised "alli" and belongs in the language, but AUI is an acronym
# and aues covers the same ground without the collision.
#
# aut, modi and ching are supporting rather than decisive for the same reason:
# AUT is "application under test", modi is Italian and a surname, Ching is a
# Chinese surname.
#
# het is the copula and is everywhere in real Bernese, so it is supporting
# rather than decisive. mys and gly likewise: MYS is the ISO 3166-1 code for
# Malaysia and Gly is the three-letter code for glycine, and both are
# low-frequency next to myni, zyt and schrybe.
#
# sy, si and ig were here and are now in neither tier. si and sy are adjacent
# COLUMN HEADINGS in vmstat and top output, so pasting a performance dump into
# an English debugging question cleared the bar on its own.
#
# Deliberately absent from both tiers: halt, grad, wäge, sowieso, merci, säge,
# mer, hoi, and bare git. They are ordinary German or English words or, in
# the case of mer and het, fall out of a URL path once tokens are split on
# non-letters. Any two of them reached the bar on prompts with no dialect at
# all, and git reached it twice in one shell command.
SUPPORTING = frozenset("""
nid nit gsi gha kei chum witt geng gits hämmer gäu aut modi ching
het mys gly
hei cha wott mues gah luege scho guet öi nöime hüt churz dänk söll
""".split())

# One decisive marker fires. Otherwise two DISTINCT supporting markers are
# required, so neither a single ambiguous token nor one token repeated can
# trigger on an English or German prompt.
MIN_SUPPORTING = 2
TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def scan_window(text):
    """The head and tail of the prompt, without scanning the middle of a paste.

    NFC first, because a decomposed umlaut would shatter every marker that
    contains one. Both cuts land on whitespace: slicing mid-word leaves a
    fragment, and a fragment can be a marker that the real text never
    contained, e.g. German "Verzeichnisch..." cut after "...isch".
    """
    text = unicodedata.normalize("NFC", text)
    # A pasted JSON log or an escaped error string contains literal \n, \r and
    # \t two-character sequences. The backslash is not a letter, so "...\nID"
    # tokenises to "nid" and "...\nIt" to "nit", conjuring supporting markers
    # out of ordinary English. Turn the escapes into the whitespace they denote
    # before tokenising.
    text = re.sub(r"\\[nrt]", " ", text)
    if len(text) <= SCAN_HEAD + SCAN_TAIL:
        return text
    # Strip the partial token at each cut with the same character class the
    # tokeniser uses, rather than hunting for a space. A minified file or a
    # base64 blob can contain no space in the whole window, and then a
    # space-based trim leaves the fragment in place.
    head = re.sub(r"[^\W\d_]+$", "", text[:SCAN_HEAD], flags=re.UNICODE)
    tail = re.sub(r"^[^\W\d_]+", "", text[-SCAN_TAIL:], flags=re.UNICODE)
    return head + "\n" + tail


def is_dialect(text):
    """Return (fire, certain).

    `certain` is True only when a decisive marker was seen. A match carried by
    supporting markers alone still injects, because the cost of being wrong is
    a conditional instruction the model ignores, but it gets the short
    checklist rather than the full rulebook and does not consume the session's
    one full injection. That caps what any residual collision can cost: three
    supporting markers in one English sentence ("run the AUT suite, check the
    modi list with Ching") is 1.4 KB, not 9 KB, and the next genuinely Bernese
    prompt in that session still gets the whole thing.
    """
    tokens = TOKEN_RE.findall(scan_window(text).lower())
    if any(t in DECISIVE for t in tokens):
        return True, True
    return len({t for t in tokens if t in SUPPORTING}) >= MIN_SUPPORTING, False


def config_dir():
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    if explicit:
        # expanduser: an unexpanded "~" would create a literal ~ directory
        # wherever the hook happens to be running from.
        return Path(explicit).expanduser()
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".claude"


def rulebooks(here):
    """Return (primary, overlays).

    Split on purpose: an overlay alone is not a rulebook. If the primary cannot
    be read, the session must fall back to the checklist rather than shipping a
    personal overlay with no rules around it, and must not be marked as served.

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
    primary = found[0] if found else None

    found.clear()
    seen.clear()
    add(os.environ.get("BERNDEUTSCH_IDIOLECT"))
    try:
        for overlay in sorted(config_dir().glob("projects/*/memory/berndeutsch-schrybwys.md")):
            add(overlay)
    except OSError:
        pass
    return primary, list(found)


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
- äu/eu becomes öi (Höi, tröime, nöi, Fröid)
- ds = article "das" (ds Modul); z = preposition "zu" (z Bärn, z tüe)
- l-vocalisation: aut, viu, wöu, Gäud, schnäu. But a single l between vowels
  stays: hole, Zahle, male. A double ll still vocalises: wölle gives wöue.
- sp/st in Anlaut stay sp/st (starch, Stei, verstecke); inside a word scht/schp
  (luschtig, Wäschpi, Poscht)
- no preterite, use the perfect (mir sy gange); pluperfect is a double perfect
- negation nid/nit, NEVER nöd, and no other Zurich or Basel forms
- itz (not jetz), louf (not Lauf), chlepfe (not chlöpfe), suber (not sufer)
- be CONSISTENT within one text, that is the golden rule"""


def build_context(here, first_time, served):
    out = [
        "<berndeutsch-schrybwys>",
        "The user's message appears to be Bärndütsch (Bernese German). If it is,",
        "reply in Bärndütsch using the house style below, NOT a generic Swiss",
        "German. If the message is actually German or English, ignore this block.",
        "",
    ]

    emitted = False
    if first_time:
        primary, overlays = rulebooks(here)
        if primary:
            try:
                body = primary.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = ""
            # An empty or unreadable rulebook is not a rulebook. Treating it as
            # one would mark the session served and leave the user with neither
            # the rules nor the checklist for the rest of it.
            if body.strip():
                out.append(body)
                emitted = True
        if emitted:
            for overlay in overlays:
                try:
                    extra = overlay.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if extra.strip():
                    out.append(extra)
            out.append("")
    if not emitted:
        # Also the path taken when no rulebook file was found or none could be
        # read. Claiming the full rulebook "was already loaded" would be a lie
        # in that case, so the checklist stands on its own wording.
        #
        # The bundled checklist is the schriftsprach-nah system. Emitting it to
        # someone who set BERNDEUTSCH_RULES precisely to replace that system
        # would hand them the rulebook they opted out of, on every prompt after
        # the first. Point at their own file instead.
        override = os.environ.get("BERNDEUTSCH_RULES")
        primary, _ = rulebooks(here)
        readable = False
        if primary:
            try:
                readable = bool(primary.read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                readable = False
        # Same test as the emit path. An existing but empty or unreadable file
        # would otherwise produce an injection that names a rulebook and
        # contains no rules.
        if override and readable:
            out.append(f"Follow the rulebook at {primary}, which is in force for")
            out.append("this user and replaces any default Bernese spelling system.")
        else:
            # Including the case where BERNDEUTSCH_RULES points at a path that
            # does not exist. Pointing the model at a file that is not there
            # would leave it with no rules whatsoever, which is worse than the
            # default checklist it opted out of.
            out.append(CHECKLIST)
        if served:
            # Only true when the full rulebook actually went out earlier. Since
            # round 5 a supporting-only match also lands here with first_time
            # False, and saying "loaded earlier" there would be a fresh lie.
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
        # Read bytes and decode explicitly: sys.stdin goes through the locale
        # wrapper, and under a non-UTF-8 locale every umlaut in the prompt would
        # arrive mangled or raise.
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return 0
    if "[hd]" in prompt.lower():
        return 0

    fire, certain = is_dialect(prompt)
    if not fire:
        return 0

    here = Path(__file__).resolve().parent
    marker = session_marker(payload.get("session_id"))
    served = bool(marker and marker.exists())
    first_time = certain and not served
    context, emitted = build_context(here, first_time, served)

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
