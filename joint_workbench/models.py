"""工作台使用的值对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Member:
    """目录声明的一份部件。"""

    station: str
    season: str
    edition: str
    id: str
    visit: str
    kind: str
    member: str
    sha256: str
    href: str | None = None
    introduced: int = 0
    state: str = "declared"
    replaces: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.station, self.season, self.id)

    @property
    def withdrawn(self) -> bool:
        return self.state == "withdrawn"

    @classmethod
    def from_catalog(cls, station: str, season: str, edition: str, raw: dict[str, Any]) -> "Member":
        return cls(
            station=station,
            season=season,
            edition=edition,
            id=raw["id"],
            visit=raw["visit"],
            kind=raw["kind"],
            member=raw["member"],
            sha256=raw["sha256"],
            href=raw.get("href"),
            introduced=int(raw.get("introduced", 0)),
            state=raw.get("state", "declared"),
            replaces=raw.get("replaces"),
        )


@dataclass(frozen=True)
class Edition:
    station: str
    season: str
    edition: str
    known: int
    state: str
    missing: tuple[str, ...]
    archive: str | None
    members: tuple[Member, ...]
    pages: int = 1
    restarts: int = 0

    def member(self, part_id: str) -> Member | None:
        return next((m for m in self.members if m.id == part_id), None)

    @property
    def sealed(self) -> bool:
        return self.state == "sealed"

    @property
    def current_parts(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if not m.withdrawn)

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "season": self.season,
            "edition": self.edition,
            "known": self.known,
            "state": self.state,
            "missing": list(self.missing),
            "archive": self.archive,
            "pages": self.pages,
            "restarts": self.restarts,
            "members": [m.__dict__ | {"key": list(m.key)} for m in self.members],
        }


@dataclass(frozen=True)
class Card:
    id: str
    unit: str
    previous: str
    target: str
    sections: tuple[str, ...]
    method: str
    state: str
    known: int = 0
    supersedes: str | None = None
    requires: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Card":
        return cls(
            id=raw["id"],
            unit=raw["unit"],
            previous=raw["previous"],
            target=raw["target"],
            sections=tuple(raw.get("sections", ())),
            method=raw["method"],
            state=raw["state"],
            known=int(raw.get("known", 0)),
            supersedes=raw.get("supersedes"),
            requires=tuple(raw.get("requires", ())),
            raw=dict(raw),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "unit": self.unit,
            "previous": self.previous,
            "target": self.target,
            "sections": list(self.sections),
            "method": self.method,
            "state": self.state,
            "known": self.known,
            "supersedes": self.supersedes,
            "requires": list(self.requires),
        }


@dataclass
class PartReceipt:
    """真正收到并核对过摘要的一份原件。"""

    station: str
    season: str
    part_id: str
    visit: str
    kind: str
    sha256: str
    data: bytes
    source: str  # object / archive
    window: int
    edition: str
    verified: bool = True
    note: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.station, self.season, self.part_id)

    def content(self) -> Any:
        import json

        try:
            return json.loads(self.data)
        except Exception:
            return None

    def to_dict(self, include_data: bool = False) -> dict[str, Any]:
        value = {
            "station": self.station,
            "season": self.season,
            "part_id": self.part_id,
            "visit": self.visit,
            "kind": self.kind,
            "sha256": self.sha256,
            "source": self.source,
            "window": self.window,
            "edition": self.edition,
            "verified": self.verified,
            "note": self.note,
            "bytes": len(self.data),
        }
        if include_data:
            import base64

            value["data"] = base64.b64encode(self.data).decode()
        return value


@dataclass
class Attempt:
    station: str
    season: str
    part_id: str
    window: int
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ArchiveSnapshot:
    archive_id: str
    station: str
    season: str
    edition: str
    window: int
    state: str
    members: dict[str, str]
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    delta: list[dict[str, Any]] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "station": self.station,
            "season": self.season,
            "edition": self.edition,
            "window": self.window,
            "state": self.state,
            "members": dict(self.members),
            "missing": list(self.missing),
            "extra": list(self.extra),
            "delta": list(self.delta),
            "digests": dict(self.digests),
            "note": self.note,
        }


# 评估员查看用的四类：可比、不可比、缺证、真实未观测；待复勘并入不可比。
# “没到”单列，不冒充实测，也不混进缺证。
CATEGORY_BY_STATE = {
    "comparable": "comparable",
    "true_unobserved": "true_unobserved",
    "incomparable": "incomparable",
    "review": "incomparable",
    "missing_evidence": "missing_evidence",
    "not_visited": "not_visited",
}


@dataclass
class UnitAssessment:
    unit: str
    card: Card
    state: str
    category: str
    reason: str
    station: str | None
    previous: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    alternates: list[dict[str, Any]] = field(default_factory=list)
    office: dict[str, Any] | None = None
    impact: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "card": self.card.to_dict(),
            "state": self.state,
            "category": self.category,
            "reason": self.reason,
            "station": self.station,
            "previous": self.previous,
            "target": self.target,
            "blockers": list(self.blockers),
            "notes": list(self.notes),
            "alternates": list(self.alternates),
            "office": self.office,
            "impact": self.impact,
        }


@dataclass
class SubmissionRecord:
    request_id: str
    window: int
    request: dict[str, Any]
    response: dict[str, Any] | None
    snapshot: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "window": self.window,
            "request": self.request,
            "response": self.response,
            "snapshot": self.snapshot,
        }
