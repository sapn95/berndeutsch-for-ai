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
chömet chöit gö dihr
öppis öpper öppe mängisch itz sött söu söue wöu gäud niemer
chli chunt chunnt chume chumme chöme chömme chöi göh göi
müesse müesset chönne
nüme nümme müed gärn üs üsi üse
zyt schrybe schrybt blybe blybt zäme wyt myni gsäh wotsch
gseht gschribe ggange verstande chönnt chönnte tuet gnoh gläse
verschobe gschaffet gschickt gwartet gluegt gsprunge dänkt gwüsst
machemer gömer simer gmacht gseit gwüss mitenand sälber eifach gäbig
öbe äbe grüessech vilmal gäng äuä äuwä nüt nüüt geits gaht gahts goht gohts
meiteli gieu hegu
aues viu viumau viumou schnäu chüngu wüescht gnue luschtig
ahnig louf louft chlepfe chlepft poschte poschtet guete
lueg luegit schryb schrybit säg sägit öb mues muess churz
mäntig zischtig mittwuch donnschtig fritig samschtig sunntig
morn übermorn geschter vorgeschter aabe morge namittag nomittag
liebschte beschte schönschte gröschte schnäuschte deheime
wäri wärsch wärit wetti wettsch wettit giengsch giengit
chiem chiemsch chiemti hätti hättsch hättit täti tätsch jä
chönnti chönntsch müessti müesstisch chunnti
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
# gange was DECISIVE and is now merely supporting: "im Gange" and "am Gange"
# are ordinary German, tokens are lowercased before matching, and a single
# German sentence about a migration in progress therefore fired the full
# rulebook AND consumed the session's one full injection. The doubled ggange,
# which is what this repository's own rulebook actually prescribes for the
# participle, has no German reading and takes its place in the decisive tier.
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
# Deliberately absent from both tiers: halt, grad, wäge, sowieso, säge,
# mer, hoi, and bare git. (merci WAS in this list and is now supporting and
# weak; the sentence claiming otherwise outlived the change by two rounds.)
# They are ordinary German or English words or, in the case of mer, fall out of
# a URL path once tokens are split on non-letters. Any two of them reached the
# bar on prompts with no dialect at all, and git reached it twice in one shell
# command.
SUPPORTING = frozenset("""
nid nit gsi gha kei chum witt geng gits hämmer gäu aut modi ching gange
het mys gly heit
hei cha wott gah luege scho öi nöime hüt dänk söll nei guet merci geit
wär hätt tät wett andersch gieng
znüni zmorge zobe zvieri meitschi bueb
""".split())

# A subset of SUPPORTING, and the reason two markers were not enough.
#
# Requiring two DISTINCT supporting words stops one ambiguous token from
# deciding a prompt, but it does nothing when a single sentence naturally
# carries two of them, and a technical prompt does exactly that. "Update the
# GHA workflow and the DynamoDB GSI projection" is ordinary English and fired
# the gate, as did "the KEI report and the Lustre NID mapping", the German "Es
# gibt zwei Modi, und die Migration ist noch im Gange", and the bare URL path
# "GET /api/het/modi returns 500".
#
# What those have in common is not that they are acronyms. It is that each word
# is ALSO an ordinary word, an acronym or an identifier fragment in the register
# this hook runs in, so they cluster: one such coincidence in a sentence makes a
# second one likely rather than unlikely. An earlier version of this set covered
# only the acronyms and the German and URL cases walked straight through it.
#
# Two of these together therefore decide nothing. Three do, because a sentence
# with three separate collisions in it is no longer a coincidence.
#
# A WEAK marker also has to be written in lower case to count at all, which is
# the evidence the earlier version threw away by lowercasing before matching.
# The collision is nearly always with an ACRONYM or with a GERMAN NOUN, and both
# of those are capitalised while the Bernese word is not: "the Lustre NID
# mapping" and "im Gange" and "zwei Modi" and "Prime Minister Modi" are all
# capitalised, and Bernese `nid`, `gange`, `modi` in running text are not.
#
# het and nid are deliberately NOT here. They are the auxiliary and the
# negation, which is the commonest two-marker shape in the language, and
# requiring a third silenced "Das het nid klappt" and "Er het nid welle". Their
# own collisions are with a URL path segment and an uppercase identifier, and
# GLUE and the lower-case rule cover both without costing the language anything.
WEAK = frozenset("""
nit gsi gha kei aut mys gly het geit hei
modi ching geng chum gits cha gange witt hämmer gäu heit
wär hätt tät wett gieng merci guet
""".split())

