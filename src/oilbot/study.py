"""Descriptive event studies with chronological episode splits, never random rows."""
from collections import defaultdict
from math import ceil
from statistics import mean

from .outcomes import HORIZONS
from .research import chronological_split


def partition_episodes(rows, boundaries):
    if any(not row["episode_verified"] for row in rows):
        raise ValueError("adjudicate every episode before evaluating partitions")
    # Later episode reassignments cannot put one incident into different folds.
    parent = {}
    def root(key):
        parent.setdefault(key, key)
        while parent[key] != key:
            key = parent[key]
        return key
    for row in rows:
        incident, episode = ("incident", row["incident_id"]), ("episode", row["episode_id"])
        parent[root(incident)] = root(episode)
    groups = {key: str(root(("episode", key))) for key in {r["episode_id"] for r in rows}}
    marked = [{**row, "original_episode_id": row["episode_id"], "episode_id": groups[row["episode_id"]]} for row in rows]
    purge = max(HORIZONS) + ceil(max((r["outcomes"]["assumptions"]["delay_ms"] for r in rows), default=0) / 1000)
    return chronological_split(marked, boundaries, purge_seconds=purge)


def event_study(rows, boundaries):
    partitions = partition_episodes(rows, boundaries)
    output = {"status": "DESCRIPTIVE_ONLY", "promotion": False, "partitions": {},
              "boundaries": boundaries,
              "purge_seconds": max(HORIZONS) + ceil(max((r["outcomes"]["assumptions"]["delay_ms"] for r in rows), default=0) / 1000),
              "limitations": "Hypothetical fills; overlapping positions not netted; no fitted edge or live authorization."}
    for name, group in partitions.items():
        summaries = {}
        for horizon in HORIZONS:
            for baseline in ("no_trade", "simple_event_direction", "price_momentum_only", "event_market_filter"):
                episodes = defaultdict(list)
                missing = 0
                for row in group:
                    direction = row["direction"]
                    momentum = row["features"]["returns_before_decision"]["30"]
                    side = (0 if baseline == "no_trade" else direction if baseline == "simple_event_direction"
                            else (0 if momentum is None else (momentum > 0) - (momentum < 0)) if baseline == "price_momentum_only"
                            else direction if row["decision"]["action"] == "RESEARCH_CANDIDATE" else 0)
                    value = row["outcomes"]["horizons"][str(horizon)]["instruments"][row["decision"]["execution_role"]]
                    pnl = value["long" if side == 1 else "short"]["net_pnl"] if side else "0"
                    if pnl is None:
                        missing += 1
                    else:
                        episodes[row["episode_id"]].append(float(pnl))
                summaries[f"{baseline}:{horizon}"] = {"episodes": len(episodes), "missing_outcomes": missing,
                    "equal_episode_mean_pnl": mean(mean(values) for values in episodes.values()) if episodes else None}
        output["partitions"][name] = {"rows": len(group), "episodes": len({r["episode_id"] for r in group}), "summaries": summaries}
    return output
