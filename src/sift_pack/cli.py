"""Command-line entry point for the pack builder.

WHY THIS MODULE EXISTS
----------------------
It wires the stages together and decides what a human sees. Two rules shape it:
the artefact goes to stdout or a file while every diagnostic goes to stderr, so
output stays pipeable; and a command that cannot do its job exits non-zero
rather than emitting something plausible-looking.

INVARIANT PROTECTED
-------------------
No command here invents a value to fill a gap. `build` cannot yet produce a
manifest — promotion needs a nativity source (M3) and image digests from the
open-data bucket — so it says so and exits non-zero, rather than emitting an
empty manifest that would read as "we tried and everything was dropped".
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from sift_pack.candidates import CandidatePool
from sift_pack.domains import TaxonDomain
from sift_pack.domains.registry import UnknownDomainError, resolve_domain
from sift_pack.fetch import fetch_pool
from sift_pack.inat.client import InatClient, InatError
from sift_pack.inat.places import PLACES_PATH, load_places, refresh_places
from sift_pack.lock import FetchLockError, fetch_lock
from sift_pack.stats import summarise

__all__ = ["app", "build", "fetch", "main", "places", "stats"]

_log = logging.getLogger("sift_pack")

DEFAULT_CACHE_DIR = Path("cache")
DEFAULT_WORK_DIR = Path("work")

_EXIT_UNKNOWN_DOMAIN = 2
_EXIT_DOMAIN_UNAVAILABLE = 3
_EXIT_NOT_YET_IMPLEMENTED = 4
_EXIT_INAT_ERROR = 5
_EXIT_MISSING_POOL = 6
_EXIT_LOCKED = 7

DEFAULT_LIMIT = 300
"""Candidates a fetch aims for.

Deliberately above the 250 a finished pack wants. M4 promotes candidates by
matching them to USDA PLANTS, and unmatched taxa are dropped — a name
iNaturalist recognises is not always one USDA carries, especially for recently
split or renamed species. Fetching exactly 250 would mean discovering the
shortfall only after the expensive stage and re-fetching to cover it. Headroom
is cheap here and expensive later."""


def pool_path(state: str, work_dir: Path = DEFAULT_WORK_DIR) -> Path:
    """Where a state's candidate pool is written.

    Args:
        state: Region code, e.g. `"MI"`.
        work_dir: Directory holding intermediate build artefacts.

    Returns:
        The path, e.g. `work/candidates_MI.json`.

    Example:
        >>> pool_path("MI").as_posix()
        'work/candidates_MI.json'
    """
    return work_dir / f"candidates_{state.upper()}.json"


def _configure_logging(verbose: bool) -> None:
    """Send progress to stderr so stdout carries only the artefact."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    # pyinaturalist logs every request at INFO, which buries our cache-miss
    # lines — the ones that actually report what the run cost.
    logging.getLogger("pyinaturalist").setLevel(logging.WARNING)


