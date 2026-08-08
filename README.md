# sift

A provenance-preserving pipeline for plant reference data.

Sift assembles botanical reference data from open-licensed sources and keeps
every user-facing claim attached to the source it came from and a confidence in
it. Where a claim cannot be attributed, it is dropped and counted — never
guessed.

Status: **M4.1 — two-source nativity**. The pipeline runs end to end: `fetch`
builds a candidate pool, `resolve` stores its photos, and `promote-pack`
attaches a nativity claim — but only where USDA PLANTS and the state's
iNaturalist place checklist agree — and emits a manifest.

## Getting started

```bash
make install   # uv sync, including dev dependencies
make check     # install + lint + typecheck + test — what CI runs

uv run sift-pack fetch --domain plants --state MI
uv run sift-pack stats --state MI

uv run sift-pack resolve --state MI
uv run sift-pack stats --state MI --resolved

uv run sift-pack promote-pack --state MI
uv run sift-pack stats --state MI --manifest
```

`fetch` writes `work/candidates_MI.json` and caches every API response under
`cache/`, so a second run makes zero network calls and an interrupted run
resumes simply by being run again. Progress and drop accounting go to stderr.
A full state fetch is about 1200 requests and twenty minutes; only one may run
at a time, enforced by `work/.fetch.lock`.

`resolve` fetches each photo from the iNaturalist open-data bucket, resizes it
to 500px, strips every byte of metadata, and stores it under the SHA-256 of the
transcoded output. It journals per taxon, so a killed run resumes; a completed
run repeats for free.

`promote-pack` is the terminal step and the only code path in Sift that can
create a nativity claim. A taxon USDA PLANTS cannot resolve unambiguously is
dropped and written to `work/unmatched_MI.csv` with a reason — dropped and
unrecorded are different things.

Individual targets: `make lint`, `make format`, `make typecheck`, `make test`.
`make help` lists them.

## The pipeline

```
iNaturalist ──fetch──> CandidatePool ──resolve──> ResolvedPool ──promote──> Manifest
   API                  (work/)      + S3 bytes    (work/ +          ^         (packs/)
                                                    images/)         │
                                    USDA PLANTS (L48) ───────────────┤
                          iNaturalist place checklist (state) ───────┘
                                          claim only where they agree
```

Each stage adds exactly what it can source. A candidate photo has a URL; a
resolved photo has bytes, a hash and a size. Neither has a nativity claim, and
neither can be made to hold one — promotion is the only path by which one comes
to exist.

`CandidateTaxon` has no field capable of holding a nativity claim, under any
name. That is what makes promotion the only path by which one can exist, and
promotion requires a source by construction.

## Photo selection

Photos are what a learner actually studies, so they are sampled deliberately
rather than taken in whatever order the API returns them. Each taxon is queried
across four seasonal buckets — spring, early summer, late summer, autumn/winter
— and selection round-robins across them, so a plant is shown in flower *and* in
fruit *and* bare. No observer may supply more than two of a taxon's photos, and
no observation more than one. A taxon that cannot reach four photos under all of
those rules is dropped rather than padded.

Seasonal spread is the only thing selection optimises for. Within a bucket,
observations are taken in ascending ID order — an arbitrary but fixed tiebreak
that claims nothing, because iNaturalist exposes no per-observation quality
signal that survives inspection.

Each candidate records `months_represented` and `distinct_observers` — quality
signals M7 surfaces to the learner, and which the schema recomputes from the
photos rather than trusting.

There is deliberately no per-taxon identification-confidence signal. iNaturalist's
`num_identification_agreements` looks like one and is not: it counts agreements
with the observer's own ID, and research grade needs only two identifications, so
an unmistakable plant and a genuinely difficult sedge report the same number. See
`docs/decisions.md`, 2026-08-07.

## Repository layout

