# sift

A provenance-preserving pipeline for plant reference data.

Sift assembles botanical reference data from open-licensed sources and keeps
every user-facing claim attached to the source it came from and a confidence in
it. Where a claim cannot be attributed, it is dropped and counted — never
guessed.

Status: **skeleton**. No feature code yet.

## Getting started

```bash
make install   # uv sync, including dev dependencies
make check     # install + lint + typecheck + test — what CI runs
```

Individual targets: `make lint`, `make format`, `make typecheck`, `make test`.
`make help` lists them.

## Repository layout

```
src/sift/            Package source
tests/               Test suite; tests never touch the network
tests/fixtures/      Recorded HTTP responses (see the README there)
docs/decisions.md    ADR-lite — dated decisions, append only
docs/sources.md      Every external source: licence, citation, limitations
STANDARDS.md         The contract all contributions must meet
```

## Before contributing

Read [STANDARDS.md](STANDARDS.md). The two rules that shape everything else:

- **Provenance** — a function producing a user-facing factual claim returns
  `(value, source, confidence)`, never a bare value.
- **No silent failures** — unknown data is dropped and counted, never guessed.

## Licensing

Sift ingests only CC0, CC-BY and CC-BY-SA sources; NonCommercial material is
excluded to preserve commercial optionality. See `docs/decisions.md`.
Per-record licences and attribution travel with the data and must be honoured
by anything built on top of it.