app = typer.Typer(
    add_completion=False,
    help="Build Sift study packs.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Anchor the CLI as a command group so each verb keeps its name."""


@app.command()
def fetch(  # noqa: PLR0913 - a CLI verb's flags are its interface, not a parameter list
    *,
    domain: Annotated[str, typer.Option(help="Domain slug, e.g. 'plants'.")],
    state: Annotated[str, typer.Option(help="US state code, e.g. 'MI'.")],
    limit: Annotated[int, typer.Option(min=1, help="Candidates to aim for.")] = DEFAULT_LIMIT,
    cache_dir: Annotated[Path, typer.Option(help="Response cache.")] = DEFAULT_CACHE_DIR,
    work_dir: Annotated[Path, typer.Option(help="Where to write the pool.")] = DEFAULT_WORK_DIR,
    offline: Annotated[
        bool, typer.Option(help="Fail on a cache miss instead of fetching.")
    ] = False,
    keep_raw: Annotated[
        bool, typer.Option(help="Also write untouched responses to cache/raw/ for debugging.")
    ] = False,
    force: Annotated[
        bool, typer.Option(help="Break a stale fetch lock left by a killed process.")
    ] = False,
    verbose: Annotated[bool, typer.Option(help="Log progress to stderr.")] = True,
) -> None:
    """Fetch a candidate pool from iNaturalist and write it to `work/`.

    Re-running is cheap: every response is cached, so a second run makes no
    network calls and a run killed halfway through resumes by being run again.

    Args:
        domain: Which domain to fetch for.
        state: US state code the pool is scoped to.
        limit: How many candidates to aim for.
        cache_dir: Where cached API responses live.
        work_dir: Where the pool is written.
        offline: When set, a cache miss is an error rather than a request.
        keep_raw: Also keep untouched response bodies under `cache/raw/`.
        force: Break a stale lock. Only when no fetch is actually running.
        verbose: Log progress to stderr.

    Raises:
        typer.Exit: 2 for an unknown domain, 3 for a domain that is known but
            unimplemented, 5 for an iNaturalist or place-table failure, 7 when
            another fetch already holds the lock.

    Example:
        >>> from typer.testing import CliRunner
        >>> CliRunner().invoke(app, ["fetch", "--domain", "birbs", "--state", "MI"]).exit_code
        2
    """
    _configure_logging(verbose)
    try:
        resolved = resolve_domain(domain)
    except UnknownDomainError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_UNKNOWN_DOMAIN) from exc

    try:
        place_id = load_places().place_id_for(state)
    except InatError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INAT_ERROR) from exc

    # The lock is taken before the client is built, so a refused second fetch
    # has made no request and has not even touched the cache.
    try:
        with fetch_lock(work_dir, force=force):
            _run_fetch(
                InatClient(cache_dir, offline=offline, keep_raw=keep_raw),
                resolved,
                _FetchRequest(
                    state=state,
                    place_id=place_id,
                    limit=limit,
                    destination=pool_path(state, work_dir),
                ),
            )
    except FetchLockError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_LOCKED) from exc
    except NotImplementedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_DOMAIN_UNAVAILABLE) from exc
    except InatError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INAT_ERROR) from exc


@dataclass(frozen=True, slots=True)
class _FetchRequest:
    """What one fetch is for, bundled so the runner has a readable signature.

    Attributes:
        state: Region code the pool is scoped to.
        place_id: iNaturalist place ID for `state`.
        limit: How many candidates to aim for.
        destination: Where the pool is written.
    """

    state: str
    place_id: int
    limit: int
    destination: Path


def _run_fetch(client: InatClient, domain: TaxonDomain, request: _FetchRequest) -> None:
    """Run the fetch and write the pool. Called only while the lock is held."""
    pool = fetch_pool(client, domain, request.state.upper(), request.place_id, request.limit)

    request.destination.parent.mkdir(parents=True, exist_ok=True)
    request.destination.write_text(pool.model_dump_json(indent=2) + "\n", encoding="utf-8")

    typer.echo(f"wrote {request.destination}", err=True)
    typer.echo(
        f"{len(pool.candidates)} candidates, {len(pool.dropped)} dropped, {client.stats.summary()}",
        err=True,
    )


@app.command()
def stats(
    state: Annotated[str, typer.Option(help="US state code, e.g. 'MI'.")],
    work_dir: Annotated[Path, typer.Option(help="Where the pool lives.")] = DEFAULT_WORK_DIR,
    cache_dir: Annotated[Path, typer.Option(help="Cache to measure.")] = DEFAULT_CACHE_DIR,
) -> None:
    """Summarise a fetched candidate pool.

    Args:
        state: Which state's pool to describe.
        work_dir: Directory holding the pool.
        cache_dir: Response cache, measured for the on-disk size line.

    Raises:
        typer.Exit: 6 when no pool has been fetched for that state.

    Example:
        >>> from typer.testing import CliRunner
        >>> result = CliRunner().invoke(app, ["stats", "--state", "ZZ"])
        >>> result.exit_code
        6
    """
    source = pool_path(state, work_dir)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(
            f"error: no candidate pool at {source}. Run `sift-pack fetch --domain "
            f"plants --state {state}` first.",
            err=True,
        )
        raise typer.Exit(code=_EXIT_MISSING_POOL) from exc

    pool = CandidatePool.model_validate_json(text)
    typer.echo(f"pool: {source} ({pool.domain}, {pool.state}, place_id {pool.place_id})", err=True)
    typer.echo(summarise(pool, cache_dir).render())


