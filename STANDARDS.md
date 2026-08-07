# Sift Engineering Standards

This is the contract for all work in this repository. It applies to human and
machine contributors equally. If a change cannot satisfy a rule, change the
rule in a pull request first — do not quietly make an exception.

Most of these rules are enforced by `make check`. The ones that are not are
marked **(review-enforced)** and are the reviewer's job.

---

## 1. Types

Every function signature is annotated — arguments and return type, including
`-> None`. `mypy --strict` passes with zero errors.

No `# type: ignore` without an inline comment on the same construct explaining
*why* the checker is wrong or which upstream gap forces it. Bare
`# type: ignore` is rejected; use the specific code (`# type: ignore[arg-type]`)
— this is enforced by mypy's `ignore-without-code`.

```python
# Bad
def load(path):  # no annotations
    ...

# Bad
result = api.fetch(taxon_id)  # type: ignore

# Good
def load(path: Path) -> TaxonRecord:
    ...

# Good — pyinaturalist returns a loosely-typed JSON dict here; the cast is
# checked at runtime by TaxonRecord.model_validate on the next line.
raw = api.fetch(taxon_id)  # type: ignore[no-any-return]
```

Prefer making the type real over silencing the checker: a `TypedDict`, a
`pydantic` model, or a narrowing `assert` usually beats an ignore.

## 2. Module docstrings explain WHY

Every module opens with a docstring that answers two questions: **why does this
module exist**, and **what invariant does it protect**. A list of the functions
inside is not a docstring — the reader can already see those.

```python
# Bad
"""Taxon utilities.

Contains fetch_taxon, parse_taxon, and normalize_name.
"""

# Good
"""Resolution of iNaturalist taxon IDs to stable records.

WHY THIS MODULE EXISTS
----------------------
Common and scientific names change; iNaturalist merges, splits and deprecates
taxa. Code that keys on a name silently rots when the name moves.

INVARIANT PROTECTED
-------------------
Nothing outside this module resolves a name to a taxon. Callers pass
`inat_taxon_id` and receive names as attributes, never the reverse.
"""
```

**(review-enforced)** — ruff requires *a* module docstring; only a human can
tell whether it says anything.

## 3. Docstrings on public functions

Every public function (no leading underscore) gets a Google-style docstring
with `Args:`, `Returns:`, `Raises:`, and a runnable example. "Runnable" means
the example is a real doctest-shaped snippet a reader can paste — not
pseudocode, not `...`.

Private helpers get a one-line docstring only when the name does not already
make them obvious. Do not pad a five-line helper with a full section header.

```python
def confidence_for(observation_count: int) -> Confidence:
    """Map an iNaturalist observation count onto a confidence band.

    Args:
        observation_count: Number of research-grade observations backing the
            claim. Must be non-negative.

    Returns:
        The confidence band; `Confidence.LOW` for anything under 10.

    Raises:
        ValueError: If `observation_count` is negative.

    Example:
        >>> confidence_for(250)
        <Confidence.HIGH: 'high'>
    """
```

## 4. The provenance rule

**Any function producing a user-facing factual claim returns it wrapped with
`(value, source, confidence)`. Never a bare value.**

This is the rule the project exists to uphold. A "user-facing factual claim" is
anything a person could act on or repeat as true: a species name, a bloom
period, an edibility note, a range, a hardiness zone. It does not cover
plumbing — file paths, cache keys, HTTP status codes, internal counters.

```python
# Bad — the caller cannot tell if this is from a herbarium or a guess.
def bloom_period(taxon_id: int) -> str: ...

# Good
def bloom_period(taxon_id: int) -> Claim[str]: ...
```

The wrapper travels with the value all the way to the boundary. Unwrapping is
allowed only at the point of display or serialisation, where the source and
confidence are rendered alongside it. If you find yourself writing
`.value` in the middle of the pipeline, you are laundering provenance.

Aggregating several claims produces a new claim whose source names *all*
contributing sources and whose confidence is no higher than the weakest input.

## 5. No silent failures

- No bare `except:`. Catch the specific exception you can actually handle.
- No `except SomeError: pass`. If there is genuinely nothing to do, log it and
  increment a counter.
- **Unknown or malformed data is dropped and counted, never guessed.** No
  default-to-empty-string, no "probably metric", no inferring a missing field
  from a neighbouring one.
- Every pipeline stage reports how many records it dropped and why. A run that
  silently produces fewer rows than its input is a bug.

```python
# Bad
try:
    zone = int(row["zone"])
except Exception:
    zone = 5  # guessed

# Good
try:
    zone = int(row["zone"])
except (KeyError, ValueError) as exc:
    stats.drop("zone_unparseable", taxon_id=row.get("id"), detail=str(exc))
    return None
```

Exceptions that cross a module boundary are Sift's own types, raised `from` the
original — never swallowed and never re-raised bare.

## 6. Tests never touch the network

All HTTP is mocked with `respx` against fixtures recorded in `tests/fixtures/`
(see the README there for the recording procedure). This is enforced
mechanically: `tests/conftest.py` blocks every non-loopback socket, so a test
that reaches for the internet fails with `NetworkAccessError` rather than
passing slowly on a good day and flaking on a bad one.

Fixtures are recorded from real responses and never hand-edited to make a test
pass. When upstream changes shape, the fix goes in the code.

## 7. Every external source is documented

No source enters the codebase before it has an entry in `docs/sources.md`
carrying: URL, licence, required citation, refresh cadence, and **known
limitations stated honestly**. The limitations section is the important one —
"crowd-sourced, unreviewed for non-research-grade records" is the kind of thing
that must be written down before someone builds a feature on top of it.

Licence compatibility is checked at the point the source is added, against the
policy in `docs/decisions.md`: CC0 / CC-BY / CC-BY-SA only, no NonCommercial.

# 8. Gates are ratchets. 

Thresholds, strictness flags, and schema constraints may be tightened freely. Loosening any of them requires a dated ADR entry in docs/decisions.md stating what was tried first. If a gate fails, the default response is to fix the code, not the gate.

---

## Enforcement

```bash
make check   # install + lint + typecheck + test — the same thing CI runs
```

| Rule | Enforced by |
| --- | --- |
| 1. Types | `mypy --strict`, ruff `ANN`, `PGH003` |
| 2. Module docstrings | ruff `D100` (presence); reviewer (content) |
| 3. Function docstrings | ruff `D101`–`D103`, `D417` (presence, Args); reviewer (example) |
| 4. Provenance | reviewer — no linter can spot a laundered claim |
| 5. No silent failures | ruff `E722`, `BLE001`, `S110`, `S112`, `TRY` |
| 6. No network in tests | `tests/conftest.py` socket guard |
| 7. Documented sources | reviewer — `docs/sources.md` diff must accompany the code |

CI runs `make check` on every push. A red build is not merged.
