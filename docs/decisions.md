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

### 2026-08-07 — The pipeline splits: `CandidatePool` from iNaturalist, `Manifest` after promotion

M1 proved a `Manifest` cannot be built from iNaturalist data alone: `Taxon`
requires an axis-1 source, and iNaturalist does not know whether a plant is
native to Michigan. The obvious fix was to relax `Taxon` so it could be built
half-finished and completed later. We rejected that, because it would make the
unattributed state representable everywhere and forever in order to solve a
problem that exists in exactly one stage of one pipeline — and the whole point
of M1 was to make that state unrepresentable. Instead the pipeline splits.
`CandidatePool` holds what iNaturalist can honestly assert; `Manifest` holds
what a learner is shown; promotion between them is where a sourced claim is
attached, and it is the only path by which a nativity value can come to exist.
`CandidateTaxon` therefore has no axis-1 field under any name — not nullable,
not empty-string, absent — including iNaturalist's own `establishment_means`,
which reports native/introduced per place and would smuggle an unsourced claim
in through the back door. It is curator-maintained, sparsely populated, and
disagrees with USDA. The cost is two schemas to keep in step, an intermediate
artefact on disk, and a promotion step that must be written; the benefit is
that no half-built taxon exists anywhere in the system.

### 2026-08-07 — `CandidatePhoto` carries no `sha256` or `bytes`

`manifest.Image` requires a content hash and a byte size; `CandidatePhoto`, the
photo record inside a candidate pool, has neither. The iNaturalist API does not
return them — they can only be computed from the image bytes, which come from
the open-data S3 bucket (2026-08-05), a phase that has not been built. The
alternatives were to make the fields nullable on `Image`, which would loosen the
runtime contract for the benefit of an intermediate stage and is forbidden by
STANDARDS.md rule 8, or to synthesise a placeholder digest, which is what M1 did
with its fabricated candidate pool and is the same class of error as inventing a
nativity label. So the field is absent until the bytes exist, exactly as the
axis-1 field is absent until a source exists. The cost is that promotion must
fetch bytes before it can emit a manifest, and that `sift-pack build` cannot run
at M2 at all; it exits non-zero saying so rather than emitting an empty manifest,
which would falsely claim that promotion ran and rejected everything.

### 2026-08-07 — Taxa below 50 research-grade observations are dropped

A taxon needs at least 50 research-grade observations in the target place to
enter a candidate pool. The threshold is doing two jobs. It is a usefulness
filter: a pack should teach the plants somebody will actually meet, and a taxon
with a dozen records in a whole state is not one of them. It is also a
reliability filter, and that is the more important one — an observation count is
a proxy for how many people have looked at that taxon's records, so a rarely
observed taxon is also a rarely reviewed one, and its "research grade"
identifications rest on far fewer eyes. Fifty is a judgement, not a derived
value: it is roughly where Michigan's plant list stops being dominated by
garden escapes and single-record curiosities. The cost is that genuinely rare
native plants — often the most interesting ones — are excluded, and that the
threshold interacts with iNaturalist's geographic bias, so an under-observed
region will qualify fewer taxa than a well-observed one with the same flora.
Revisit it per-region if packs come out thin. Tightening it needs no ADR;
lowering it does (rule 8).

### 2026-08-07 — Tests mock at the cache seam, not with respx

STANDARDS.md rule 6 names `respx` as the mocking mechanism. `respx` intercepts
`httpx`, and the iNaturalist client is `pyinaturalist`, which is built on
`requests` — respx cannot see its traffic at all. We considered adding
`responses` or `requests-mock` to intercept at the transport layer, and rejected
it: the client already has a disk cache whose envelope format is exactly what a
recorded fixture needs to be, so pointing a real `InatClient` at
`tests/fixtures/inat_cache/` with `offline=True` exercises the production code
path — key derivation, envelope parsing, response normalisation — with no HTTP
library in the loop at all. That is strictly stronger than mocking transport:
there is no socket to intercept, so there is nothing to get wrong. The socket
blocker in `conftest.py` remains the backstop, and rule 6's actual requirement —
tests never touch the network, fixtures are recorded from real responses — is
met in full. `respx` stays the required mechanism for any future `httpx` client.
The cost is that fixtures are keyed by hash rather than by readable filename;
each envelope records its endpoint and parameters so a cache directory is still
auditable by reading it.

### 2026-08-07 — The cache stores projections, not raw responses

`InatClient` caches `sift_pack.inat.projections.project()` output — the fields
the parsers actually read — rather than whole response bodies. M2 cached raw and
paid 1.5 GB per state to answer four questions about it. Size is the smaller
half of the reason. The larger half is that a projected cache is a record of
what Sift *understood*: a field the projection does not carry is a field no
parser can quietly start depending on, because the cached fixtures replaying in
the test suite simply do not have it. `PROJECTION_VERSION` is part of every
cache key, so widening a projection partitions the cache instead of corrupting
it — old entries become unreachable rather than being served with a shape the
new parser does not expect. `--keep-raw` writes untouched bodies to `cache/raw/`
for debugging, keyed without the projection version so they survive a bump, and
that directory can be deleted at any time without affecting correctness. The
cost is that a new field needs a projection change and a version bump before any
parser can use it, and that debugging a parse failure means re-fetching with
`--keep-raw` unless it was already on. Caches written before this change carry
no format marker; the client detects that and refuses to use the directory,
naming the `rm -rf` rather than silently re-fetching around gigabytes of dead
weight.

