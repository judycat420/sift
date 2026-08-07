# sift

A provenance-preserving pipeline for plant reference data.

Sift assembles botanical reference data from open-licensed sources and keeps
every user-facing claim attached to the source it came from and a confidence in
it. Where a claim cannot be attributed, it is dropped and counted — never
guessed.

Status: **M1 — schema and domain seam**. No network calls and no real data yet;
the plants domain resolves nothing, so a build correctly emits an empty pack.

## Getting started

```bash
make install   # uv sync, including dev dependencies
make check     # install + lint + typecheck + test — what CI runs

uv run sift-pack build --domain plants --state MI --limit 3
```

The manifest goes to stdout; the drop accounting goes to stderr, so the JSON
stays pipeable. Today every candidate is dropped for `axis1_undetermined` —
nothing is wired to USDA PLANTS until M3, and a pack with no claims is the
correct output for a build that resolved none.

Individual targets: `make lint`, `make format`, `make typecheck`, `make test`.
`make help` lists them.

## Repository layout

```
src/sift_pack/           The build half: fetches, filters, assembles packs
  manifest.py            Pack schema — the contract with the runtime half
  domains/               The one axis on which plants/birds/pollinators differ
  cli.py                 `sift-pack`, and the stage that enforces the drop rule
tests/                   Test suite; tests never touch the network
tests/fixtures/          Recorded HTTP responses (see the README there)
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
