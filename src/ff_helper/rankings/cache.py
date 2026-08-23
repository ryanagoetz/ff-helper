"""On-disk snapshot of everything fetched from the network.

Draft day must not depend on the internet behaving. FantasyPros can redesign a page, an
API can rate-limit, hotel wifi can be hotel wifi. So the whole fetch step is separated
from the draft step: run ``scripts/fetch_rankings.py`` the day before, and the app then
starts from disk.

Raw inputs are cached rather than the finished blend, so the valuation can be recomputed
offline -- which is what makes ``scripts/replay.py`` able to backtest without a network.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ff_helper.config import cache_dir
from ff_helper.rankings.players import SourceRow
from ff_helper.yahoo.models import DraftAnalysis, YahooPlayer

# Version 2: SourceRow grew ecr_std (expert-rank spread, feeding projection variance).
# Version 3: YahooPlayer grew status_full, injury_note and percent_owned. ``_player_to_dict``
#   serializes with ``asdict``, so those land in the file and a v2 snapshot silently reads
#   them back as None/"" -- exactly the "a missing field can't read as None" case the
#   version exists to prevent. The in-season path treats that as "no ownership signal, no
#   injury detail" rather than as a stale file, which is the wrong answer quietly.
# Older snapshots are refused so the fields are actually present, not silently None --
# ``load`` degrades to "re-run scripts/fetch_rankings.py", which takes a minute.
SNAPSHOT_VERSION = 3


@dataclass
class Snapshot:
    """Everything the recommendation engine needs, minus the live draft board."""

    league_key: str
    fetched_at: float
    players: list[YahooPlayer] = field(default_factory=list)
    rows: list[SourceRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    version: int = SNAPSHOT_VERSION

    @property
    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600.0


def default_path(league_key: str) -> Path:
    safe = league_key.replace("/", "_")
    return cache_dir() / f"snapshot-{safe}.json"


def save(snapshot: Snapshot, path: Path | None = None) -> Path:
    target = path or default_path(snapshot.league_key)
    payload = {
        "version": snapshot.version,
        "league_key": snapshot.league_key,
        "fetched_at": snapshot.fetched_at,
        "notes": snapshot.notes,
        "players": [_player_to_dict(player) for player in snapshot.players],
        "rows": [asdict(row) for row in snapshot.rows],
    }
    target.write_text(json.dumps(payload))
    return target


def load(league_key: str, path: Path | None = None) -> Snapshot | None:
    target = path or default_path(league_key)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text())
    except json.JSONDecodeError:
        return None

    if payload.get("version") != SNAPSHOT_VERSION:
        # An older layout is not worth migrating; re-fetching takes a minute.
        return None

    return Snapshot(
        league_key=payload["league_key"],
        fetched_at=payload["fetched_at"],
        players=[_player_from_dict(entry) for entry in payload.get("players", [])],
        rows=[SourceRow(**entry) for entry in payload.get("rows", [])],
        notes=payload.get("notes", []),
    )


def _player_to_dict(player: YahooPlayer) -> dict:
    data = asdict(player)
    data["eligible_positions"] = list(player.eligible_positions)
    return data


def _player_from_dict(entry: dict) -> YahooPlayer:
    analysis = entry.get("draft_analysis") or {}
    return YahooPlayer(
        player_key=entry["player_key"],
        player_id=entry.get("player_id", ""),
        full_name=entry.get("full_name", ""),
        team_abbr=entry.get("team_abbr", ""),
        display_position=entry.get("display_position", ""),
        eligible_positions=tuple(entry.get("eligible_positions", ())),
        bye_week=entry.get("bye_week"),
        status=entry.get("status", ""),
        draft_analysis=DraftAnalysis(**analysis),
        # In-season fields. They default rather than being read positionally so this
        # function stays total, but that is belt-and-braces only: SNAPSHOT_VERSION went to
        # 3 when they were added, so ``load`` refuses any file old enough to be missing
        # them and these defaults are unreachable for a real v2 snapshot.
        status_full=entry.get("status_full", ""),
        injury_note=entry.get("injury_note", ""),
        percent_owned=entry.get("percent_owned"),
    )
