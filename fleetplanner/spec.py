"""
MissionSpec: the typed contract between the language layer and the planner.

Tasks say what must happen; constraints say what must not. Times are wall-clock
"HH:MM" strings, converted to simulation steps using the world's start_time.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator

_TIME = r"^([01]\d|2[0-3]):[0-5]\d$"


class _Task(BaseModel):
    id: str
    deadline: str | None = Field(default=None, pattern=_TIME, description="Finish by HH:MM")
    priority: int = Field(default=1, ge=1, le=5, description="5 = most urgent")
    robot: str | None = Field(default=None, description="Pin to a specific robot")


class Transport(_Task):
    """Carry `quantity` totes from one location to another."""
    type: Literal["transport"] = "transport"
    from_location: str
    to_location: str
    quantity: int = Field(default=1, ge=1, le=50)


class Charge(_Task):
    """Send a robot to a charger until it reaches target_battery."""
    type: Literal["charge"] = "charge"
    target_battery: float = Field(default=90, gt=0, le=100)


class Park(_Task):
    """Send a robot to a location and leave it there."""
    type: Literal["park"] = "park"
    location: str


Task = Annotated[Union[Transport, Charge, Park], Field(discriminator="type")]


class _Window(BaseModel):
    start: str | None = Field(default=None, pattern=_TIME)
    end: str | None = Field(default=None, pattern=_TIME)


class ZoneClosed(_Window):
    """No robot may be inside the zone during the window."""
    type: Literal["zone_closed"] = "zone_closed"
    zone: str


class KeepClear(_Window):
    """Robots may pass through the zone but may not stop in it during the window."""
    type: Literal["keep_clear"] = "keep_clear"
    zone: str


class RobotUnavailable(BaseModel):
    """The robot must not move."""
    type: Literal["robot_unavailable"] = "robot_unavailable"
    robot: str


Constraint = Annotated[Union[ZoneClosed, KeepClear, RobotUnavailable], Field(discriminator="type")]


class MissionSpec(BaseModel):
    tasks: list[Task] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)

    @field_validator("tasks")
    @classmethod
    def unique_ids(cls, tasks: list) -> list:
        ids = [t.id for t in tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("task ids must be unique")
        return tasks

    def task(self, tid: str):
        return next(t for t in self.tasks if t.id == tid)