```
src/sift_pack/           The build half: fetches, filters, assembles packs
  manifest.py            Pack schema — the contract with the runtime half
  candidates.py          Intermediate schema — what iNaturalist alone can assert
  inat/                  The only code permitted to call the API
    client.py            Disk cache + rate limiting + response normalisation
    places.py            State -> place_id, resolved once into data/places.json
    deck.py              Which taxa are worth learning in a place
    photos.py            Month-stratified photo sampling and selection policy
    nativity.py          Per-place establishment status, and the place guard on it
    projections.py       What Sift reads from each response; the cache stores this
  fetch.py               Orchestrates the three stages into one pool
  resolved.py            Resolved schema — photos that exist as stored bytes
  resolve.py             Download -> transcode -> store, resumable per taxon
  download.py            Concurrent retrieval from the open-data bucket
  transcode.py           WebP at 500px, and the EXIF strip
  imagestore.py          Content-addressed store; cross-state dedupe lives here
  lock.py                One fetch at a time; Sift is a guest on a public API
  stats.py               What a pool actually contains
  domains/               The one axis on which plants/birds/pollinators differ
  usda/                  One of the two nativity sources
    client.py            Cached, projected access to the PLANTS services API
    reconcile.py         Three named matching tiers, and when to refuse
    index.py             Reconciles a whole pool against PLANTS
  nativity.py            The two-source rule: claim on agreement, refuse on conflict
  promote.py             Terminal step: resolved taxa + claims -> manifest
  cli.py                 `sift-pack fetch | resolve | promote-pack | stats | gc`
data/genus_demotions.json  Genera a card may only ask about at genus rank
data/state_exclusions.json Taxa withheld per state, each with a reason and source
images/                  Content-addressed WebP store (gitignored)
data/places.json         Committed state -> place_id table
scripts/record_fixtures.py  Re-record test fixtures (live; run by hand)
tests/                   Test suite; tests never touch the network
tests/fixtures/          Recorded API responses (see the README there)
docs/decisions.md        ADR-lite — dated decisions, append only
docs/sources.md          Every external source: licence, citation, limitations
STANDARDS.md             The contract all contributions must meet
```

## Before contributing

Read [STANDARDS.md](STANDARDS.md). The two rules that shape everything else:

- **Provenance** — a function producing a user-facing factual claim returns
  `(value, sources, confidence)`, never a bare value. `Axis1Result` is this made
  structural: it has no default for any field, and `sources` is typed so that an
  *empty* source list is a type error too. An unattributed claim does not
  typecheck, rather than being something a review has to catch.
- **No silent failures** — unknown data is dropped and counted, never guessed.
  A domain returning `None` means "cannot determine"; callers must drop the
  taxon and count it, and the schema gives them nowhere to put a guess.

## What a nativity label actually means

A card says **native** only when two independent sources said so: USDA PLANTS
for the lower 48, and the state's iNaturalist place checklist for the state
itself. Neither is trusted alone, because they fail in opposite directions.
PLANTS has no per-state data at all, so a Sonoran Desert native naturalised
around Detroit reads `L48 (N)` — that is how `Robinia pseudoacacia`, which
Michigan DNR lists as invasive, would otherwise read native. The checklist is
per-place but curator-maintained, and calls `Geranium robertianum` and
`Clinopodium vulgare` introduced where Michigan Flora considers both native.

Across the 300-taxon Michigan pool the two agree on 282 of the 285 they both
cover. The three they disagree on are all genuinely contested plants, and all
three are dropped rather than labelled — a flashcard is not the place to take a
side in a live botanical dispute. A taxon only one source can speak to still
gets a card, at `medium` rather than `high`.

Two further honesty constraints:

- A checklist value is used only when iNaturalist answered from the state
  itself. It will answer a per-place query from an ancestor place — North
  America, say — and the value alone looks identical, so anything inherited is
  refused outright.
- A checklist flag is not a curated nativity judgement. `Echinacea purpurea` is
  flagged native in Michigan and is presumed extirpated there; both sources
  agree and both are wrong. A short, capped, cited list at
  `data/state_exclusions.json` withholds those.

Every claim carries each source's own retrieval date, because neither source
publishes a version stamp. See `docs/decisions.md`, 2026-08-08, and
`docs/sources.md`.

## Licensing

Sift ingests only CC0, CC-BY and CC-BY-SA sources; NonCommercial material is
excluded to preserve commercial optionality. See `docs/decisions.md`.
Per-record licences and attribution travel with the data and must be honoured
by anything built on top of it.
