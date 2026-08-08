# Decisions

ADR-lite. One dated entry per decision, one paragraph each: what was decided,
why, and what it costs.

## Convention

Every entry carries a `Status:` line directly under its heading:

- **`Status: Active`** — this is current guidance. Build to it.
- **`Status: Superseded by <date> — <title>`** — this records what was decided
  at the time and why. It is history, not instruction. Follow the entry it
  points to.

Entries are append-only in substance: no reasoning is ever deleted or rewritten,
because the reasoning that led to a wrong turn is the most useful thing in this
file. When a decision is replaced, the new decision gets its own dated entry and
the old entry changes in exactly two ways — its `Status:` line, and its title, so
the title describes what was decided then rather than asserting it as current.
Nothing else in a superseded entry is touched.

---

### 2026-08-05 — Sift is a separate application from Loam

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

Status: Active

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

### 2026-08-07 — Month-stratified photo sampling, originally ranked spread above agreement count

Status: Superseded by 2026-08-07 — Identification agreement removed from selection

Superseded only in its second criterion. Month stratification, the round-robin
across buckets, the per-observer cap and the four-photo floor are all still
current; what follows describes them accurately. The part that no longer holds
is the ranking of seasonal spread *above identification confidence*, which
presumed agreement count measured confidence. It does not. There is no longer a
second criterion for spread to be ranked ahead of.

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

Status: Active

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

Status: Active

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

Status: Active

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

### 2026-08-07 — `min_identification_agreement` is deleted; the statistic measures nothing

Status: Active

`CandidateTaxon.min_identification_agreement` is removed entirely, not recomputed
as a median or rehabilitated some other way. The field was uninformative by
construction, and no aggregation of it can fix that.

The mechanism: iNaturalist's `num_identification_agreements` counts
identifications that agree with **the observer's own** identification, and
research grade requires only two identifications in total. So the ordinary
research-grade observation reports exactly 1 — not because the taxon is hard,
not because the record is doubtful, but because two identifications is the
threshold and the second one is the first agreement. The number is a restatement
of the quality-grade filter we already applied, wearing the costume of a
confidence measure.

Verified against the Michigan pool: *Symplocarpus foetidus* (skunk cabbage,
unmistakable — no other Michigan plant looks remotely like it) and *Carex
intumescens* (a sedge, genuinely difficult, in a genus that defeats most
non-specialists) return near-identical agreement profiles, `[2,2,2,3,2,1,2,3]`
and `[2,3,3,1,2,2,2,2]`, and identical floors of 1. Across all 2400 photos the
distribution is 33% ones, 53% twos, 12% three-or-more; across taxa, 249 of 300
floor at 1. Taking the minimum over eight photos makes it worse — with eight
draws one is very likely to be a 1 — but the underlying field would not have
discriminated either.

RECORDED REFUTED HYPOTHESIS
---------------------------
We first read the flat distribution as evidence that the statistic measured
*obviousness* rather than *confidence* — the thought being that a distinctive
plant gets confirmed once and moved on from, while a difficult one attracts
argument, so a low floor might mark an easy taxon rather than a doubtful record.
That was wrong, and the skunk-cabbage/sedge pair refutes it directly: the
unmistakable species and the genuinely hard one are indistinguishable in this
number. It measures neither confidence nor obviousness. It measures that the
observation reached research grade, which we already knew, because we filtered
on it.

What survives is `CandidatePhoto.identification_agreements`, kept as a verbatim
record of an API value rather than a derived claim, and used only as a weak
tiebreaker within a seasonal bucket where the alternative is ordering by
observation ID. No taxon-level statistic is derived from it and none is shown to
a learner. Removing the field is a schema change: `extra="forbid"` means a pool
written before this fails to parse, loudly, rather than quietly dropping the key
and handing back a pool whose provenance differs from what this build produces.

The cost is that Sift now has no per-taxon identification-confidence signal at
all. That is the honest position — we had no such signal before either, only a
number that looked like one.

### 2026-08-07 — Identification agreement removed from selection

Status: Active

`PREFERRED_MIN_AGREEMENTS` is deleted and photo selection no longer reads
`num_identification_agreements` at all. Within a seasonal bucket, observations
are now taken in ascending observation-ID order: a tiebreak that asserts nothing
about the observations it orders and makes selection reproducible from a given
cache.

