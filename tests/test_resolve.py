"""Tests for the resolve stage: downloads, transcoding, the store, and resumption.

The downloader is a recorded stand-in, so these run with no HTTP library in the
loop at all — the same seam pattern `inat.client.Fetcher` uses for the API, and
the same reason: there is no socket to block.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image as PILImage

from sift_pack.candidates import CandidatePool, CandidateTaxon
from sift_pack.download import DownloadError, DownloadFailure, download_one, sized_url
from sift_pack.imagestore import ImageStore, StoreProfileError, TranscodeProfile, sha256_of
from sift_pack.manifest import Image
from sift_pack.resolve import (
    ResolveOptions,
    commit,
    journal_path,
    referenced_digests,
    resolve_pool,
)
from sift_pack.resolved import ResolvedPool, ResolvedTaxon
from sift_pack.transcode import TranscodeError, current_profile, transcode
from tests.test_candidates import _photo, _pool


def jpeg_bytes(width: int = 1200, height: int = 800, shade: int = 40) -> bytes:
    """A real JPEG, so the transcoder does real work."""
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (10, shade, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def distinct_jpeg(seed: str) -> bytes:
    """A JPEG whose pixels are unique to `seed`.

    Colours are drawn from a hash across the full range rather than from a
    counter: adjacent shades quantise to identical bytes at WebP q75, which
    would make two different photos collide on one digest and trip the
    duplicate-image validator for reasons that have nothing to do with the code
    under test.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    image = PILImage.new("RGB", (1200, 800), (digest[0], digest[1], digest[2]))
    image.paste(
        (255, 255, 255),
        (digest[3] % 400, digest[4] % 300, digest[3] % 400 + 200, digest[4] % 300 + 200),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def jpeg_with_gps() -> bytes:
    """A JPEG carrying EXIF GPS, the thing transcoding must not redistribute."""
    buffer = io.BytesIO()
    image = PILImage.new("RGB", (600, 400), (200, 30, 30))
    exif = image.getexif()
    exif[0x8825] = {1: "N", 2: (42.0, 16.0, 0.0)}  # GPSInfo
    exif[0x010F] = "SecretCameraCo"  # Make
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


class RecordedDownloader:
    """Serves canned responses per URL, and counts what was asked for."""

    def __init__(self, responses: dict[str, tuple[int, str, bytes]] | None = None) -> None:
        """Start with a response table and an empty request log."""
        self.responses = responses or {}
        self.requested: list[str] = []

    def get(self, url: str) -> tuple[int, str, bytes]:
        """Return the canned response for a URL, else an image unique to it.

        Distinct bytes per URL by default, because real photos differ and the
        schema rejects a taxon carrying the same stored image twice.
        """
        self.requested.append(url)
        if url in self.responses:
            return self.responses[url]
        return 200, "image/jpeg", distinct_jpeg(url)


def candidate_pool(taxa: int = 2, photos: int = 4) -> CandidatePool:
    """A candidate pool whose photos have distinct, resolvable URLs."""
    built = []
    for index in range(1, taxa + 1):
        taxon_id = 47900 + index
        images = [
            _photo(
                index * 100 + n,
                taxon_id,
                photographer_login=f"obs{index}_{n}",
                month_bucket="ABCD"[n % 4],
                source_url=(
                    f"https://inaturalist-open-data.s3.amazonaws.com/photos/"
                    f"{index * 1000 + n}/square.jpg"
                ),
                inat_photo_id=index * 1000 + n,
            )
            for n in range(photos)
        ]
        built.append(
            CandidateTaxon(
                inat_taxon_id=taxon_id,
                scientific_name=f"Genus species{index}",
                common_names=[],
                rank="species",
                genus="Genus",
                family="Familia",
                obs_count=500,
                months_represented=len({i.month_bucket for i in images}),
                distinct_observers=len({i.photographer_login for i in images}),
                images=images,
            )
        )
    return _pool(candidates=built)


def store_at(path: Path) -> ImageStore:
    """A store using this build's encoder profile."""
    return ImageStore(path, current_profile())


# --- URL handling: never constructed ------------------------------------------


def test_size_segment_is_rewritten_and_the_extension_preserved() -> None:
    assert sized_url("https://h/photos/1/square.jpg") == "https://h/photos/1/medium.jpg"
    assert sized_url("https://h/photos/2/square.jpeg") == "https://h/photos/2/medium.jpeg"
    assert sized_url("https://h/photos/3/square.png") == "https://h/photos/3/medium.png"


def test_a_url_of_unexpected_shape_is_refused_not_guessed_at() -> None:
    with pytest.raises(DownloadError, match="refusing to rewrite"):
        sized_url("https://h/photos/3/square")


# --- download failure classification ------------------------------------------


@pytest.mark.parametrize(
    ("status", "content_type", "body", "reason"),
    [
        (404, "text/html", b"gone", "photo_not_found"),
        (500, "text/html", b"oops", "photo_download_failed"),
        (403, "text/html", b"nope", "photo_download_failed"),
        (200, "text/html", b"<html>error</html>", "photo_not_an_image"),
        (200, "image/jpeg", b"", "photo_download_failed"),
    ],
)
def test_each_failure_mode_gets_its_own_reason(
    status: int, content_type: str, body: bytes, reason: str
) -> None:
    downloader = RecordedDownloader({"https://h/a.jpg": (status, content_type, body)})
    outcome = download_one(downloader, 1, "https://h/a.jpg")
    assert isinstance(outcome, DownloadFailure)
    assert outcome.reason == reason


def test_a_transport_failure_is_a_recorded_outcome_not_an_exception() -> None:
    class Broken:
        def get(self, url: str) -> tuple[int, str, bytes]:
            message = f"connection reset for {url}"
            raise DownloadError(message)

    outcome = download_one(Broken(), 1, "https://h/a.jpg")
    assert isinstance(outcome, DownloadFailure)
    assert outcome.reason == "photo_download_failed"
    assert "connection reset" in outcome.detail


# --- transcoding ---------------------------------------------------------------


def test_transcode_resizes_to_the_longest_edge() -> None:
    result = transcode(jpeg_bytes(1200, 800))
    assert (result.width, result.height) == (500, 333)


def test_transcode_does_not_upscale_a_small_image() -> None:
    result = transcode(jpeg_bytes(200, 100))
    assert (result.width, result.height) == (200, 100)


def test_identical_input_bytes_produce_identical_output() -> None:
    source = jpeg_bytes()
    assert transcode(source).payload == transcode(source).payload
    assert sha256_of(transcode(source).payload) == sha256_of(transcode(source).payload)


def test_different_images_produce_different_digests() -> None:
    assert sha256_of(transcode(jpeg_bytes(shade=40)).payload) != sha256_of(
        transcode(jpeg_bytes(shade=200)).payload
    )


def test_no_stored_webp_retains_exif() -> None:
    source = jpeg_with_gps()
    assert PILImage.open(io.BytesIO(source)).getexif(), "fixture must actually carry EXIF"

    output = PILImage.open(io.BytesIO(transcode(source).payload))
    assert not dict(output.getexif())
    assert "exif" not in output.info
    assert "icc_profile" not in output.info


def test_gps_bytes_are_absent_from_the_output_entirely() -> None:
    # Not just "the parser reports no EXIF" — the camera make must not survive
    # anywhere in the payload.
    payload = transcode(jpeg_with_gps()).payload
    assert b"SecretCameraCo" not in payload


def test_undecodable_bytes_raise_rather_than_yielding_a_placeholder() -> None:
    with pytest.raises(TranscodeError, match="not a decodable image"):
        transcode(b"this is not an image")


# --- the content-addressed store ----------------------------------------------


def test_bytes_are_addressed_by_their_own_hash(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    digest = store.put(b"payload")
    assert digest == sha256_of(b"payload")
    assert store.path_for(digest).read_bytes() == b"payload"


def test_storing_the_same_bytes_twice_reuses_one_entry(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.put(b"payload")
    store.put(b"payload")
    assert len(list(store.digests())) == 1


def test_a_store_written_by_another_encoder_is_refused(tmp_path: Path) -> None:
    store_at(tmp_path).put(b"payload")
    other = TranscodeProfile(max_edge=500, quality=90, image_format="WEBP", pillow_version="1.0")
    with pytest.raises(StoreProfileError, match="different encoder"):
        ImageStore(tmp_path, other)


def test_the_refusal_explains_that_hashes_are_over_transcoded_bytes(tmp_path: Path) -> None:
    store_at(tmp_path)
    other = TranscodeProfile(max_edge=400, quality=75, image_format="WEBP", pillow_version="1.0")
    with pytest.raises(StoreProfileError, match="transcoded bytes"):
        ImageStore(tmp_path, other)


def test_gc_removes_only_unreferenced_entries(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    keep = store.put(b"keep")
    store.put(b"bin")
    removed, freed = store.collect({keep})
    assert removed == 1
    assert freed == len(b"bin")
    assert list(store.digests()) == [keep]


# --- resolving a pool ----------------------------------------------------------


def test_resolve_produces_complete_image_records(tmp_path: Path) -> None:
    pool = candidate_pool()
    resolved, stats = resolve_pool(pool, RecordedDownloader(), store_at(tmp_path / "s"), tmp_path)

    assert len(resolved.taxa) == 2
    for taxon in resolved.taxa:
        for photo in taxon.images:
            assert len(photo.sha256) == 64
            assert photo.bytes > 0
            assert photo.license in {"cc0", "cc-by", "cc-by-sa"}
            assert photo.photographer_login
            # Every resolved photo is already a valid manifest Image.
            assert isinstance(photo.as_image(), Image)
    assert stats.downloaded > 0


def test_resolve_requests_the_medium_variant(tmp_path: Path) -> None:
    downloader = RecordedDownloader()
    resolve_pool(candidate_pool(), downloader, store_at(tmp_path / "s"), tmp_path)
    assert downloader.requested
    assert all(url.endswith("/medium.jpg") for url in downloader.requested)


def test_the_same_photo_resolved_twice_is_stored_once(tmp_path: Path) -> None:
    # The cross-state case: Michigan and Ohio share taxa, so the second state's
    # resolve finds the bytes already present. Simulated here by resolving the
    # same pool into the same store from two different work directories.
    store = store_at(tmp_path / "s")
    pool = candidate_pool()
    first, first_stats = resolve_pool(pool, RecordedDownloader(), store, tmp_path / "a")
    _, second_stats = resolve_pool(pool, RecordedDownloader(), store, tmp_path / "b")

    stored = len(list(store.digests()))
    assert stored == sum(len(t.images) for t in first.taxa)
    assert first_stats.stored == stored
    assert second_stats.stored == 0
    assert second_stats.deduped == stored


def test_a_404_mid_run_drops_that_photo_and_continues(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=2, photos=5)
    victim = pool.candidates[0].images[2]
    downloader = RecordedDownloader({sized_url(victim.source_url): (404, "text/html", b"gone")})

    resolved, _ = resolve_pool(pool, downloader, store_at(tmp_path / "s"), tmp_path)

    # The run finished, the other taxon is untouched, and the loss is named.
    assert len(resolved.taxa) == 2
    survivors = {t.inat_taxon_id: t for t in resolved.taxa}
    assert len(survivors[pool.candidates[0].inat_taxon_id].images) == 4
    assert len(survivors[pool.candidates[1].inat_taxon_id].images) == 5
    losses = [d for d in resolved.resolve_dropped if d.reason == "photo_not_found"]
    assert len(losses) == 1
    assert losses[0].inat_photo_id == victim.inat_photo_id


def test_a_taxon_falling_below_four_images_is_dropped_with_a_reason(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=2, photos=4)
    doomed = pool.candidates[0]
    downloader = RecordedDownloader(
        {sized_url(photo.source_url): (404, "text/html", b"gone") for photo in doomed.images[:2]}
    )

    resolved, _ = resolve_pool(pool, downloader, store_at(tmp_path / "s"), tmp_path)

    assert [t.inat_taxon_id for t in resolved.taxa] == [pool.candidates[1].inat_taxon_id]
    dropped = [d for d in resolved.resolve_dropped if d.reason == "insufficient_resolved_images"]
    assert len(dropped) == 1
    assert dropped[0].inat_taxon_id == doomed.inat_taxon_id
    assert "need 4" in dropped[0].detail
    assert "nothing honest to pad with" in dropped[0].detail


def test_an_undecodable_body_is_recorded_not_stored(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=1, photos=5)
    victim = pool.candidates[0].images[0]
    downloader = RecordedDownloader(
        {sized_url(victim.source_url): (200, "image/jpeg", b"not really a jpeg")}
    )
    resolved, _ = resolve_pool(pool, downloader, store_at(tmp_path / "s"), tmp_path)
    assert [d.reason for d in resolved.resolve_dropped] == ["photo_undecodable"]
    assert len(resolved.taxa[0].images) == 4


def test_signals_are_recomputed_from_what_survived(tmp_path: Path) -> None:
    # A photo lost to a 404 must not leave months_represented claiming a bucket
    # the learner will never see.
    pool = candidate_pool(taxa=1, photos=5)
    victim = pool.candidates[0].images[3]
    downloader = RecordedDownloader({sized_url(victim.source_url): (404, "text/html", b"gone")})
    resolved, _ = resolve_pool(pool, downloader, store_at(tmp_path / "s"), tmp_path)

    taxon = resolved.taxa[0]
    assert len(taxon.images) == 4
    assert taxon.months_represented == len({p.month_bucket for p in taxon.images})
    assert taxon.distinct_observers == len({p.photographer_login for p in taxon.images})


# --- resumability --------------------------------------------------------------


def test_a_rerun_downloads_and_transcodes_nothing(tmp_path: Path) -> None:
    pool = candidate_pool()
    store = store_at(tmp_path / "s")
    first, _ = resolve_pool(pool, RecordedDownloader(), store, tmp_path)
    commit(first, tmp_path)

    second = RecordedDownloader()
    again, stats = resolve_pool(pool, second, store, tmp_path)

    assert second.requested == []
    assert stats.downloaded == 0
    assert stats.stored == 0
    assert stats.transcode_seconds == 0.0
    assert {t.inat_taxon_id for t in again.taxa} == {t.inat_taxon_id for t in first.taxa}


def test_a_rerun_after_a_changed_candidate_set_redoes_that_taxon(tmp_path: Path) -> None:
    # Reuse is keyed on the candidate photo set, so a taxon whose fetch stage
    # picked different photos is resolved again rather than served stale.
    store = store_at(tmp_path / "s")
    first, _ = resolve_pool(candidate_pool(taxa=1, photos=4), RecordedDownloader(), store, tmp_path)
    commit(first, tmp_path)

    downloader = RecordedDownloader()
    resolve_pool(candidate_pool(taxa=1, photos=5), downloader, store, tmp_path)
    assert downloader.requested


def test_an_interrupted_run_resumes_from_its_journal(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=3)
    store = store_at(tmp_path / "s")

    # Simulate a crash after the first taxon by resolving a one-taxon slice,
    # leaving its journal in place.
    partial = _pool(candidates=[pool.candidates[0]])
    resolve_pool(partial, RecordedDownloader(), store, tmp_path)
    assert journal_path("MI", tmp_path).exists()

    resumed = RecordedDownloader()
    resolved, stats = resolve_pool(pool, resumed, store, tmp_path)

    assert stats.skipped == 1
    assert len(resolved.taxa) == 3
    # The replayed taxon's photos were not re-requested.
    first_urls = {sized_url(p.source_url) for p in pool.candidates[0].images}
    assert not (first_urls & set(resumed.requested))


def test_the_journal_survives_a_truncated_final_line(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=2)
    store = store_at(tmp_path / "s")
    resolve_pool(_pool(candidates=[pool.candidates[0]]), RecordedDownloader(), store, tmp_path)

    journal = journal_path("MI", tmp_path)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"inat_taxon_id": 99, "taxo')  # killed mid-write

    resolved, stats = resolve_pool(pool, RecordedDownloader(), store, tmp_path)
    assert stats.skipped == 1
    assert len(resolved.taxa) == 2


def test_commit_writes_atomically_and_retires_the_journal(tmp_path: Path) -> None:
    pool = candidate_pool()
    resolved, _ = resolve_pool(pool, RecordedDownloader(), store_at(tmp_path / "s"), tmp_path)
    assert journal_path("MI", tmp_path).exists()

    destination = commit(resolved, tmp_path)
    assert destination.exists()
    assert not journal_path("MI", tmp_path).exists()
    assert not list(tmp_path.glob("*.tmp"))
    reloaded = ResolvedPool.model_validate_json(destination.read_text(encoding="utf-8"))
    assert reloaded == resolved


def test_the_resolved_pool_round_trips(tmp_path: Path) -> None:
    resolved, _ = resolve_pool(
        candidate_pool(), RecordedDownloader(), store_at(tmp_path / "s"), tmp_path
    )
    first = resolved.model_dump_json(indent=2)
    assert ResolvedPool.model_validate_json(first).model_dump_json(indent=2) == first


# --- provenance ----------------------------------------------------------------


def test_the_encoder_is_recorded_as_a_source(tmp_path: Path) -> None:
    # Hashes are over transcoded bytes, so the encoder produced this artefact
    # as much as the bucket did.
    resolved, _ = resolve_pool(
        candidate_pool(), RecordedDownloader(), store_at(tmp_path / "s"), tmp_path
    )
    encoders = [s for s in resolved.sources if s.name == "Sift transcoder"]
    assert len(encoders) == 1
    assert "WEBP q75" in encoders[0].version
    assert "Pillow" in encoders[0].version


def test_the_open_data_bucket_is_recorded_as_a_source(tmp_path: Path) -> None:
    resolved, _ = resolve_pool(
        candidate_pool(), RecordedDownloader(), store_at(tmp_path / "s"), tmp_path
    )
    assert any(s.name == "iNaturalist Open Dataset" for s in resolved.sources)


def test_candidate_stage_sources_are_carried_forward(tmp_path: Path) -> None:
    resolved, _ = resolve_pool(
        candidate_pool(), RecordedDownloader(), store_at(tmp_path / "s"), tmp_path
    )
    assert any(s.name == "iNaturalist API" for s in resolved.sources)


def test_referenced_digests_reads_every_pool(tmp_path: Path) -> None:
    resolved, _ = resolve_pool(
        candidate_pool(), RecordedDownloader(), store_at(tmp_path / "s"), tmp_path
    )
    commit(resolved, tmp_path)
    assert set(referenced_digests(tmp_path)) == resolved.digests()


def test_a_resolved_pool_carries_no_axis1_field() -> None:
    assert not [f for f in ResolvedTaxon.model_fields if "axis1" in f or "native" in f]


def test_the_journal_is_valid_jsonl(tmp_path: Path) -> None:
    resolve_pool(candidate_pool(), RecordedDownloader(), store_at(tmp_path / "s"), tmp_path)
    lines = journal_path("MI", tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert "inat_taxon_id" in json.loads(line)


# --- --retry-failed: clear the failure ledger, keep everything else -----------


def test_retry_failed_retries_only_the_taxa_that_lost_photos(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=2, photos=5)
    victim = pool.candidates[0].images[0]
    store = store_at(tmp_path / "s")

    failing = RecordedDownloader({sized_url(victim.source_url): (404, "text/html", b"gone")})
    first, _ = resolve_pool(pool, failing, store, tmp_path)
    commit(first, tmp_path)
    assert [d.reason for d in first.resolve_dropped] == ["photo_not_found"]

    healed = RecordedDownloader()
    again, stats = resolve_pool(pool, healed, store, tmp_path, ResolveOptions(retry_failed=True))

    # Only the taxon that lost a photo was redone, and only its missing photo
    # was fetched — the four already stored were reused from the ledger.
    assert healed.requested == [sized_url(victim.source_url)]
    assert stats.downloaded == 1
    assert again.resolve_dropped == []
    assert len(again.taxa) == 2
    retried = next(t for t in again.taxa if t.inat_taxon_id == pool.candidates[0].inat_taxon_id)
    assert len(retried.images) == 5


def test_retry_failed_keeps_every_stored_image(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=2, photos=5)
    victim = pool.candidates[0].images[0]
    store = store_at(tmp_path / "s")
    failing = RecordedDownloader({sized_url(victim.source_url): (404, "text/html", b"gone")})
    first, _ = resolve_pool(pool, failing, store, tmp_path)
    commit(first, tmp_path)
    before = set(store.digests())

    resolve_pool(pool, RecordedDownloader(), store, tmp_path, ResolveOptions(retry_failed=True))
    assert before <= set(store.digests())


def test_retry_failed_revives_a_taxon_that_was_dropped_entirely(tmp_path: Path) -> None:
    pool = candidate_pool(taxa=2, photos=4)
    doomed = pool.candidates[0]
    store = store_at(tmp_path / "s")
    failing = RecordedDownloader(
        {sized_url(p.source_url): (404, "text/html", b"gone") for p in doomed.images[:2]}
    )
    first, _ = resolve_pool(pool, failing, store, tmp_path)
    commit(first, tmp_path)
    assert [t.inat_taxon_id for t in first.taxa] == [pool.candidates[1].inat_taxon_id]

    again, _ = resolve_pool(
        pool, RecordedDownloader(), store, tmp_path, ResolveOptions(retry_failed=True)
    )
    assert len(again.taxa) == 2
    assert again.resolve_dropped == []


def test_without_retry_failed_a_rerun_stays_sticky(tmp_path: Path) -> None:
    # The default contract: failures do not retry, so a re-run costs nothing.
    pool = candidate_pool(taxa=2, photos=5)
    victim = pool.candidates[0].images[0]
    store = store_at(tmp_path / "s")
    failing = RecordedDownloader({sized_url(victim.source_url): (404, "text/html", b"gone")})
    first, _ = resolve_pool(pool, failing, store, tmp_path)
    commit(first, tmp_path)

    healed = RecordedDownloader()
    again, stats = resolve_pool(pool, healed, store, tmp_path)
    assert healed.requested == []
    assert stats.downloaded == 0
    assert [d.reason for d in again.resolve_dropped] == ["photo_not_found"]