# One decisive marker fires. Otherwise two DISTINCT supporting markers are
# required, so neither a single ambiguous token nor one token repeated can
# trigger on an English or German prompt. If every one of them is WEAK, three
# are required instead.
MIN_SUPPORTING = 2
MIN_WEAK_ONLY = 3

# The rulebook's own rules, applied to the marker lists instead of being
# hand-copied into them. Two whole categories of everyday Bernese were silently
# undetected because the lists carried one spelling of a word the rules say has
# two, and one shape of a verb the language uses constantly.

# nd -> ng. rules/schrybwys.md prescribes the velarisation (Ching, angers,
# mitenang, Hang), and both tiers carried only the nd spelling, so a writer
# following this repository's own rulebook went undetected: "I ha das nid
# verstange", "Sali mitenang", "Fingsch du das o". Derived rather than listed,
# because a list needs every twin remembered and this cannot forget one.
# The velarisation is a rule about a CLUSTER inside a word, not about the two
# letters wherever they meet. In "bärndütsch" the n and the d are on either side
# of a compound seam (bärn + dütsch), and applying the rule there manufactured
# bärngütsch, berngütsch, baernguetsch and bärngütschi, four words nobody has
# ever written. Harmless, but a rule that generates markers has to be right
# about what it generates, because there is no list to read afterwards.
SEAMS = ("dütsch", "duetsch")


def velarised(markers):
    return frozenset(m.replace("nd", "ng") for m in markers
                     if "nd" in m and not any(seam in m for seam in SEAMS))


# Separable prefixes. The perfect is the only past tense the language has, and
# a participle takes its prefix in front of the ge-: zuegmacht, ufgschribe,
# dürgläse, usegange. Listing gmacht and gschribe therefore missed the perfect
# in exactly the sentences where it does the work, seven of eight in the
# labelled set. Matching a listed participle as a SUFFIX behind a known prefix
# covers the paradigm without enumerating the cross product.
PREFIXES = tuple("""
uf zue ab dür use ine ache uehe abe vor no mit y a i um
""".split())
# Only participles, not every marker: "ufisch" is not a word, and allowing any
# marker to be suffixed would let a prefix manufacture matches.
PARTICIPLES = frozenset("""
gmacht gschribe gläse ggange gseit gnoh gseh gsy gsii boue gschaffet gschickt
gnu gwüsst gfunde gha gsprunge gschlafe gläbt gwartet gluegt choufft
verschobe verstande vergässe
""".split())


# The cross product is 468 tokens and every one of them is DECISIVE, so a
# single wrong combination injects the whole rulebook. Checked against a
# 234k-word dictionary, exactly one collided: "agha", which is an English word
# (an Ottoman title) generated by the one-letter prefix a- on gha. The
# one-letter prefixes are real (y- for "ein", a- for "an") but they produce
# four- and five-letter tokens, which is where both the collision and all the
# nonsense live. A minimum length keeps ygschribe and umboue and drops agha,
# igsy, agnu and the rest.
MIN_PREFIXED = 6


def prefixed(token):
    """True when the token is a listed participle behind a separable prefix."""
    if len(token) < MIN_PREFIXED:
        return False
    for prefix in PREFIXES:
        if token.startswith(prefix) and token[len(prefix):] in PARTICIPLES:
            return True
    return False


# The derived twins join the tier their source came from, so a velarised
# decisive marker stays decisive and a velarised supporting one stays
# supporting. Computed at import: the lists above stay readable as the thing a
# human maintains, and the rules stay in one place.
DECISIVE |= velarised(DECISIVE)
SUPPORTING |= velarised(SUPPORTING)