This is the second consequence of the finding recorded in "`min_identification_agreement`
is deleted; the statistic measures nothing" (2026-08-07). The first removed the
place where the value was *reported*; this removes the last place where it
influenced an *outcome*. Selection previously ordered by agreeing-ID count,
which by that entry's mechanism ranks observations that drew an extra
identifier — a property tracking whether a record was photogenic or contentious,
not whether it was correctly identified. It was noise presented as signal, and
worse, a future reader would reasonably assume a field named
`num_identification_agreements` meant what its name suggests and build on it.

The measured effect on the Michigan pool is larger than expected, and confirms
the sort was doing real damage rather than being merely inert:

| | share of photos reporting exactly 1 agreement |
| --- | --- |
| all 21,612 observations the buckets returned | 85.3% |
| selected photos, old agreement sort | 33.0% |
| selected photos, observation-ID tiebreak | 74.8% |

The old sort steered roughly two thirds of every pack toward the ~15% of records
that happened to attract an extra identifier. The residual gap between 74.8% and
the corpus rate is the per-observer cap and one-photo-per-observation dedup, not
a preference.

Seasonal spread was unaffected: `months_represented` is bit-for-bit identical
before and after the change — 4 taxa spanning one bucket, 12 spanning two, 30
spanning three, 254 spanning all four. That is the clearest evidence available
that the removed sort was orthogonal to the only property selection exists to
protect. It changed which photos, never which seasons.

The cost is that selection now makes no quality judgement below seasonal spread
and the observer cap. That is the honest position: iNaturalist exposes no
per-observation quality signal that survives inspection, and ordering by an
arbitrary-but-fixed key is better than ordering by a meaningful-looking one that
means nothing.

### 2026-08-07 — Frequency ranking already selects for identifiability; genus demotion is a safety net

Status: Active

Ranking by observation frequency does most of the work that a
difficulty filter would do, because the two properties are not independent:
taxa that are hard to identify are also taxa that get recorded rarely. Nobody
uploads the sedge they could not name, and the ones they do upload rarely reach
research grade. Difficulty suppresses observation count, so the frequency cut
that selects the top 300 of Michigan's 3390 plant taxa — the top 9% — is already
filtering for identifiability without being asked to.

Measured on the Michigan pool: 9 of 300 candidates sit in genera where even the
common species need technical characters, mature fruit or microscopy —
*Carex*, *Rubus*, *Salix*, *Crataegus*, *Amelanchier*, *Symphyotrichum*,
*Solidago*, *Viola*, *Hieracium*, *Elymus* and the graminoids. They cluster in
the bottom half of the ranking (ranks 68, 88, 131, 223, 232, 235, 256, 272, 284)
and their median observation count is 1053 against 1499 for the pool as a whole.
Several of the nine are distinctive at species level regardless — *Rubus
parviflorus* and *Symphyotrichum novae-angliae* are not the reason their genera
are feared — so the number of genuinely problematic cards is lower still. A
broader genus list, counting genera that are hard in general even where the
common species is not, gives 31; that the two counts differ this much is itself
the point, since frequency selects the distinctive species *within* hard genera
as well as avoiding the genera themselves.

The consequence for M4: `answer_rank = "genus"` demotion is a safety net
catching roughly seven to nine cards, not a primary filter the pack depends on.
It should be built and tested, and it should not be given a difficulty model, a
tuned threshold, or a curated genus list to maintain — the cost of that
machinery would exceed the harm it prevents. If a later state's pool shows the
hard-genus share climbing well above 3%, that assumption has broken and this
entry should be revisited.

### 2026-08-07 — Image bytes are transcoded and addressed by the hash of the output

Status: Active

Downloaded photos are decoded, resized to 500px on the longest edge, re-encoded
as WebP at quality 75, and stored at `images/{sha[:2]}/{sha}.webp` where the hash
is taken over the **transcoded** bytes, not the download.

Hashing the output rather than the input is what makes cross-state dedupe work.
Michigan, Ohio and Indiana share most of their flora; addressing by content means
the second and third states find the milkweed photograph already present instead
of storing three copies under three paths, with nothing to indicate they are the
same picture. It also means the store is only meaningful relative to an encoder:
change the resize, the quality, or the Pillow version and the same photo produces
different bytes and a different address. So the encoder profile — including the
exact Pillow version, patch included — is written into the store's marker file
and recorded as a `SourceRef` on every resolved pool. A store opened under a
different profile refuses to open rather than silently mixing two encoders'
output, which would return different bytes for the same logical image depending
on when each entry was written.

