# berndeutsch-for-ai

**Bärndütsch for AI.**

A rulebook that makes any AI write real Bernese German, instead of a generic
Swiss German that drifts a little further toward Zurich every session.

Write to an AI in Bärndütsch and it will answer in something dialect-shaped.
Over a long conversation that something decays: `nöd` creeps in for `nid`, `ä`
inflates everywhere (`Gipfäli`), and words get invented on the spot because they
sound plausible. The cause is mundane. Whatever spelling rules you gave the model
live in a memory or a preferences file that is only sometimes in context. When
they are absent, the model falls back on its generic Swiss German prior.

**The rulebook works with any AI.** `rules/schrybwys.md` is a plain markdown file
with no tool-specific syntax, and `rules/schrybwys-compact.md` holds the same
rules in a 1776-character block for instruction boxes that impose a limit. Drop
either into a system prompt, custom instructions, `AGENTS.md`,
`.github/copilot-instructions.md` or `.cursor/rules/` and you are done.

**For Claude Code there is also a hook**, which removes the "sometimes"
entirely. `UserPromptSubmit` checks each prompt for Bernese markers and, when it
finds them, injects the rules into that turn. Deterministically, every time, and
silently on prompts that are not in dialect.

```
you:     hesch mer chönne luege wies mitem Modul steit?
hook:    [detects Bärndütsch] -> injects rules/schrybwys.md
claude:  answers in Bärndütsch, with nid instead of nöd
```

## What is in the box

| | |
|---|---|
| `rules/schrybwys.md` | The rulebook. The codified *schriftsprach-nah* system after Marti and Bietenhard: vowels, consonants, `ds`/`z`, grammar, and what separates Bernese from its neighbours. Tool-agnostic. |
| `rules/schrybwys-compact.md` | The same rules in 1776 characters, with attribution and licence inside the block. |
| `hooks/berndeutsch_gate.py` | The detector and injector, for Claude Code. |
| `scripts/bdw` | Dictionary lookup against berndeutsch.ch. Answers the question the model cannot answer honestly by itself: is this actually a word? |
| `scripts/bd-corpus` | Fetches a small corpus of genuinely Bernese text, for feel rather than rules. |
| `corpus/sources.md` | Where to read real Bernese, with licences. |
| `NOTICE` | Who wrote what this repository summarises, and under which licence. |

## Use it with any AI

The rulebook is the product; the hook is one delivery mechanism. Nothing in
`rules/` is Claude-specific.

| Where | What to do |
|---|---|
| **ChatGPT** | Custom instructions, or a Project's instructions. Paste the block from `rules/schrybwys-compact.md`, i.e. everything below the `---`. |
| **Any system prompt / API** | Paste `rules/schrybwys.md` verbatim. |
| **GitHub Copilot** | `.github/copilot-instructions.md` |
| **Cursor / Windsurf** | `.cursor/rules/berndeutsch.md` |
| **Codex, and anything reading `AGENTS.md`** | Append it to `AGENTS.md`. |
| **Claude, without the hook** | `~/.claude/CLAUDE.md`, or `/memory`. |
| **Claude Code, with the hook** | See below. Only this one is conditional and automatic. |

The difference the hook makes is *when* the rules are present. Pasted
instructions sit in context on every unrelated turn, competing with everything
else in the prompt. The hook injects them only on the turns that are in dialect.

## Install the Claude Code hook

**As a plugin**

```
/plugin marketplace add sapn95/berndeutsch-for-ai
/plugin install berndeutsch-for-ai
```

**By hand**, which is the path this repo was developed and tested on:

```bash
git clone https://github.com/sapn95/berndeutsch-for-ai.git
cd berndeutsch-for-ai && ./scripts/install.py
```

`install.py` symlinks the hook and `bdw` into your Claude config directory,
self-tests both the firing and the silent case, prints the settings block to
merge, and changes nothing else. It never writes `settings.json` for you: that
file is yours and usually already has hooks in it.