# Markers that must be written in lower case to count, or capitalised only at
# the start of a sentence. Every WEAK marker qualifies by construction, and a
# few DECISIVE ones do too: ITZ is a German IT department, and one occurrence
# of a decisive marker injects the whole rulebook, so an acronym reading is
# expensive there rather than merely wrong.
CASED = WEAK | frozenset("itz".split())
# Splitting one list into two invites a typo that leaves an entry unreachable,
# which is the bug scripts/bd-corpus shipped for real. The check lives in
# scripts/selftest.py rather than in an assert here: a hook that raises on a
# prompt is worse for the user than a marker that quietly does nothing, and
# assertions are stripped under python3 -O anyway.
TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Characters that split a token but never split a word in prose, so a letter run
# touching one came out of an identifier, a path or an encoded blob rather than
# out of a sentence. The slash and the plus are here because a URL path segment
# ("GET /api/het/modi") and the base64 alphabet produce exactly the same short
# letter runs as a pod-name hash does. Prose uses "and/or", which costs nothing:
# neither half is a marker.
GLUE = frozenset("0123456789_/+")


def _is_word_char(ch):
    """True for exactly the characters TOKEN_RE groups into a token.

    isalpha() is not the same class as `[^\\W\\d_]`: the underscore is excluded
    by both, but isalpha() also rejects a combining mark, and after NFC there
    can still be one (there is no precomposed form for every sequence). Testing
    the real pattern keeps the trim and the tokeniser from disagreeing.
    """
    return TOKEN_RE.fullmatch(ch) is not None


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
    # An apostrophe inside a word marks an elision, and the elided spelling is
    # what the marker lists carry: geit's is geits, hesch's is heschs. Removing
    # it joins the halves instead of splitting the word in two. Nothing else
    # depends on it: English "don't" becomes "dont", which is not a marker.
    text = re.sub(r"(?<=\w)['\u2019](?=\w)", "", text)
    if len(text) <= SCAN_HEAD + SCAN_TAIL:
        return text
    # Strip the partial token at each cut with the same character class the
    # tokeniser uses, rather than hunting for a space. A minified file or a
    # base64 blob can contain no space in the whole window, and then a
    # space-based trim leaves the fragment in place.
    #
    # Scanned character by character rather than with an anchored `+$` regex.
    # That pattern is quadratic on the trailing side: at every start offset the
    # engine matches a letter run and then backtracks it away against the
    # anchor, so a 3000-character paste with no letter at the very end costs
    # millions of steps. This hook runs on a timeout, on every prompt, and a
    # pasted blob is the normal case rather than the adversarial one.
    head, tail = text[:SCAN_HEAD], text[-SCAN_TAIL:]
    cut = len(head)
    while cut and _is_word_char(head[cut - 1]):
        cut -= 1
    start = 0
    while start < len(tail) and _is_word_char(tail[start]):
        start += 1
    return head[:cut] + "\n" + tail[start:]


def word_tokens(text):
    """The letter runs that are words, dropping the ones that are identifier bits.

    The tokeniser splits on digits and on the underscore, so any alphanumeric
    blob is shredded into short letter runs, and short letter runs are what the
    decisive tier is made of. A Kubernetes pod name does this constantly:
    `api-gateway-7d4b9c8f5-itz9q` yields `itz`, which alone injects the whole
    rulebook and spends the session's single full injection, so the genuinely
    Bernese question that follows gets only the checklist. Measured over random
    pastes this was the dominant remaining false positive: a base64 blob fired
    4.5% of the time, a PEM certificate 1.35%, a `kubectl get pods` listing
    0.77%.

    A GLUE character never separates two letters inside a word of running
    prose, in any of the languages this has to survive. It only happens inside
    an identifier, a hash, a path or an encoded blob. So a letter run touching
    one on either side is not a word and does not vote. Ordinary punctuation is
    left alone, because prose does use it: "Wo-n-i" must still tokenise.

    Case is preserved. The caller needs it: an all-caps NID is an acronym and a
    lower-case nid is the Bernese negation, and lowercasing here threw away the
    only evidence that tells them apart.
    """
    return [m.group() for m in word_matches(text)]


# What can precede a word and still leave it sentence-initial. A capital there
# is the orthography, not a signal, so a WEAK marker written "Chum" at the start
# of a Bernese sentence has to keep counting.
#
# Openers as well as closers. The first version held only closing punctuation,
# which is the wrong half: a sentence does not begin after a closing quote, it
# begins after an OPENING one. «Chum mer wei das luege.» went silent, and so did
# the same sentence as a markdown bullet or a blockquote, which is how half the
# prompts in a chat client are actually written.
SENTENCE_END = frozenset(".!?:;\"'»«„“”‹›()[]…\n\r-*>•\u2013\u2014")