The cost is that a Pillow upgrade invalidates the whole store and forces a
re-transcode. That is the honest consequence of content-addressing derived bytes,
and the alternative — hashing the download and treating the encoder as an
implementation detail — would make the store's contents unverifiable against the
records that point at them.

### 2026-08-07 — All EXIF is stripped, and the reason is privacy rather than size

Status: Active

Transcoding discards every byte of source metadata. Not "GPS is removed and
orientation is kept" — the image is decoded to raw pixels and pasted into a fresh
buffer, so nothing from the source's `info` dictionary can reach the output.

A JPEG's EXIF block can carry the GPS coordinates of the camera that took it,
along with the device serial and the photographer's software. Sift redistributes
these images to strangers. iNaturalist obscures coordinates for threatened taxa
in its *API responses*, but that protection does not extend to metadata embedded
in an image file, and an observer who marked an observation obscured has not
thereby stripped their camera's EXIF. The failure mode of getting this wrong is
publishing somebody's home address, so the safe design is the one with no list to
maintain: keep nothing.

Orientation is the one thing worth losing, and it is not lost — EXIF rotation is
applied to the pixels before the metadata is discarded, so a photo that displayed
upright still does. The cost is that colour-managed images lose their ICC profile
and shift slightly; for 500px study photographs of plants that is not a trade
worth a metadata allowlist.

### 2026-08-07 — Photo URLs come from the API and are never rebuilt from a photo id

Status: Active

`PROJECTION_VERSION` went to 2 to carry `photos[].url`, and the URL is stored on
each `CandidatePhoto`. The only rewriting Sift does is replacing the size segment
— `square.jpg` becomes `medium.jpg` — which preserves the extension the API
reported. A URL whose final segment is not `<stem>.<ext>` is refused rather than
rewritten.

Version 1 dropped the URL, reasoning that image bytes come from the open-data
bucket keyed by photo id (2026-08-05). That was true and not sufficient: the id
alone does not give a URL, because file extensions vary per photo, and a
templated `.jpg` would 404 on every PNG. Those 404s are indistinguishable from
photos genuinely deleted since the fetch, so the loss rate would be unknown and
the loss itself silent — exactly the failure this project is organised against.

`medium` is requested because it is 500px on its longest edge, the same as the
transcode target: no pixels are downloaded that the encoder would immediately
discard, and nothing is ever upscaled.

The cost was a full re-fetch of Michigan's 1222 API responses, because raw bodies
were not kept and a projection is not reversible. That is the standing price of
projecting: a field dropped is a re-fetch if it turns out to be needed. It was
still the right trade — the projected cache is 17 MB where the raw one was 1.5 GB
— but the lesson is that `--keep-raw` is cheap insurance during a phase where the
projection is still settling.

### 2026-08-07 — Resolve is resumable per taxon, and a completed re-run is free

Status: Active

The resolve stage journals one line per taxon to
`work/resolved_<STATE>.partial.jsonl`, flushed as each taxon completes, and the
final pool is written atomically with the journal retired only after it lands. A
killed run resumes from the journal; a crash between the two leaves both and the
next run rebuilds from the journal. There is never a half-written pool.

A *completed* run is also free to repeat, because the finished
`resolved_<STATE>.json` is itself a ledger: a taxon is reused when every one of
its candidate photos is accounted for in that pool, either stored or recorded as
having failed. Anything less exact — a subset match, say — would silently serve a
stale pack when the fetch stage picked different photos.

Failures are sticky. A taxon that lost photos to 404s stays dropped across
re-runs rather than being retried each time, because retrying makes a re-run cost
2400 downloads and the common case is that a deleted photo stays deleted. To
retry, delete `resolved_<STATE>.json` and resolve again. That is a stated
contract rather than a guess about transience, and it is the one place where
being resumable and being self-healing conflict; resumable wins because this
stage is long enough that process death is the expected event.

### 2026-08-07 — `sift-pack gc` is manual and dry-run by default

Status: Active

Garbage collection deletes stored images no `resolved_*.json` in `work/`
references. It never runs as part of another command, and it reports without
deleting unless `--no-dry-run` is passed.

