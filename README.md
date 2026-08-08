# sift

A provenance-preserving pipeline for plant reference data.

Sift assembles botanical reference data from open-licensed sources and keeps
every user-facing claim attached to the source it came from and a confidence in
it. Where a claim cannot be attributed, it is dropped and counted — never
guessed.

Status: **M3 — image resolution**. `fetch` produces candidate pools sampled
across the seasons; `resolve` downloads, transcodes and content-addresses their
photos; `build` still cannot produce a manifest and says so rather than guessing.

## Getting started

```bash
make install   # uv sync, including dev dependencies
make check     # install + lint + typecheck + test — what CI runs

uv run sift-pack fetch --domain plants --state MI
uv run sift-pack stats --state MI

uv run sift-pack resolve --state MI
uv run sift-pack stats --state MI --resolved
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

`build` exits 4: promotion needs a nativity claim with a source — USDA PLANTS,
M4. It refuses to emit an empty manifest, because that would claim promotion ran
and rejected everything.

Individual targets: `make lint`, `make format`, `make typecheck`, `make test`.
`make help` lists them.

## The pipeline

```
iNaturalist ──fetch──> CandidatePool ──resolve──> ResolvedPool ──promote──> Manifest
   API                  (work/)      + S3 bytes    (work/ +        (M4,        (pack)
                                                    images/)      USDA)
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
  cli.py                 `sift-pack fetch | resolve | stats | places | gc | build`
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
  `(value, source, confidence)`, never a bare value. `Axis1Result` is this made
  structural: it has no default for any field, so an unattributed claim is a
  type error rather than something a review has to catch.
- **No silent failures** — unknown data is dropped and counted, never guessed.
  A domain returning `None` means "cannot determine"; callers must drop the
  taxon and count it, and the schema gives them nowhere to put a guess.

## Licensing

Sift ingests only CC0, CC-BY and CC-BY-SA sources; NonCommercial material is
excluded to preserve commercial optionality. See `docs/decisions.md`.
Per-record licences and attribution travel with the data and must be honoured
by anything built on top of it.