The block it prints uses the exec form, so the hook is spawned directly and no
shell re-interprets the path:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3",
            "args": ["/Users/you/.claude/hooks/berndeutsch_gate.py"],
            "timeout": 10,
            "statusMessage": "Bärndütsch-Schrybwys lade…"
          }
        ]
      }
    ]
  }
}
```

Then open `/hooks` once in Claude Code, or restart it, so the new config is read.

The only requirement is `python3`. The hook and both scripts are standard
library only, with no third-party packages and no shell.

## Using it

Nothing to do. Write in dialect and the rules are there.

Three controls exist when you want them:

- **`[hd]`** anywhere in a prompt suppresses the hook for that turn.
- **`BERNDEUTSCH_RULES`** points at your own rulebook and *replaces* the bundled
  one. Use it if you write the lautgetreu system after Dieth, where handing the
  model both rulebooks would be worse than handing it neither.
- **`BERNDEUTSCH_IDIOLECT`** points at a personal overlay, which is appended
  after whichever rulebook is in force. This is where "I write `dr`, not `der`"
  belongs.

The hook also picks up an overlay automatically from any file named
`berndeutsch-schrybwys.md` under `<claude-config>/projects/*/memory/`, which is
where Claude Code's own memory lives. If you do not want that, name the file
something else.

Look a word up before you use it:

```
$ ~/.claude/scripts/bdw suber
EXACT    suber  [Adj./Adv.]
      Schreibweisen: sufer (alt)
      1. sauber, rein, z. B. "suberi Chleider", 2. ei...
      https://www.berndeutsch.ch/words/44197
verwandt suber u glatt
      klar, tatsächlich.
      https://www.berndeutsch.ch/words/14701

$ ~/.claude/scripts/bdw -n 0 nöd; echo $?
KEIN EXAKTER EINTRAG für «nöd» (1 Seite(n) durchsucht).
        Nicht als Stichwort oder Schreibvariante geführt. Ein hochdeutsches
        Lehnwort ist ehrlicher als eine erfundene Dialektform.
1
```

`install.py` symlinks `bdw` into your Claude config directory rather than onto
`PATH`, so either use the full path as above or add that directory to `PATH`
yourself. `-n 0` suppresses the related hits; without it both commands also list the
entries that merely mention the word. Exit 0 means an exact entry was found,
1 means the result set was searched to the end and there is none, and 2 means
the answer is unknown rather than negative: a transport failure, or a search
that hit the page cap.

Two things make that answer trustworthy. The site's search also matches the
German glosses, so querying `jetz` returns every entry whose translation
contains "jetzt" and none of them is the word itself; `bdw` reports `EXACT`
only for a real headword or a listed spelling variant. And results are
paginated ten to a page, with `gäng` sitting on page two of its own query, so
`bdw` walks the pages instead of judging a word from the first ten hits. When
the page cap stops the walk early it says so rather than claiming the word does
not exist.

## How the detection works

Marker matching over the first and last 3000 characters of the prompt, so that
a large paste neither hides the question nor costs anything to scan. A 1.4 MB
paste takes 0.15 s.

Two tiers, and both of them decide something.

**Decisive markers** cannot plausibly appear in English or German running text.
They are the second-person `-sch` verb forms (`chasch`, `bisch`, `weisch`,
`machsch`, `wohnsch`), plus everyday words that carry the dialect without a
verb: `isch`, `gsy`, `öppis`, `chli`, `znüni`, `Meitschi`, and the l-vocalised
spellings this repo's own rulebook prescribes (`aues`, `viu`, `schnäu`).
One is enough to fire. The verb forms alone were not enough: an imperative, a
first-person statement or a bare greeting contains none of them.

**Supporting markers** are genuinely Bernese but each collides with something
else: `gsi` is a DynamoDB Global Secondary Index, `nit` is the English noun in
"nit-picking", `chum` is an English word, `kei` is Dutch, `nid` is French for
nest and an HPC network identifier, `gäu` is a German toponym (das Gäu, Bezirk
Gäu SO), `aut` is "application under test". None of them decides anything alone. Two
*different* ones are required, so neither a lone ambiguous token nor the same
token twice ("the Lustre NID and the nid mapping") can fire.

Ordinary German words like `halt`, `grad`, `wäge` and `sowieso` are in neither
tier. An earlier version had them, and `Das ist halt so, das dauert grad noch
ein bisschen` fired the hook on a sentence with no dialect in it at all.

A false positive is harmless by construction: the injected text says *if* this
message is Bärndütsch, use these rules, so an English answer stays English.

It is also cheap by construction. The **full rulebook is sent only on a
decisive marker**. A match carried by supporting markers alone gets the short
checklist and does not consume the session's one full injection, so a residual
collision costs 1.4 KB rather than 9 KB and the next genuinely Bernese prompt
in that session still gets the whole rulebook. That cap is deliberate: no
word list is ever going to be perfect, so the design limits what being wrong
can cost instead of pretending the list is finished.

After the full rulebook has gone in once, later dialect prompts in the same
session get the short checklist, since the first injection is still in the
transcript.

## Deliberately not solved

Bernese has no binding orthography, and this repo does not pretend otherwise.
It picks the schriftsprach-nah system, states so, and applies it consistently.
The equally valid lautgetreu system after Dieth is documented in
[Hans Jürg Zingg's recommendations](https://www.berndeutsch.ch/doc/berndeutsch-schreiben-aussprach-nah-v1.pdf);
point `BERNDEUTSCH_RULES` at your own file if that is your system.

> Di wichtigschti Regu isch nid, weles System du nimmsch. Sondern das du bim
> glyche blybsch.

## Credit where it is owed

This repo is a thin wrapper around other people's work. `NOTICE` records
precisely which part comes from whom and under which licence; the short version:

- **[berndeutsch.ch](https://www.berndeutsch.ch)**, the free Bernese dictionary
  online since 2000, run as **100% volunteer work** by Stephan Burkard. It has
  no API and asks nothing of anyone. `bdw` queries it one page at a time, with a
  delay, an honest User-Agent, a bounded page walk, and a link back to every
  entry rather than a copy of it. If this tool is useful to you, the dictionary
  is the reason. Go
  [add the words it is missing](https://www.berndeutsch.ch/pages/wordaddinfo).
- **Ursula Pinheiro-Weber**, *Bärndütsch schrybe: schriftsprach-nach* (2021,
  CC BY-ND 4.0), the detailed public summary of the system.
  [Read the original](https://www.berndeutsch.ch/doc/berndeutsch-schreiben-schriftsprach-nah-v1.pdf),
  it is five pages and better than any summary of it. Neither that document is
  redistributed here, and `NOTICE` states plainly which illustrative words this
  rulebook shares with it and why.
- **Hans Jürg Zingg** for the lautgetreu counterpart.
- **Werner Marti**, *Bärndütschi Schrybwys* (1978), and **Ruth Bietenhard**,
  *Berndeutsches Wörterbuch* (10th ed. 2017), whose codification everything here
  ultimately rests on.
- The **Alemannic Wikipedia** authors (CC BY-SA 4.0) for the grammar section and
  the corpus.

## Licence

MIT for the code in `hooks/`, `scripts/` and the packaging files.

**`rules/` is CC BY-SA 4.0** (see `rules/LICENSE`). That is deliberate: this
README tells you to paste those files into a system prompt or an instructions
file, which is redistribution, so they need a licence that permits it. Attribute
as "Bärndütschi Schrybwys, github.com/sapn95/berndeutsch-for-ai, CC BY-SA 4.0".

`corpus/` describes material belonging to others. `NOTICE` records the
provenance of everything and is the file to read before vendoring any of it.
