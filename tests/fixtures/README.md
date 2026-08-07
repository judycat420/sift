# Recorded HTTP fixtures

Tests never touch the network (STANDARDS.md rule 6). Every outbound HTTP call is
mocked with [`respx`](https://lundberg.github.io/respx/) and replayed from a
fixture recorded here.

## Layout

```
tests/fixtures/
  inaturalist/          # one directory per external source
    taxa_47126.json     # <endpoint>_<key>.json
    taxa_47126.meta.json
```

## Recording a new fixture

1. Fetch the real response **once**, by hand, outside the test suite:

   ```bash
   curl -s 'https://api.inaturalist.org/v1/taxa/47126' \
     | python -m json.tool > tests/fixtures/inaturalist/taxa_47126.json
   ```

2. Write a sibling `*.meta.json` recording where it came from, so a future
   reader can tell whether the fixture has gone stale:

   ```json
   {
     "url": "https://api.inaturalist.org/v1/taxa/47126",
     "recorded_at": "2026-08-05",
     "source": "iNaturalist API v1",
     "notes": "Plantae root taxon; used for the happy-path taxon parse test."
   }
   ```

3. Trim the payload to what the test actually exercises, but never edit values
   to make a test pass — a fixture that no longer matches reality is worse than
   no fixture. If the upstream shape changed, change the code.

## Rules

- No credentials, API keys, or personal data in a committed fixture.
- Keep fixtures small; large binary assets (image bytes) belong in a test that
  generates them locally with Pillow instead.
- Re-record deliberately, in its own commit, so the diff shows what upstream
  changed.