def word_matches(text):
    """word_tokens, but as match objects, so the caller can see the position."""
    return [m for m in TOKEN_RE.finditer(text)
            if not (m.start() and text[m.start() - 1] in GLUE)
            and not (m.end() < len(text) and text[m.end()] in GLUE)]


def sentence_initial(text, start):
    """True when only whitespace and sentence-ending punctuation precede."""
    i = start - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return i < 0 or text[i] in SENTENCE_END


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
    window = scan_window(text)
    matches = word_matches(window)

    def counts(m):
        """False when a case-sensitive marker was not written like the word.

        Its collision is with an acronym or a German noun, and both of those
        are capitalised where the Bernese word is not: NID, GSI, GHA, MYS,
        Gly, ITZ, "im Gange", "zwei Modi", "Minister Modi". A capital at the
        start of a sentence is orthography rather than evidence, so that one
        still counts.
        """
        token = m.group()
        if token.lower() not in CASED or token == token.lower():
            return True
        return (token == token.capitalize()
                and sentence_initial(window, m.start()))

    if any((m.group().lower() in DECISIVE or prefixed(m.group().lower()))
           and counts(m) for m in matches):
        return True, True
    matched = {m.group().lower() for m in matches
               if m.group().lower() in SUPPORTING and counts(m)}
    # Two distinct markers, or three if every one of them also reads as
    # ordinary non-Bernese text. Both halves are needed: without the count, one
    # token decides a prompt; without the weak rule, "the GHA workflow and the
    # DynamoDB GSI" is two.
    need = MIN_WEAK_ONLY if matched <= WEAK else MIN_SUPPORTING
    return len(matched) >= need, False


def expand(path):
    """Path(path).expanduser(), without letting it take the hook down.

    expanduser() does not raise OSError when it cannot resolve a "~user"
    prefix, it raises RuntimeError, which is not caught by any guard written
    around filesystem errors. A single environment variable set to "~someone"
    therefore escaped every try block and killed the hook for the whole
    session, silently, on a machine where that user does not exist. The
    unexpanded path is a better answer than no hook at all.
    """
    try:
        return Path(path).expanduser()
    except (OSError, RuntimeError, ValueError):
        return Path(path)


def unresolved(path):
    """True when a leading ~ survived expansion, so the path is not a path.

    expand() falls back to the literal string when "~someone" cannot be
    resolved, which keeps the hook alive but leaves a name beginning with a
    tilde. Handing that to mkdir(parents=True) does not fail: it creates a
    directory literally called "~someone" in whatever directory the hook
    happens to have been started from, which for a prompt hook is the user's
    project. Silently, on every prompt.
    """
    return str(path).startswith("~")


def config_dir():
    """Where session state lives, or None if there is nowhere sensible.

    None rather than a guess. Every caller already has to cope with the state
    directory being unusable, because it can be read-only or full, and a
    littered project directory is a worse failure than losing the once-per-
    session budget for one session.
    """
    explicit = os.environ.get("CLAUDE_CONFIG_DIR")
    if explicit:
        candidate = expand(explicit)
        return None if unresolved(candidate) else candidate
    home = os.environ.get("HOME")
    if not home:
        try:
            home = os.path.expanduser("~")
        except (OSError, RuntimeError, ValueError):
            return None
    if not home or unresolved(home):
        return None
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
        p = expand(path)
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
        cfg = config_dir()
        for overlay in sorted(cfg.glob("projects/*/memory/berndeutsch-schrybwys.md")
                              if cfg else []):
            add(overlay)
    except OSError:
        pass
    return primary, list(found)


def lookup_tool(here):
    cfg = config_dir()
    places = [here.parent / "scripts" / "bdw"]
    if cfg:
        places.append(cfg / "scripts" / "bdw")
    for candidate in places:
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
- no preterite, use the perfect (mir sy ggange); pluperfect is a double perfect
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
    cfg = config_dir()
    if cfg is None:
        # Nowhere sensible to keep state. The turn still gets its rules; it just
        # cannot remember that it did, which costs the once-per-session budget
        # and nothing else.
        return None
    state_dir = cfg / "cache" / "berndeutsch-gate"
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
