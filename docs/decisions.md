# Decisions

ADR-lite. One dated entry per decision, one paragraph each: what was decided,
why, and what it costs. Append only — if a decision is reversed, add a new
entry that supersedes the old one rather than editing history.

---

### 2026-08-05 — Sift is a separate application from Loam

Sift is built as its own application with its own repository, dependencies and
release cycle; it shares no Python package with Loam. If the two need to
exchange data, they do it over a documented JSON bridge — a file or endpoint
with an explicit schema — not by importing each other's internals. The reason
is coupling: a shared package makes every Sift refactor a Loam migration, and
the two have different rates of change and different data lifetimes. The cost
is some duplication of small utilities and the discipline of versioning the
bridge format; that is cheaper than a shared dependency that neither project
can move independently.

### 2026-08-05 — Licences: CC0, CC-BY and CC-BY-SA only; NonCommercial excluded

Sift ingests only sources licensed CC0, CC-BY or CC-BY-SA, and explicitly
excludes anything carrying a NonCommercial (NC) restriction, along with
all-rights-reserved material. This is not a judgement about NC data's quality —
it is about optionality. A single NC record propagates its restriction into
every derived dataset and forecloses any future commercial use of the whole
pipeline, and unpicking it later means auditing provenance across the entire
corpus. Excluding it up front costs us real data (a large share of iNaturalist
photos are NC-licensed) but keeps every downstream option open. Share-Alike is
accepted with the understanding that derived datasets incorporating CC-BY-SA
material inherit that obligation, which is tracked per-record via the
provenance wrapper.

### 2026-08-05 — `inat_taxon_id` is the primary key; names are mutable attributes

Every taxon record is keyed on its iNaturalist taxon ID. Scientific and common
names are stored as attributes of that record, never as identifiers, and no
lookup path resolves a name to data without going through an ID first.
Taxonomy is not stable: species get split, merged, renamed and reassigned to
new genera, and common names vary by region and vernacular. Code keyed on a
name silently rots — the name still resolves, just to a different organism, and
nothing errors. The cost is an extra resolution step on every user-supplied
name and a dependency on iNaturalist's ID space; the benefit is that taxonomic
churn shows up as an explicit ID remap we can detect and record, rather than as
data quietly drifting under us.

### 2026-08-05 — Metadata from the iNaturalist API, image bytes from the open-data S3 bucket

Taxon and observation metadata is fetched from the iNaturalist API; image bytes
are fetched from the `inaturalist-open-data` S3 bucket, never by scraping
photo URLs from API responses. The two paths have different characteristics:
the API is rate-limited, authoritative and current, and is the right place for
records that change; the S3 bucket is bulk-accessible, licence-filtered to
open-data content, and carries no rate limit worth the name, which is the right
place for the bytes. Pulling images through the API would burn the request
budget we need for metadata and would pull in photos whose licences we have
excluded. The cost is two ingest paths to maintain and a bucket export that
lags the API by roughly a month, so image availability trails metadata; that
lag is acceptable because images are cached and metadata is not.

### 2026-08-06 — The build half is the `sift_pack` package; the distribution stays `sift`

Python code lives in `src/sift_pack/`, published by a distribution still named
`sift` and exposed as the `sift-pack` console script. The split names the two
halves of the system: `sift_pack` builds packs and is the only half that talks
to upstream sources, while the runtime that consumes a pack shares no code with
it and communicates only through the manifest schema. Naming the package for
its job rather than for the product makes the boundary hard to blur — there is
no obvious place inside `sift_pack` to put runtime code. The cost is one
indirection in `pyproject.toml` (`tool.uv.build-backend.module-name`) and a
package name that does not match the import people expect from the repo name.

### 2026-08-06 — Bird axis 1 is seasonality, not nativity

When the birds domain is implemented, its second axis will be seasonality —
resident, summer, winter, migrant — and not the native/introduced distinction
the plants domain uses. Nativity is close to meaningless for a migratory
animal: a snowy owl in Michigan in January is not "introduced", it is an
irruptive winter visitor, and a barn swallow is present for half the year and
in South America for the other half. Reusing the plants vocabulary would
produce labels that are grammatical, confident and wrong — the exact failure
mode Sift exists to prevent, and one that would be nearly invisible because
every card would look plausible. Until somebody implements a seasonality axis
with a real source behind it, `BirdsDomain` raises `NotImplementedError` on
every method rather than inheriting plants' behaviour. The cost is that adding
birds is a genuine piece of work rather than a config change, plus a domain in
the registry that cannot be built today; that is the honest price.

### 2026-08-06 — `axis1_answer` returns `None` for "cannot determine", and callers must drop

The domain protocol's `axis1_answer` returns `Axis1Result | None`, where `None`
means the domain could not determine the claim. Callers are required to drop
the taxon and count the drop; there is no default, no fallback to the commonest
value, and no "unknown" member in the vocabulary to put a guess into. `None`
rather than an exception, because not knowing whether a given species is native
to a given state is the ordinary state of affairs — regional datasets are
patchy, and an exception would imply something went wrong when nothing did.
The schema backs this up: `Taxon.axis1_source` has no default, so a taxon
without a resolved claim cannot be constructed at all. The cost is that packs
will be much smaller than the candidate pool, especially early — the M1 plants
build resolves nothing and emits zero taxa — and that every caller carries the
obligation to handle `None`. An empty deck is the correct output for a build
that knows nothing.