@app.command()
def places(
    refresh: Annotated[
        bool, typer.Option(help="Re-resolve all 50 states against the live API.")
    ] = False,
    cache_dir: Annotated[Path, typer.Option(help="Response cache.")] = DEFAULT_CACHE_DIR,
    path: Annotated[Path, typer.Option(help="Place table location.")] = PLACES_PATH,
    verbose: Annotated[bool, typer.Option(help="Log progress to stderr.")] = True,
) -> None:
    """Show or regenerate the committed state-to-place-ID table.

    `--refresh` makes 50 live requests and is run by hand, never in CI: the
    answers change essentially never, and the table is committed precisely so
    that no build depends on autocomplete ranking.

    Args:
        refresh: Re-resolve every state and rewrite the table.
        cache_dir: Where cached API responses live.
        path: Where the table is read from and written to.
        verbose: Log progress to stderr.

    Raises:
        typer.Exit: 5 if the table cannot be read, or a state fails to resolve.

    Example:
        >>> from typer.testing import CliRunner
        >>> CliRunner().invoke(app, ["places"]).exit_code
        0
    """
    _configure_logging(verbose)
    try:
        table = refresh_places(InatClient(cache_dir), path) if refresh else load_places(path)
    except InatError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_INAT_ERROR) from exc

    typer.echo(f"{len(table.states)} states in {path}", err=True)
    for state in table.states:
        typer.echo(f"{state.code}\t{state.place_id}\t{state.name}")


@app.command()
def build(
    state: Annotated[str, typer.Option(help="US state code, e.g. 'MI'.")],
    work_dir: Annotated[Path, typer.Option(help="Where the pool lives.")] = DEFAULT_WORK_DIR,
) -> None:
    """Promote a candidate pool into a manifest. Not implemented yet.

    Promotion needs two things that do not exist at M2, and this command exits
    non-zero rather than emitting an empty manifest. An empty manifest would be
    a specific claim — "we tried to promote every candidate and none qualified"
    — and that claim would be false. Nothing tried.

    Args:
        state: Which state's pool to promote.
        work_dir: Directory holding the pool.

    Raises:
        typer.Exit: 4 always, until M3. 6 when there is no pool to promote.

    Example:
        >>> from typer.testing import CliRunner
        >>> CliRunner().invoke(app, ["build", "--state", "ZZ"]).exit_code
        6
    """
    source = pool_path(state, work_dir)
    if not source.exists():
        typer.echo(
            f"error: no candidate pool at {source}. Run `sift-pack fetch` first.",
            err=True,
        )
        raise typer.Exit(code=_EXIT_MISSING_POOL)

    pool = CandidatePool.model_validate_json(source.read_text(encoding="utf-8"))
    typer.echo(
        f"error: cannot build a manifest yet. {source} holds {len(pool.candidates)} "
        "candidates, but promotion requires two things M2 does not have:\n"
        "  1. a nativity claim per taxon, with a source — USDA PLANTS, wired in M3\n"
        "  2. sha256 and byte size per image, which only exist once the bytes are\n"
        "     fetched from the inaturalist-open-data bucket\n"
        "Emitting an empty manifest instead would claim promotion ran and rejected\n"
        "everything. It did not run.",
        err=True,
    )
    raise typer.Exit(code=_EXIT_NOT_YET_IMPLEMENTED)


def main() -> None:
    """Entry point for the `sift-pack` console script.

    Example:
        >>> callable(main)
        True
    """
    app()


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    sys.exit(app())
