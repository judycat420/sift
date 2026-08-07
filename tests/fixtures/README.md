# Recorded HTTP fixtures

Tests never touch the network (STANDARDS.md rule 6). Every response the code
would have fetched is recorded here and replayed from disk.

## Layout

`inat_cache/` is a real `InatClient` cache directory. Tests point a client at it
with `offline=True`, so they exercise the production code path — key derivation,
envelope parsing, response normalisation — with no HTTP library and no mock in
the loop. Anything a test asks for that was not recorded raises `CacheMissError`
naming the request, rather than a socket error.

```
tests/fixtures/inat_cache/
  observations/<sha256-of-endpoint-and-params>.json
  species_counts/...
  taxa_by_id/...
  places_autocomplete/...
  RECORDED_TAXON_IDS.json   # which taxa the deck stage selected when recorded
```

Filenames are hashes, but each envelope carries its own `endpoint` and `params`,
so the directory is auditable by reading it:

```json
{
  "endpoint": "observations",
  "params": {"taxon_id": 47911, "place_id": 29, "...": "..."},
  "recorded_by": "scripts/record_fixtures.py",
  "note": "projected to the fields Sift reads; values are verbatim",
  "response": {"total_results": 705, "results": ["..."]}
}
```

`respx` is not used here: it intercepts `httpx`, and the iNaturalist client is
`pyinaturalist`, which is built on `requests`. See `docs/decisions.md`,
2026-08-07.

## Recording fixtures

```bash
uv run python scripts/record_fixtures.py
```

It makes live requests (served from `cache/` when warm) and rewrites the whole
fixture set. Never run in CI.

Responses are **projected**, not trimmed by hand: `scripts/record_fixtures.py`
keeps exactly the fields the parsers read and drops the rest, so an observations
page falls from megabytes to kilobytes. No value is altered, reordered or
invented, and the projection functions are committed so the reduction is
auditable. Never hand-edit a fixture to make a test pass — a fixture that no
longer matches reality is worse than no fixture. If the upstream shape changed,
change the code and re-record.

The recorder derives which taxa to record by running the real deck stage over
the recorded `species_counts` response, so the fixture set is exactly what the
pipeline requests. A hardcoded list would drift the moment observation counts
reorder the deck.

## Rules

- No credentials, API keys, or personal data in a committed fixture.
- Keep fixtures small; large binary assets (image bytes) belong in a test that
  generates them locally with Pillow instead.
- Re-record deliberately, in its own commit, so the diff shows what upstream
  changed.
