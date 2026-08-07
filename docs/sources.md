# External data sources

Every external source Sift reads from is documented here **before** any code
depends on it (STANDARDS.md rule 7). Each entry carries URL, licence, required
citation, refresh cadence, and known limitations — stated honestly, because the
limitations are what stop someone building a feature the data cannot support.

Licence policy: CC0 / CC-BY / CC-BY-SA only, NonCommercial excluded
(`docs/decisions.md`, 2026-08-05).

---

## iNaturalist API (v1)

- **URL:** https://api.inaturalist.org/v1/ (docs: https://api.inaturalist.org/v1/docs/)
- **Used for:** Taxon records, taxonomy hierarchy, observation metadata, photo
  metadata and licence fields. Not image bytes — see the Open Dataset entry.
- **Licence:** Metadata is made available under CC0 by iNaturalist; individual
  observations and photos carry their own per-record licences, which must be
  read from the record and honoured. Do not assume the container licence
  applies to the contents.
- **Citation:** "iNaturalist. Available from https://www.inaturalist.org.
  Accessed <date>." Per-record photo attribution must name the observer and
  their photo's licence.
- **Refresh cadence:** Live. Records may change at any time; taxon IDs may be
  merged or deprecated without notice.
- **Rate limits:** Documented guidance is a maximum of 100 requests/minute and
  a courtesy target of ~1 request/second sustained; bulk work is expected to
  move to the Open Dataset instead. A descriptive `User-Agent` with contact
  details is expected.
- **How Sift uses it (as of M2):** four endpoints only, listed in
  `sift_pack.inat.client.Endpoint` — `species_counts` (rank a place's taxa),
  `observations` (licence-filtered photo selection), `taxa_by_id` (genus and
  family from the ancestor list), and `places_autocomplete` (state to place ID,
  resolved once into the committed `data/places.json`). Rate limiting is
  `pyinaturalist`'s default implementation of iNaturalist's published guidance,
  not a hand-rolled limiter. Every response is cached on disk, so a re-run costs
  nothing. A full 250-candidate state fetch is roughly 280 requests.
- **Known limitations:**
  - Crowd-sourced. Only "research grade" observations have community
    identification agreement; everything else is one person's opinion and must
    not be presented as an identification.
  - Taxonomy follows iNaturalist's own curated tree, which diverges from POWO,
    GBIF and USDA in places, especially below species rank. Cross-source joins
    on name will mismatch.
  - Geographic coverage is heavily biased toward North America, Europe and
    populated areas generally; absence of observations is not absence of the
    organism.
  - Coordinates for threatened taxa are obscured or hidden, so range data is
    systematically incomplete for exactly the species where it matters most.
  - Many photos are NC-licensed and therefore excluded by our licence policy;
    expect a substantial share of taxa to have no usable image.
  - No edibility, toxicity or medicinal data. Do not attempt to derive it.
  - `establishment_means` reports native/introduced status per place, and Sift
    deliberately does not use it. It is curator-maintained, sparsely populated,
    and disagrees with USDA PLANTS; nativity comes from USDA (M3). See
    `docs/decisions.md`, 2026-08-07.
  - `num_identification_agreements` counts agreements at fetch time and can fall
    as well as rise when identifications are withdrawn. Sift records the value
    it saw, with the fetch timestamp, rather than treating it as stable.
  - Observation photos carry per-photo licences that differ from the
    observation's own licence. Sift filters on the photo licence, and re-checks
    it client-side, because a server-side filter that stops working is silent.

## iNaturalist Open Dataset (AWS Open Data)

- **URL:** https://registry.opendata.aws/inaturalist-open-data/ — bucket
  `s3://inaturalist-open-data/` (metadata exports plus `photos/` image bytes)
- **Used for:** Image bytes, and bulk metadata joins that would be abusive to
  do against the API.
- **Licence:** The export is restricted to observations and photos under CC0,
  CC-BY and CC-BY-SA — which is precisely why we use it as the image path.
  Per-photo licence is carried in `photos.csv.gz` and must be preserved into
  our own records.
- **Citation:** "iNaturalist Open Data, hosted on AWS Open Data,
  https://registry.opendata.aws/inaturalist-open-data/. Accessed <date>."
  Plus per-photo attribution to the observer as required by CC-BY / CC-BY-SA.
- **Refresh cadence:** Metadata exports are regenerated roughly monthly; image
  objects are added continuously. Treat anything here as lagging the API.
- **Known limitations:**
  - Lags the API by up to a month, so a taxon may be current in the API and
    absent from the export. Metadata and image availability are not in sync.
  - Contains only open-licensed content by design — the absence of a photo here
    usually means it exists but is NC or all-rights-reserved, not that the
    observation has no photo.
  - Inherits every taxonomic and geographic bias of the API above.
  - Multiple image sizes exist per photo and not every size is present for
    every photo; code must handle a missing size rather than assuming a
    fallback.
  - Deleted or licence-changed photos disappear between exports; a URL that
    worked last month is not guaranteed to work now.
  - Requester pays does not apply, but full-corpus transfer is on the order of
    terabytes — bulk pulls must be scoped and cached.

## USDA PLANTS Database

- **URL:** https://plants.usda.gov/ (bulk download:
  https://plants.usda.gov/downloads)
- **Used for:** US distribution by state, native/introduced status, wetland
  indicator status, growth habit, and the USDA symbol as a cross-reference key.
- **Licence:** US federal government work, public domain within the United
  States. Some contributed content (notably certain photographs and drawings)
  is credited to third parties and is not automatically public domain — check
  per item before use.
- **Citation:** "USDA, NRCS. The PLANTS Database (https://plants.usda.gov).
  National Plant Data Team, Greensboro, NC, USA. Accessed <date>."
- **Refresh cadence:** Irregular. Updates are infrequent and not announced on a
  schedule; pin the download date in provenance and re-check periodically
  rather than assuming currency.
- **Known limitations:**
  - United States and its territories only. No use for anything outside that
    range, and no inference of absence elsewhere.
  - Distribution is recorded at state (and sometimes county) granularity, which
    is far coarser than it looks — "present in California" spans deserts and
    rainforest.
  - Taxonomy is its own accepted-name system and does not align with
    iNaturalist's; joins must go through name matching with manual review, and
    that matching is lossy. There is no shared stable identifier.
  - Some records are decades old and reflect historical rather than current
    distribution; introduced-species data in particular lags reality.
  - Site has been reorganised more than once and bulk download URLs have
    changed; treat the endpoint as unstable and fail loudly rather than
    silently fetching an empty file.
  - No phenology, cultivation or edibility data of usable quality.
