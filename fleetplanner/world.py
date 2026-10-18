"""Warehouse world: grid, named locations, zones, robots, and time."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

Cell = tuple[int, int]
MOVES: dict[str, Cell] = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}


@dataclass(frozen=True)
class RobotConfig:
    id: str
    home: str
    battery: float
    capacity: int


@dataclass(frozen=True)
class BatteryModel:
    move_cost: float = 0.5
    idle_cost: float = 0.05
    charge_rate: float = 5.0
    reserve: float = 20.0


@dataclass
class World:
    name: str
    width: int
    height: int
    blocked: frozenset[Cell]
    locations: dict[str, Cell]
    zones: dict[str, frozenset[Cell]]
    robots: list[RobotConfig]
    battery: BatteryModel
    start_time: str = "00:00"
    step_seconds: int = 10
    grid_text: str = field(default="", repr=False)
    descriptions: dict[str, str] = field(default_factory=dict)
    conventions: list[str] = field(default_factory=list)

    # ── construction ───────────────────────────────────────────────────────
    @classmethod
    def load(cls, path: str | Path) -> "World":
        data = yaml.safe_load(Path(path).read_text())
        rows = [r for r in data["grid"].splitlines() if r.strip()]
        height, width = len(rows), max(len(r) for r in rows)
        blocked = frozenset(
            (x, y) for y, row in enumerate(rows) for x in range(width)
            if x >= len(row) or row[x] == "#"
        )
        zones = {
            name: frozenset((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                            if (x, y) not in blocked)
            for name, (x0, y0, x1, y1) in data.get("zones", {}).items()
        }
        world = cls(
            name=data["name"],
            width=width,
            height=height,
            blocked=blocked,
            locations={k: tuple(v) for k, v in data["locations"].items()},
            zones=zones,
            robots=[RobotConfig(**r) for r in data["robots"]],
            battery=BatteryModel(**data.get("battery", {})),
            start_time=data.get("start_time", "00:00"),
            step_seconds=data.get("step_seconds", 10),
            grid_text="\n".join(rows),
            descriptions=data.get("descriptions", {}),
            conventions=data.get("conventions", []),
        )
        world.check()
        return world

    def check(self) -> None:
        for name, cell in self.locations.items():
            if not self.is_free(cell):
                raise ValueError(f"location {name} {cell} is not a floor cell")
        for r in self.robots:
            if r.home not in self.locations:
                raise ValueError(f"robot {r.id} home {r.home!r} is not a location")
        for name in self.descriptions:
            if name not in self.locations and name not in self.zones:
                raise ValueError(f"description for unknown location or zone {name!r}")
        homes = [r.home for r in self.robots]
        if len(set(homes)) != len(homes):
            raise ValueError("robots must have distinct homes")

    # ── geometry ───────────────────────────────────────────────────────────
    def in_bounds(self, c: Cell) -> bool:
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height

    def is_free(self, c: Cell) -> bool:
        return self.in_bounds(c) and c not in self.blocked

    def neighbors(self, c: Cell) -> list[Cell]:
        return [n for dx, dy in MOVES.values() if self.is_free(n := (c[0] + dx, c[1] + dy))]

    @cached_property
    def free_cells(self) -> list[Cell]:
        return [(x, y) for y in range(self.height) for x in range(self.width) if self.is_free((x, y))]

    def distances_from(self, goal: Cell) -> dict[Cell, int]:
        """BFS distances to goal over free cells (ignores other robots and time)."""
        dist = {goal: 0}
        q = deque([goal])
        while q:
            c = q.popleft()
            for n in self.neighbors(c):
                if n not in dist:
                    dist[n] = dist[c] + 1
                    q.append(n)
        return dist

    @cached_property
    def _dist_cache(self) -> dict[Cell, dict[Cell, int]]:
        return {}

    def distance(self, a: Cell, b: Cell) -> int | None:
        if b not in self._dist_cache:
            self._dist_cache[b] = self.distances_from(b)
        return self._dist_cache[b].get(a)

    def zones_of(self, c: Cell) -> list[str]:
        return [z for z, cells in self.zones.items() if c in cells]

    @property
    def chargers(self) -> dict[str, Cell]:
        return {k: v for k, v in self.locations.items() if k.startswith("charger")}

    def robot(self, rid: str) -> RobotConfig:
        for r in self.robots:
            if r.id == rid:
                return r
        raise KeyError(rid)

    # ── time ───────────────────────────────────────────────────────────────
    def clock_to_step(self, hhmm: str) -> int:
        """'14:30' -> step index from start_time. Times before start are negative."""
        sh, sm = map(int, self.start_time.split(":"))
        h, m = map(int, hhmm.split(":"))
        return ((h * 60 + m) - (sh * 60 + sm)) * 60 // self.step_seconds

    def step_to_clock(self, step: int) -> str:
        sh, sm = map(int, self.start_time.split(":"))
        total = sh * 3600 + sm * 60 + step * self.step_seconds
        return f"{total // 3600 % 24:02d}:{total // 60 % 60:02d}:{total % 60:02d}"

    @property
    def end_time(self) -> str:
        """Nominal end of the planning horizon used for validation (two hours after start)."""
        h, m = map(int, self.start_time.split(":"))
        return f"{(h + 2) % 24:02d}:{m:02d}"

    def summary(self) -> dict:
        """JSON-friendly description, used in run logs and LLM prompts."""
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "grid": self.grid_text,
            "start_time": self.start_time,
            "step_seconds": self.step_seconds,
            "locations": {k: list(v) for k, v in self.locations.items()},
            "zones": {k: sorted(map(list, v)) for k, v in self.zones.items()},
            "robots": [vars(r) for r in self.robots],
            "battery": vars(self.battery),
        }
