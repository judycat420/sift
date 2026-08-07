# sift

A provenance-preserving pipeline for plant reference data.

Sift assembles botanical reference data from open-licensed sources and keeps
every user-facing claim attached to the source it came from and a confidence in
it. Where a claim cannot be attributed, it is dropped and counted — never
guessed.

Status: **M2 — iNaturalist ingest**. `fetch` produces real candidate pools;
`build` cannot yet produce a manifest and says so rather than guessing.

## Getting started

```bash
make install   # uv sync, including dev dependencies
make check     # install + lint + typecheck + test — what CI runs

uv run sift-pack fetch --domain plants --state MI --limit 250
uv run sift-pack stats --state MI
```

`fetch` writes `work/candidates_MI.json` and caches every API response under
`cache/`, so a second run makes zero network calls and an interrupted run
resumes simply by being run again. Progress and drop accounting go to stderr.

`build` exits 4: promotion needs a nativity claim with a source (USDA PLANTS,
M3) and image digests from the open-data bucket. It refuses to emit an empty
manifest, because that would claim promotion ran and rejected everything.

Individual targets: `make lint`, `make format`, `make typecheck`, `make test`.
`make help` lists them.

## The pipeline

```
iNaturalist ──fetch──> CandidatePool ──promote──> Manifest ──> runtime
                       (work/)          (M3)      (pack)
```

`CandidateTaxon` has no field capable of holding a nativity claim, under any
name. That is what makes promotion the only path by which one can exist, and
promotion requires a source by construction.

## Repository layout

```
src/sift_pack/           The build half: fetches, filters, assembles packs
  manifest.py            Pack schema — the contract with the runtime half
  candidates.py          Intermediate schema — what iNaturalist alone can assert
  inat/                  The only code permitted to call the API
    client.py            Disk cache + rate limiting + response normalisation
    places.py            State -> place_id, resolved once into data/places.json
    deck.py              Which taxa are worth learning in a place
    photos.py            Licence-cleared photos, one per observation
  fetch.py               Orchestrates the three stages into one pool
  stats.py               What a pool actually contains
  domains/               The one axis on which plants/birds/pollinators differ
  cli.py                 `sift-pack fetch | stats | places | build`
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