### 2026-08-07 — Photos are sampled month-stratified, and selected for spread before confidence

Each taxon's photos come from four requests — months 3-5, 6-7, 8-9, and 10-2 —
at 25 per bucket, and selection round-robins across buckets so every season
contributes before any season contributes twice. Within a bucket, observations
with at least two agreeing identifications come first. No observer may supply
more than two of a taxon's photos.

M2 made one request for 200 observations and took the first eight usable ones.
Every rule it stated was satisfied and the result was still wrong: an
unstratified page is a seasonally and socially clustered sample. Bloodroot
photographed in April is bloodroot in flower, and a learner taught only that
cannot identify its leaves in July; one enthusiastic local observer can supply
much of a taxon's records, so the pack teaches their camera and their habitat.
This is an educational-quality defect, not a cost defect, and it is invisible in
every count M2 reported — eight photos from eight distinct observations in one
week of one spring looks identical, in the stats, to eight photos across a year.

Spread is ranked *above* identification confidence deliberately. A
thinly-confirmed photo of a plant in fruit teaches something no third flowering
photo can, so trading it for confidence would trade a real gap for a marginal
gain. The confidence floor that actually held is recorded per taxon, so the
compromise stays visible rather than assumed. Empty buckets are normal — a
spring ephemeral has no October records — and are recorded as zero rather than
treated as an error, because failing on them would drop precisely the plants
whose seasonality is most worth teaching. The cost is four requests per taxon
instead of one: about 1200 requests and twenty minutes for a 300-taxon state,
still an order of magnitude inside iNaturalist's daily guidance, and against a
much smaller cache than the single-request version produced.

### 2026-08-07 — One fetch at a time, enforced by a lockfile

`fetch` takes an exclusive lock on `work/.fetch.lock` before any network call.
A second concurrent fetch exits 7 having made no request; a lock whose owning
PID is dead is reported as stale and broken only with `--force`.

Sift's rate limiting comes from `pyinaturalist` and is per-process, so two
concurrent fetches simply double our request rate against a free,
donation-funded API. This is not hypothetical — it is what happens when a
twenty-minute fetch looks stalled and somebody starts another in a second
terminal, which is exactly what happened during M2 development. Being blocked
would affect every Sift user at once, not just the person who did it.

STANDARDS.md rule 6 set the precedent that a rule worth having gets a mechanism
rather than a convention: the conftest socket blocker makes "tests never touch
the network" unbreakable by accident. This does the same for "one fetch at a
time". The cost is a lock file that can outlive a killed process, which is why
staleness is detected and reported rather than either blocking forever or being
silently stolen.

### 2026-08-07 — Default fetch limit is 300, for a 250-taxon pack

`--limit` defaults to 300 rather than the 250 a finished pack wants. M4 promotes
candidates by matching them to USDA PLANTS, and unmatched taxa are dropped: a
name iNaturalist recognises is not always one USDA carries, especially for
recently split, merged or renamed species, and the two taxonomies are known not
to align (`docs/sources.md`). Fetching exactly 250 would mean discovering the
shortfall only after the expensive stage, then re-fetching to cover it — and the
second fetch would pull the *next* 50 taxa by observation count, which are
systematically less common and less well photographed than the first 250. The
headroom costs about 200 extra requests now and avoids a biased top-up later. If
M4's match rate turns out better than 83%, this can come back down; that is a
tightening and needs no ADR.

### 2026-08-07 — Throttle responses are waited out, not fatal

`PyinaturalistFetcher` catches HTTP 429, waits — honouring `Retry-After` when
the server sends one, otherwise backing off from 30 seconds and doubling — and
retries up to four times before giving up with a message saying nothing was
lost. This was found by the M2.1 gate run: a 1200-request fetch died 139
requests in on a single throttle. `pyinaturalist` retries 500, 502, 503 and 504
but not 429 (`RETRY_STATUSES`), so a fetch long enough to matter was near
certain to die partway on any given day.

This is error handling, not a second rate limiter. Request pacing stays entirely
pyinaturalist's — one per second, sixty per minute, ten thousand per day, shared
across processes through its SQLite bucket — and this code only decides what to
do when the server has already said "slow down". Backoff starts near the
per-minute window rather than at a token second, because a 429 means that window
is already spent and retrying inside it just spends another request learning the
same thing. Throttles are counted and logged rather than absorbed silently.

Giving up is still possible, and is safe: everything already fetched is cached,
so a re-run resumes rather than starting over. That property is why the fetch
needs no checkpoint file, and it is what makes a fatal-on-429 fetch merely
annoying rather than expensive.
