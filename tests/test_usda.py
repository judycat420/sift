"""Tests for USDA reconciliation — the only code that can create a nativity claim.

The transport is a recorded stand-in, so these run with no HTTP library in the
loop. The spot-check cases at the end are hand-verified ground truth: if
reconciliation disagrees with them, the reconciler is wrong, not the test.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from sift_pack.domains import Axis1Result
from sift_pack.domains.plants import PlantsDomain
from sift_pack.usda.client import (
    HttpxTransport,
    PlantsCacheMissError,
    PlantsClient,
    PlantsError,
    parse_plants_name,
)
from sift_pack.usda.reconcile import (
    NATIVITY_REGION,
    USDA_SOURCE_NAME,
    reconcile,
    usda_source_ref,
)

VERSION = date(2026, 8, 8)
HYBRID = "\u00d7"  # U+00D7, the botanical hybrid sign


class RecordedTransport:
    """Serves canned PLANTS responses and counts requests."""

    def __init__(
        self,
        search: dict[str, list[dict[str, Any]]] | None = None,
        profiles: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        """Record the response tables."""
        self.search = search or {}
        self.profiles = profiles or {}
        self.requested: list[str] = []

    def get_json(self, url: str) -> object:
        """Return the canned response for a URL."""
        self.requested.append(url)
        if "PlantSearch" in url:
            key = urllib.parse.unquote(url.split("searchText=")[1])
            return [{"Plant": p} for p in self.search.get(key, [])]
        symbol = urllib.parse.unquote(url.split("symbol=")[1])
        return {
            "Symbol": symbol,
            "ScientificName": f"<i>{symbol}</i>",
            "NativeStatuses": self.profiles.get(symbol, []),
        }


def plant(
    name: str, symbol: str, *, accepted: str | None = None, synonym: bool = False
) -> dict[str, Any]:
    """One PLANTS search hit, italicised the way PLANTS italicises names."""
    return {
        "Symbol": symbol,
        "ScientificName": f"<i>{name}</i> L.",
        "AcceptedSymbol": accepted,
        "SynonymSymbol": symbol if synonym else None,
    }


def client_for(tmp_path: Path, transport: RecordedTransport) -> PlantsClient:
    """A PLANTS client over a recorded transport."""
    return PlantsClient(tmp_path, transport)


# --- name parsing: the only safe way to strip an authority ---------------------


@pytest.mark.parametrize(
    ("html", "expected", "infra"),
    [
        ("<i>Asclepias tuberosa</i> L.", "Asclepias tuberosa", False),
        ("<i>Trillium grandiflorum</i> (Michx.) Salisb.", "Trillium grandiflorum", False),
        ("<i>Alliaria petiolata</i> (M. Bieb.) Cavara & Grande", "Alliaria petiolata", False),
        ("<i>Asclepias tuberosa</i> L. ssp. <i>interior</i> Woodson", "Asclepias tuberosa", True),
        ("<i>Daucus carota</i> L. var. <i>sativus</i> Hoffm.", "Daucus carota", True),
        (None, None, False),
    ],
)
def test_authority_is_stripped_by_the_italic_block(
    html: str | None, expected: str | None, infra: bool
) -> None:
    # Token counting gets "(Michx.) Salisb." wrong; PLANTS italicises exactly
    # the botanical name, so the first italic block is the answer.
    assert parse_plants_name(html) == (expected, infra)


# --- the three tiers ----------------------------------------------------------


def test_tier_1_exact_accepted_name_is_high_confidence(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {"Asclepias tuberosa": [plant("Asclepias tuberosa", "ASTU")]},
        {"ASTU": [{"Region": "L48", "Status": "N"}]},
    )
    out = reconcile(client_for(tmp_path, transport), 1, "Asclepias tuberosa", VERSION)
    assert out.tier == 1
    assert out.claim is not None
    assert (out.claim.value, out.claim.confidence) == ("native", "high")
    assert [source.name for source in out.claim.sources] == [USDA_SOURCE_NAME]
    assert out.claim.sources[0].version == f"retrieved {VERSION.isoformat()}"


def test_tier_2_synonym_follows_to_the_accepted_taxon(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {
            "Aster novae-angliae": [
                plant("Aster novae-angliae", "ASNO", accepted="SYNO2", synonym=True)
            ]
        },
        {"SYNO2": [{"Region": "L48", "Status": "N"}]},
    )
    out = reconcile(client_for(tmp_path, transport), 2, "Aster novae-angliae", VERSION)
    assert out.tier == 2
    assert out.plants_symbol == "SYNO2"
    assert out.claim is not None
    assert out.claim.confidence == "high"


def test_tier_3_loose_match_is_medium_confidence(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {f"Quercus {HYBRID}warei": [plant("Quercus warei", "QUWA")]},
        {"QUWA": [{"Region": "L48", "Status": "N"}]},
    )
    out = reconcile(client_for(tmp_path, transport), 3, f"Quercus {HYBRID}warei", VERSION)
    assert out.tier == 3
    assert out.claim is not None
    assert out.claim.confidence == "medium"


def test_an_accepted_name_beats_a_synonym_of_the_same_string(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {
            "Some plant": [
                plant("Some plant", "SYN1", accepted="ACC1", synonym=True),
                plant("Some plant", "ACC1"),
            ]
        },
        {"ACC1": [{"Region": "L48", "Status": "N"}]},
    )
    out = reconcile(client_for(tmp_path, transport), 4, "Some plant", VERSION)
    assert out.tier == 1


# --- refusals: every way a claim is declined ----------------------------------


def test_no_match_returns_no_claim(tmp_path: Path) -> None:
    out = reconcile(client_for(tmp_path, RecordedTransport()), 5, "Nothing atall", VERSION)
    assert out.claim is None
    assert out.reason == "no_plants_record"
    assert not out.matched


def test_an_infraspecific_only_match_is_not_a_species_match(tmp_path: Path) -> None:
    # PLANTS returning only a subspecies is not the species Sift asked about.
    transport = RecordedTransport(
        {
            "Daucus carota": [
                {
                    "Symbol": "DACAS",
                    "ScientificName": "<i>Daucus carota</i> L. ssp. <i>sativus</i> Hoffm.",
                    "AcceptedSymbol": None,
                    "SynonymSymbol": None,
                }
            ]
        }
    )
    out = reconcile(client_for(tmp_path, transport), 6, "Daucus carota", VERSION)
    assert out.reason == "no_plants_record"


def test_two_accepted_taxa_that_disagree_are_ambiguous(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {"Contested name": [plant("Contested name", "AAA1"), plant("Contested name", "BBB2")]},
        {
            "AAA1": [{"Region": "L48", "Status": "N"}],
            "BBB2": [{"Region": "L48", "Status": "I"}],
        },
    )
    out = reconcile(client_for(tmp_path, transport), 7, "Contested name", VERSION)
    assert out.reason == "ambiguous_plants_match"
    assert "picking one would be a guess" in out.detail


def test_homonyms_that_agree_are_not_ambiguous(tmp_path: Path) -> None:
    # PLANTS lists later homonyms as taxa of their own — `Monarda fistulosa
    # Sims, nom. illeg.` alongside the legitimate name. When every candidate
    # gives the same label, which record is "right" cannot change the answer,
    # so refusing would drop a common plant for a purely nomenclatural reason.
    transport = RecordedTransport(
        {
            "Monarda fistulosa": [
                plant("Monarda fistulosa", "MOFI"),
                plant("Monarda fistulosa", "MOFI2"),
            ]
        },
        {
            "MOFI": [{"Region": "L48", "Status": "N"}],
            "MOFI2": [{"Region": "L48", "Status": "N"}],
        },
    )
    out = reconcile(client_for(tmp_path, transport), 7, "Monarda fistulosa", VERSION)
    assert out.claim is not None
    assert out.claim.value == "native"
    assert out.tier == 1


def test_homonyms_are_still_refused_when_only_one_has_a_status(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {"Some plant": [plant("Some plant", "AAA1"), plant("Some plant", "BBB2")]},
        {"AAA1": [{"Region": "L48", "Status": "N"}]},
    )
    out = reconcile(client_for(tmp_path, transport), 7, "Some plant", VERSION)
    assert out.claim is None
    assert out.reason == "ambiguous_plants_match"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("NI", "conflicting_native_status"),
        ("N?", "uncertain_native_status"),
        ("I?", "uncertain_native_status"),
        ("N?I", "uncertain_native_status"),
        ("W", "uncertain_native_status"),
        ("GP", "uncertain_native_status"),
    ],
)
def test_an_ambiguous_status_code_never_becomes_a_label(
    tmp_path: Path, code: str, reason: str
) -> None:
    # PLANTS' own hedges must not be coerced into the nearer of native/introduced.
    transport = RecordedTransport(
        {"Some plant": [plant("Some plant", "SP1")]}, {"SP1": [{"Region": "L48", "Status": code}]}
    )
    out = reconcile(client_for(tmp_path, transport), 8, "Some plant", VERSION)
    assert out.claim is None
    assert out.reason == reason


def test_a_taxon_with_no_l48_status_is_declined(tmp_path: Path) -> None:
    transport = RecordedTransport(
        {"Arctic plant": [plant("Arctic plant", "AP1")]}, {"AP1": [{"Region": "AK", "Status": "N"}]}
    )
    out = reconcile(client_for(tmp_path, transport), 9, "Arctic plant", VERSION)
    assert out.reason == "no_native_status"
    assert NATIVITY_REGION in out.detail


def test_a_lookup_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    class Broken:
        def get_json(self, url: str) -> object:
            del url
            message = "PLANTS is down"
            raise PlantsError(message)

    out = reconcile(PlantsClient(tmp_path, Broken()), 10, "Some plant", VERSION)
    assert out.reason == "plants_lookup_failed"


def test_every_outcome_has_a_claim_or_a_reason_never_both_nor_neither(tmp_path: Path) -> None:
    cases = [
        (RecordedTransport(), "Nothing atall"),
        (
            RecordedTransport(
                {"Some plant": [plant("Some plant", "SP1")]},
                {"SP1": [{"Region": "L48", "Status": "N"}]},
            ),
            "Some plant",
        ),
    ]
    for transport, name in cases:
        out = reconcile(client_for(tmp_path / name, transport), 11, name, VERSION)
        assert (out.claim is None) != (out.reason is None)


# --- the client ---------------------------------------------------------------


def test_a_second_lookup_is_served_from_cache(tmp_path: Path) -> None:
    transport = RecordedTransport({"Some plant": [plant("Some plant", "SP1")]})
    client = client_for(tmp_path, transport)
    client.search("Some plant")
    client.search("Some plant")
    assert len(transport.requested) == 1
    assert client.stats.hits == 1


def test_an_offline_client_raises_on_a_miss(tmp_path: Path) -> None:
    with pytest.raises(PlantsCacheMissError, match="no cached response"):
        PlantsClient(tmp_path, offline=True).search("Some plant")


def test_an_html_shell_response_is_an_error_not_data() -> None:
    # PLANTS serves its Angular shell with HTTP 200 for retired endpoints, so a
    # moved URL looks exactly like a successful fetch unless this fires.
    def shell(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="<!doctype html>", headers={"content-type": "text/html"})

    transport = HttpxTransport(sleeper=lambda _: None)
    transport.client = httpx.Client(transport=httpx.MockTransport(shell))
    with pytest.raises(PlantsError, match="web application shell"):
        transport.get_json("https://plants.usda.gov/csvdownload")


def test_the_cached_projection_drops_what_reconciliation_does_not_read(tmp_path: Path) -> None:
    transport = RecordedTransport({"Some plant": [plant("Some plant", "SP1")]})
    client_for(tmp_path, transport).search("Some plant")
    entry = next((tmp_path / "search").glob("*.json"))
    payload = json.loads(entry.read_text(encoding="utf-8"))
    assert set(payload["response"]["results"][0]) == {
        "symbol",
        "scientific_name",
        "binomial",
        "is_infraspecific",
        "accepted_symbol",
        "is_synonym",
    }


# --- the domain is a lookup, never a derivation --------------------------------


def test_an_unwired_plants_domain_still_determines_nothing() -> None:
    # The M1 behaviour, unchanged: a domain with no source claims nothing.
    assert PlantsDomain().axis1_answer(48662, "MI") is None


def test_the_domain_returns_only_what_the_index_holds() -> None:
    claim = Axis1Result("native", (usda_source_ref(VERSION),), "high")
    domain = PlantsDomain({48662: claim})
    assert domain.axis1_answer(48662, "MI") is claim
    assert domain.axis1_answer(99999, "MI") is None


# --- spot check: fifteen hand-verified Michigan species ------------------------
#
# Ground truth, verified by hand against the Michigan flora. These run against
# real recorded PLANTS responses (tests/fixtures/usda_cache/), so a failure here
# means the reconciler is wrong — not the test.

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "usda_cache"


def spot_check_cases() -> list[tuple[str, str]]:
    """The hand-verified species and their expected labels."""
    payload = json.loads((FIXTURE_CACHE / "SPOT_CHECK.json").read_text(encoding="utf-8"))
    return [(name, "native") for name in payload["native"]] + [
        (name, "introduced") for name in payload["introduced"]
    ]


@pytest.mark.parametrize(("name", "expected"), spot_check_cases())
def test_hand_verified_michigan_species_get_the_right_label(name: str, expected: str) -> None:
    client = PlantsClient(FIXTURE_CACHE, offline=True)
    out = reconcile(client, 1, name, VERSION)
    assert out.claim is not None, f"{name} should reconcile, got {out.reason}: {out.detail}"
    assert out.claim.value == expected, (
        f"{name} is {expected} in Michigan; reconciliation said {out.claim.value} "
        f"via tier {out.tier} against PLANTS {out.plants_symbol}"
    )


def test_every_spot_check_species_matches_at_tier_1_or_2() -> None:
    # The gate is >=90% of the pool at tier 1 or 2; these fifteen are the
    # commonest plants in the state and must all be at the top tiers.
    client = PlantsClient(FIXTURE_CACHE, offline=True)
    tiers = {}
    for name, _ in spot_check_cases():
        out = reconcile(client, 1, name, VERSION)
        tiers[name] = out.tier
    assert all(tier in (1, 2) for tier in tiers.values()), tiers


def test_every_spot_check_claim_carries_full_provenance() -> None:
    client = PlantsClient(FIXTURE_CACHE, offline=True)
    for name, _ in spot_check_cases():
        claim = reconcile(client, 1, name, VERSION).claim
        assert claim is not None
        assert [source.name for source in claim.sources] == [USDA_SOURCE_NAME]
        assert claim.sources[0].version == f"retrieved {VERSION.isoformat()}"
        assert claim.sources[0].url
        assert claim.confidence in ("high", "medium")


def test_the_spot_check_covers_both_labels() -> None:
    # A test that only checked natives would pass with a reconciler hard-wired
    # to say "native".
    labels = {expected for _, expected in spot_check_cases()}
    assert labels == {"native", "introduced"}
    assert len(spot_check_cases()) == 15
