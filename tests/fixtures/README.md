# Recorded HTTP fixtures

Tests never touch the network (STANDARDS.md rule 6). Every response the code
would have fetched is recorded here and replayed from disk.

## Layout

`inat_cache/` is a real `InatClient` cache directory. Tests point a client at it
with `offline=True`, so they exercise the production code path — key derivation,
envelope parsing, response normalisation — with no HTTP library and no mock in
the loop. Anything a test asks for that was not recorded raises `CacheMissError`
naming the request, rather than a socket error.

Since M2.1 the client caches **projections** rather than raw bodies, so a
fixture is exactly what the parsers read and nothing else. The projection is
defined once, in `sift_pack.inat.projections`, and the cache key includes
`PROJECTION_VERSION` — so widening a projection means bumping the version and
re-recording, never silently reading an old shape.

```
tests/fixtures/inat_cache/
  .sift-cache-format.json   # format + projection version; its absence means legacy
  observations/<sha256-of-endpoint-params-and-projection-version>.json
  species_counts/...
  taxa_by_id/...
  places_autocomplete/...
  RECORDED_TAXON_IDS.json   # which taxa the deck stage selected when recorded
```

There are four `observations` entries per taxon, one per seasonal bucket. All
four must exist even when a bucket returned nothing: `select_photos` makes four
requests, and a missing entry is a cache miss rather than an empty bucket.

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

The recorder simply runs the real pipeline with its cache pointed here. It has
no projection logic of its own — that would be a second definition of "what Sift
reads", able to drift from the one the parsers use. Fixtures are produced by the
same code path that later replays them.

Which taxa get recorded is derived by running the real deck stage, not
hardcoded: a hardcoded list would drift the moment observation counts reorder
the deck, and every subsequent fixture lookup would miss.

Never hand-edit a fixture to make a test pass — a fixture that no longer matches
reality is worse than no fixture. If the upstream shape changed, change the code
and re-record. Truncating a recorded response to construct a specific condition
(too few observations, say) is fine in a test that says so, since the records
themselves stay unedited.

## Rules

- No credentials, API keys, or personal data in a committed fixture.
- Keep fixtures small; large binary assets (image bytes) belong in a test that
  generates them locally with Pillow instead.
- Re-record deliberately, in its own commit, so the diff shows what upstream
  changed.
