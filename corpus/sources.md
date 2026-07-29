# Where to read real Bärndütsch

Rules give you correctness. Only reading gives you rhythm, idiom and word
choice. These are the places worth reading, with their licence, so you know what
you may copy and what you may only link to.

## Fetchable, licence-clean

**Alemannic Wikipedia — [Kategorie:Bärndütsch](https://als.wikipedia.org/wiki/Kategorie:B%C3%A4rnd%C3%BCtsch)**
CC BY-SA 4.0. `scripts/bd-corpus` fetches these and filters out the articles
that are written in a different Alemannic variant. The article
[Berndeutsch](https://als.wikipedia.org/wiki/Berndeutsch) is the single most
useful text here: it is about the dialect *and* written in it, and it is where
the grammar section of `rules/schrybwys.md` comes from.

Style note: these articles are mostly written in the **lautgetreu** system
(`Merkmau`, `gäute`, `Vokausystem`), not the schriftsprach-nah one this repo
follows. Read them for vocabulary, idiom and grammar, not for spelling.

**[berndeutsch.ch](https://www.berndeutsch.ch)** — the dictionary itself. Every
entry carries example sentences in real Bernese. `scripts/bdw` queries it one
word at a time.

## Read online, do not copy

These are in copyright. Read them, link them, quote a line with attribution, but
do not vendor them into a repository.

- **[Mani Matter](https://als.wikipedia.org/wiki/Mani_Matter)** (1936–1972) — the
  reference point for Bernese as a written, sung language. Lyrics are in
  copyright until 2043. Official site: [manimatter.ch](https://www.manimatter.ch).
- **Pedro Lenz** — contemporary Bernese prose, e.g. *Der Goalie bin ig*.
  [pedrolenz.ch](https://www.pedrolenz.ch)
- **Franz Hohler**, *Ds Totemügerli* (1967) — the most quoted piece of invented
  Bernese there is.
- **[blog.berndeutsch.ch](https://blog.berndeutsch.ch)** — longer articles in
  and about Bernese.
- **Bible in Bernese**, tr. Hans and Ruth Bietenhard — the largest body of
  carefully edited Bernese prose that exists.

## Public domain, but hard to use

- **Rudolf von Tavel** (1866–1934, PD in Switzerland since 2005). Scans exist,
  e.g. *Der Schtärn vo Buebebärg* (1907) on
  [archive.org](https://archive.org/details/bub_gb_BAtUAAAAYAAJ). The OCR is
  Fraktur and effectively unusable (`isch` comes out as `ifd)`). Worth knowing:
  that 1907 printing uses `Schtärn` and `gschtande`, i.e. **scht** in the
  Anlaut, which is the lautgetreu convention rather than the schriftsprach-nah
  one that later carried his name.
- **Simon Gfeller** (1868–1943, PD since 2014) — *Heimisbach*, *Ämmegrund*.

## A note on the sources

berndeutsch.ch is run as 100% volunteer work and has no API. `scripts/bdw`
therefore behaves like a courteous human reader: one request per result page, a
delay between requests, a page walk bounded at 15 pages per word, an honest
User-Agent that names this repository, and links back to every entry instead of
reproducing the dictionary. Please keep it that way,
and if you find the dictionary useful,
[add the words it is missing](https://www.berndeutsch.ch/pages/wordaddinfo).