An unreferenced image is usually a pool that has not been rebuilt yet, not
garbage — resolve a state, change a filter upstream, and every image is briefly
unreferenced until the pool is rewritten. Automatic collection would make that
window destructive. The asymmetry decides it: deleting an image wrongly costs a
re-download, and deleting a store wrongly costs an afternoon of re-downloading
and re-transcoding several gigabytes, so the default is the one that cannot lose
an afternoon.

### 2026-08-08 — USDA PLANTS has no per-state native status; Sift teaches the L48 status

Status: Active

PLANTS records native status by *region* — `L48`, `CAN`, `AK`, `HI`, `PR` — and
not by state. There is no per-state native status anywhere in the database.
Sift therefore reads the `L48` entry, and the honest reading of a Michigan card
is "USDA records this taxon as native within the lower 48 states", not "native
to Michigan".

Those are not the same claim. A plant native to the Sonoran Desert and
naturalised around Detroit is `L48 (N)`, and Sift will call it native. The
error is real, it is systematic rather than random, and it is invisible in every
count this pipeline produces — a wrongly-native card looks exactly like a
correct one.

We looked for a better source inside PLANTS and there is not one. Its state data
is distribution — presence and absence — which cannot distinguish a native
occurrence from a naturalised one, so joining it would add a cross-check on
"does USDA agree this grows in Michigan" without touching the actual problem.
Genuinely per-state nativity exists in other datasets (BONAP, Michigan Flora,
state natural-heritage programs) with their own licences and their own
reconciliation problems; adopting one is a phase, not a patch.

What was done instead: the limitation is stated on the source
(`docs/sources.md`), in the reconciler's module docstring, and in the domain's
`axis1_answer`, whose `state` argument is explicitly not consulted so that
nobody reads the signature and assumes a state-level lookup happens. The cost is
that Sift ships a known-imperfect label rather than none. That is a real
departure from this project's usual posture, and it is a judgement that the L48
status is right for the large majority of a Michigan pack — every one of the
fifteen hand-verified spot-check species resolves correctly — while being wrong
for an unmeasured minority of US natives adventive in the Great Lakes. If that
minority matters more than the pack does, the answer is a per-state source, not
a tweak here.

### 2026-08-08 — Nativity claims are versioned by retrieval date, not publication date

Status: Active

`Axis1Result.source_version` carries the date Sift retrieved a PLANTS record,
because PLANTS exposes no publication date, edition or dataset version through
its services API. The profile endpoint returns taxonomy and status with nothing
to say when either last changed.

A retrieval date is weaker than a publication date and the difference matters:
two retrievals months apart are distinguishable only by this stamp, and a PLANTS
revision between them leaves no other trace in the record. A claim can be
re-checked against a later retrieval, but nothing tells us whether the answer
changed because the data did or because we asked again. This is recorded as the
best available answer rather than a good one; if PLANTS ever publishes a version
stamp, that becomes `source_version` and this entry is superseded.

### 2026-08-08 — Name reconciliation matches on the italicised binomial, in three named tiers

Status: Active

iNaturalist and USDA share no identifier, so the join is by name — the operation
most likely to produce a confident wrong answer, because a name that matches the
wrong record looks exactly like one that matches the right one.

Three rules, ordered, each recorded on the claim it produces: an exact accepted
species name (`high`), a PLANTS synonym followed to its accepted taxon (`high`),
and a match after case and the hybrid sign are normalised away (`medium`).
Anything else returns `None` and the taxon is dropped to
`work/unmatched_<STATE>.csv` with a reason.

The authority is stripped by reading the first `<i>` block, because PLANTS
italicises exactly the botanical name and leaves the authority in plain text.
Counting tokens instead gets `(Michx.) Salisb.` and `(M. Bieb.) Cavara & Grande`
wrong, and gets them wrong silently. Infraspecific records are excluded from a
species-level match: PLANTS returning only `Daucus carota ssp. sativus` is not
the species Sift asked about, and treating it as one would attach a cultivated
carrot's status to wild carrot.

PLANTS' own hedges are never coerced. `NI` (native in part of the region and
introduced in another), `N?`, `I?`, `W` (waif) and `GP` (cultivation only) each
become a distinct rejection reason rather than being rounded to the nearer of
native and introduced. The cost is a smaller pack; the alternative is a card
asserting something USDA itself declines to assert.
