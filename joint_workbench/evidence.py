"""把目录部件还原成逐次访问的证据，并据此评估比较单元。"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Mapping, Sequence

from .models import Card, Edition, Member, PartReceipt, UnitAssessment, CATEGORY_BY_STATE


# ---------------------------------------------------------------------------
# 访问证据
# ---------------------------------------------------------------------------


@dataclass
class VisitEvidence:
    station: str
    season: str
    visit: str
    plot: str | None
    team: str | None
    method: str | None
    outcome: str | None
    sections: tuple[str, ...]
    visit_part: str | None
    visit_received: bool
    presence_parts: tuple[str, ...]
    observation_parts: tuple[str, ...]
    confirmed: bool
    observations: tuple[dict[str, Any], ...]
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def cancelled(self) -> bool:
        return self.outcome == "cancelled"

    @property
    def claimed_visited(self) -> bool:
        return self.outcome == "visited"

    @property
    def zero(self) -> bool:
        return bool(self.observations) and all(o["zero"] for o in self.observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "season": self.season,
            "visit": self.visit,
            "plot": self.plot,
            "team": self.team,
            "method": self.method,
            "outcome": self.outcome,
            "sections": list(self.sections),
            "visit_part": self.visit_part,
            "visit_received": self.visit_received,
            "presence_parts": list(self.presence_parts),
            "observation_parts": list(self.observation_parts),
            "confirmed": self.confirmed,
            "observations": list(self.observations),
            "missing": list(self.missing),
            "notes": list(self.notes),
        }


class Inventory:
    """某一时刻（window）能核查的部件与访问事实。"""

    def __init__(self, campaigns: Sequence[dict[str, Any]], editions: Sequence[Edition],
                 parts: Mapping[tuple[str, str, str], PartReceipt], cards: Sequence[Card]):
        self.campaigns = list(campaigns)
        self.editions = {(e.station, e.season): e for e in editions}
        self.parts = dict(parts)
        self.cards = list(cards)
        self.visits: dict[tuple[str, str, str], VisitEvidence] = {}
        self.expected: dict[tuple[str, str], tuple[str, ...]] = {}
        self._build_expected()
        self._build_visits()

    # -- 目录/预期 -----------------------------------------------------
    def _build_expected(self) -> None:
        for campaign in self.campaigns:
            station = campaign["station"]
            for season, visits in campaign.get("seasons", {}).items():
                self.expected[(station, str(season))] = tuple(visits)

    def seasons(self, station: str) -> list[str]:
        found = [season for (st, season) in self.editions if st == station]
        return sorted(set(found))

    def latest_edition(self, station: str, season: str) -> Edition | None:
        return self.editions.get((station, season))

    def current_member(self, station: str, season: str, part_id: str) -> Member | None:
        edition = self.latest_edition(station, season)
        if not edition:
            return None
        return edition.member(part_id)

    def current_parts(self, station: str, season: str) -> list[Member]:
        edition = self.latest_edition(station, season)
        return list(edition.current_parts) if edition else []

    def receipt(self, station: str, season: str, part_id: str) -> PartReceipt | None:
        return self.parts.get((station, season, part_id))

    def verified(self, station: str, season: str, part_id: str) -> bool:
        member = self.current_member(station, season, part_id)
        receipt = self.receipt(station, season, part_id)
        return bool(member and receipt and receipt.verified and receipt.sha256 == member.sha256
                    and not member.withdrawn)

    # -- 访问 ---------------------------------------------------------
    def _build_visits(self) -> None:
        for (station, season), edition in self.editions.items():
            by_visit: dict[str, dict[str, list[Member]]] = {}
            replaced = {m.replaces for m in edition.current_parts if m.replaces}
            for member in edition.current_parts:
                # 已被今次目录中另一部件替代的旧件只保留历史用途，不再进入比较证据。
                if member.id in replaced:
                    continue
                by_visit.setdefault(member.visit, {"visit": [], "presence": [], "observation": []})
                by_visit[member.visit].setdefault(member.kind, []).append(member)
            expected = self.expected.get((station, season), ())
            for visit in sorted(set(by_visit) | set(expected)):
                slots = by_visit.get(visit, {"visit": [], "presence": [], "observation": []})
                self.visits[(station, season, visit)] = self._assemble(station, season, visit, slots)

    def _assemble(self, station: str, season: str, visit: str, slots: dict[str, list[Member]]) -> VisitEvidence:
        notes: list[str] = []
        missing: list[str] = []
        visit_members = slots.get("visit", [])
        visit_member = self._preferred(visit_members)
        content: dict[str, Any] = {}
        visit_part = None
        visit_received = False
        if visit_member is None:
            missing.append("visit")
        else:
            visit_part = visit_member.id
            receipt = self.receipt(station, season, visit_member.id)
            if not receipt or not receipt.verified or receipt.sha256 != visit_member.sha256:
                missing.append(f"visit:{visit_member.id}")
                notes.append(f"到访件 {visit_member.id} 尚未收到或摘要不符")
            else:
                visit_received = True
                content = receipt.content() or {}
                if not isinstance(content, dict):
                    content = {}
                    missing.append(f"visit:{visit_member.id}")

        presence_members = slots.get("presence", [])
        presence_parts: list[str] = []
        confirmed = False
        for member in presence_members:
            receipt = self.receipt(station, season, member.id)
            if not receipt or not receipt.verified or receipt.sha256 != member.sha256:
                notes.append(f"到访凭据 {member.id} 未收到或摘要不符")
                continue
            body = receipt.content()
            if isinstance(body, dict) and body.get("visit") == visit and body.get("confirmed") is True:
                confirmed = True
                presence_parts.append(member.id)
            else:
                notes.append(f"到访凭据 {member.id} 未确认同次访问")
        if not presence_members:
            missing.append("presence")

        observation_members = slots.get("observation", [])
        observation_parts: list[str] = []
        observations: list[dict[str, Any]] = []
        for member in self._ordered_observations(observation_members):
            receipt = self.receipt(station, season, member.id)
            if not receipt or not receipt.verified or receipt.sha256 != member.sha256:
                notes.append(f"观测表 {member.id} 未收到或摘要不符")
                continue
            body = receipt.content()
            if not isinstance(body, dict) or body.get("visit") != visit:
                notes.append(f"观测表 {member.id} 不是同次访问")
                continue
            sections = body.get("sections")
            if isinstance(sections, dict) and sections:
                counts = {str(k): int(v) for k, v in sections.items()}
                zero = all(value == 0 for value in counts.values())
                observations.append({
                    "part": member.id,
                    "kind": "sections",
                    "counts": counts,
                    "species_count": sum(counts.values()),
                    "zero": zero,
                    "covers": sorted(counts),
                })
            else:
                count = body.get("species_count")
                if not isinstance(count, int):
                    notes.append(f"观测表 {member.id} 没有可核查的计数")
                    continue
                observations.append({
                    "part": member.id,
                    "kind": "total",
                    "counts": None,
                    "species_count": count,
                    "zero": count == 0,
                    "covers": None,
                })
            observation_parts.append(member.id)
        if not observation_members:
            missing.append("observation")

        plot = content.get("plot")
        team = content.get("team")
        method = content.get("method")
        outcome = content.get("outcome")
        sections = tuple(content.get("sections") or ())
        for key in ("note", "statement", "reason", "remark"):
            if content.get(key):
                notes.append(f"站点说明：{content[key]}")
        if outcome == "cancelled":
            notes.append("取消到访，保留该次访问记录")
        if content and not plot:
            notes.append("到访件缺少样点")
        return VisitEvidence(
            station=station,
            season=season,
            visit=visit,
            plot=plot,
            team=team,
            method=method,
            outcome=outcome,
            sections=sections,
            visit_part=visit_part,
            visit_received=visit_received,
            presence_parts=tuple(presence_parts),
            observation_parts=tuple(observation_parts),
            confirmed=confirmed,
            observations=tuple(observations),
            missing=tuple(missing),
            notes=tuple(notes),
        )

    @staticmethod
    def _preferred(members: Sequence[Member]) -> Member | None:
        # 同一访问的 visit 部件正常只有一份；若有多份，优先最新引入的申报。
        return sorted(members, key=lambda m: (m.introduced, m.id))[-1] if members else None

    @staticmethod
    def _ordered_observations(members: Sequence[Member]) -> list[Member]:
        # 替代件（replaces）排在前面；同访问多个观测表时保留全部供核对，不把旧件当替代件。
        return sorted(members, key=lambda m: (0 if m.replaces else 1, -m.introduced, m.id))

    def visits_for(self, station: str, season: str, plot: str) -> list[VisitEvidence]:
        return [v for (st, se, _), v in sorted(self.visits.items())
                if st == station and se == season and v.plot == plot]

    def visits_of(self, station: str, season: str) -> list[VisitEvidence]:
        return [v for (st, se, _), v in sorted(self.visits.items()) if st == station and se == season]

    def plot_station(self, plot: str) -> str | None:
        stations = {v.station for v in self.visits.values() if v.plot == plot}
        if len(stations) == 1:
            return stations.pop()
        # 没有到访件时退回命名约定。
        if plot.startswith("N-"):
            return "north"
        if plot.startswith("S-"):
            return "south"
        return stations.pop() if stations else None

    def season_of_plot(self, station: str, plot: str) -> list[str]:
        return sorted({v.season for v in self.visits.values()
                       if v.station == station and v.plot == plot})


# ---------------------------------------------------------------------------
# 单元评估
# ---------------------------------------------------------------------------


@dataclass
class SideResult:
    season: str
    plot: str
    visits: list[VisitEvidence]
    valid: list[VisitEvidence] = field(default_factory=list)
    cancelled: list[VisitEvidence] = field(default_factory=list)
    method_mismatch: list[VisitEvidence] = field(default_factory=list)
    no_coverage: list[VisitEvidence] = field(default_factory=list)
    evidence_gap: list[VisitEvidence] = field(default_factory=list)
    missing_record: bool = False
    reasons: list[str] = field(default_factory=list)
    zero: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "season": self.season,
            "plot": self.plot,
            "visits": [v.to_dict() for v in self.visits],
            "valid": [v.visit for v in self.valid],
            "cancelled": [v.visit for v in self.cancelled],
            "method_mismatch": [v.visit for v in self.method_mismatch],
            "no_coverage": [v.visit for v in self.no_coverage],
            "evidence_gap": [v.visit for v in self.evidence_gap],
            "missing_record": self.missing_record,
            "reasons": list(self.reasons),
            "zero": dict(self.zero),
        }


def _covers(observation: dict[str, Any], required: Sequence[str], visit_sections: Sequence[str]) -> bool:
    if observation["kind"] == "sections":
        return set(required).issubset(observation["covers"] or ())
    # 无分区观测表：只有到访本身覆盖了要求的小区划，才可把计数用于该单元。
    return set(required).issubset(set(visit_sections))


def evaluate_side(inventory: Inventory, station: str, season: str, plot: str,
                  required: Sequence[str], method: str) -> SideResult:
    visits = inventory.visits_for(station, season, plot)
    result = SideResult(season=season, plot=plot, visits=visits)
    if not visits:
        result.missing_record = True
        result.reasons.append(f"{season} 没有 {plot} 的到访记录")
        return result
    for visit in visits:
        if visit.cancelled:
            result.cancelled.append(visit)
            result.reasons.append(f"{visit.visit} 明确取消，保留站点说明")
            continue
        if visit.method != method:
            result.method_mismatch.append(visit)
            result.reasons.append(f"{visit.visit} 方法 {visit.method!r} 与约定 {method!r} 不可比")
            continue
        if not visit.confirmed:
            result.evidence_gap.append(visit)
            result.reasons.append(f"{visit.visit} 没有独立到访凭据")
            continue
        if not visit.observations:
            result.evidence_gap.append(visit)
            result.reasons.append(f"{visit.visit} 没有有效观测表")
            continue
        usable = [o for o in visit.observations if _covers(o, required, visit.sections)]
        if not usable:
            result.no_coverage.append(visit)
            result.reasons.append(f"{visit.visit} 的观测没有覆盖小区划 {','.join(required)}")
            continue
        # 零值需要同次到访凭据 + 有效观测表 + 覆盖；前三项上面已核对。
        result.zero[visit.visit] = all(_observation_zero(o, required) for o in usable)
        result.valid.append(visit)
    if not result.valid and not (result.cancelled or result.method_mismatch):
        if result.evidence_gap and not result.no_coverage:
            result.reasons.append("只有空表或缺少凭据，按缺材料处理")
    return result


def _observation_zero(observation: Mapping[str, Any], required: Sequence[str]) -> bool:
    if observation["kind"] == "sections":
        counts = observation.get("counts") or {}
        return all(int(counts.get(section, 0)) == 0 for section in required)
    return int(observation.get("species_count", 0)) == 0


def assess_units(inventory: Inventory) -> list[UnitAssessment]:
    """按最新对应卡逐单元评估；同一旧访问不重复分给两个新样地。"""
    latest: list[Card] = []
    superseded = {card.supersedes for card in inventory.cards if card.supersedes}
    for card in inventory.cards:
        if card.id in superseded:
            continue
        latest.append(card)
    latest.sort(key=lambda card: (card.unit, card.id))

    assessments: list[UnitAssessment] = []
    for card in latest:
        assessments.append(assess_unit(inventory, card))
    _flag_shared_previous(assessments)
    return assessments


def assess_unit(inventory: Inventory, card: Card) -> UnitAssessment:
    station = inventory.plot_station(card.target) or inventory.plot_station(card.previous)
    if station is None:
        return UnitAssessment(unit=card.unit, card=card, state="missing_evidence",
                              category="missing_evidence", reason="无法从样点确定站点",
                              station=None)
    seasons = inventory.seasons(station)
    # 对应卡固定 previous 为旧期、target 为新期。优先采用样点实际出现过的工作季；
    # 样点只在单季出现时不能把两期压成一季，退回全站最早/最晚季并如实标出缺记录。
    prev_candidates = inventory.season_of_plot(station, card.previous)
    target_candidates = inventory.season_of_plot(station, card.target)
    prev_season = prev_candidates[0] if prev_candidates else (seasons[0] if seasons else "")
    target_season = target_candidates[-1] if target_candidates else (seasons[-1] if seasons else "")
    if prev_season == target_season and len(seasons) > 1:
        prev_season, target_season = seasons[0], seasons[-1]
    same = prev_season == target_season

    prev_side = evaluate_side(inventory, station, prev_season, card.previous, card.sections, card.method)
    target_side = evaluate_side(inventory, station, target_season, card.target, card.sections, card.method)

    notes: list[str] = []
    blockers: list[str] = []
    if card.state == "review":
        notes.append(f"对应卡 {card.id} 要求复勘，复勘完成前不重建比较")
    if card.state == "context_only":
        notes.append(f"对应卡 {card.id} 仅说明同一大范围，不能作两期比较")
    if same:
        blockers.append("两期落在同一季，无法比较")
    for part_id in card.requires:
        holder = next(
            (season for season in (target_season, prev_season, *inventory.seasons(station))
             if inventory.current_member(station, season, part_id) is not None),
            None,
        )
        if holder is None:
            blockers.append(f"缺少修订要求的部件 {part_id}（今次目录未声明）")
            continue
        member = inventory.current_member(station, holder, part_id)
        if member.withdrawn:
            blockers.append(f"修订要求的部件 {part_id} 已撤回")
        elif not inventory.verified(station, holder, part_id):
            blockers.append(f"修订要求的部件 {part_id} 尚未可核查")
        else:
            notes.append(f"修订要求的部件 {part_id} 已按今次身份核验")

    # 同一观测身份不能靠旧件代替：requires 只认目录里今次的部件 id。
    state, reason = _decide(card, prev_side, target_side, blockers)
    return UnitAssessment(
        unit=card.unit,
        card=card,
        state=state,
        category=CATEGORY_BY_STATE[state],
        reason=reason,
        station=station,
        previous=prev_side.to_dict(),
        target=target_side.to_dict(),
        blockers=blockers,
        notes=notes,
        alternates=_alternates(prev_side, target_side),
    )


def _decide(card: Card, prev: SideResult, target: SideResult, blockers: list[str]) -> tuple[str, str]:
    if card.state == "review":
        return "review", f"对应卡 {card.id} 要求复勘"
    if card.state == "context_only":
        return "incomparable", f"对应卡 {card.id} 仅表明地理关联"
    if blockers:
        return "missing_evidence", "；".join(blockers)
    if not prev.valid:
        if prev.cancelled and not prev.method_mismatch:
            return "not_visited", "上期计划访问明确取消"
        if prev.method_mismatch:
            return "incomparable", "上期到访方法与约定不一致"
        if prev.no_coverage:
            return "missing_evidence", "上期观测没有覆盖要求的小区划"
        if prev.missing_record:
            return "missing_evidence", f"上期没有 {prev.plot} 的到访申报，按缺材料处理"
        return "missing_evidence", _gap_reason(prev, "上期")
    if not target.valid:
        if target.cancelled and not target.method_mismatch:
            return "not_visited", "本期计划访问明确取消，未到访"
        if target.method_mismatch:
            return "incomparable", "本期到访方法与约定不一致"
        if target.no_coverage:
            return "missing_evidence", "本期观测没有覆盖要求的小区划"
        if target.missing_record:
            return "missing_evidence", f"本期没有 {target.plot} 的到访申报，按缺材料处理"
        return "missing_evidence", _gap_reason(target, "本期")
    if target.valid and all(target.zero.get(v.visit, False) for v in target.valid):
        return "true_unobserved", "两期均有有效到访与覆盖，本期观测为零"
    return "comparable", "两期均有可比的到访和有效观测"


def _gap_reason(side: SideResult, prefix: str) -> str:
    lacks_presence = any(not v.confirmed for v in side.evidence_gap)
    lacks_observation = any(not v.observations for v in side.evidence_gap)
    parts = []
    if lacks_presence:
        parts.append("独立到访凭据")
    if lacks_observation:
        parts.append("有效观测表")
    detail = "、".join(parts) if parts else "可用到访证据"
    visits = ",".join(v.visit for v in side.evidence_gap)
    return f"{prefix}缺少{detail}" + (f"（{visits}）" if visits else "")


def _alternates(prev: SideResult, target: SideResult) -> list[dict[str, Any]]:
    rows = []
    for side, label in ((prev, "previous"), (target, "target")):
        for visit in side.visits:
            rows.append({
                "side": label,
                "visit": visit.visit,
                "team": visit.team,
                "method": visit.method,
                "outcome": visit.outcome,
                "valid": visit in side.valid,
                "zero": side.zero.get(visit.visit, False),
                "observations": list(visit.observations),
                "notes": list(visit.notes),
            })
    return rows


def pairs_for(assessment: UnitAssessment) -> list[dict[str, Any]]:
    """返回实际支持比较的到访对（各自班组证据）。"""
    prev = assessment.previous or {}
    target = assessment.target or {}
    prev_visits = prev.get("visits", [])
    target_visits = target.get("visits", [])
    prev_valid = set(prev.get("valid", []))
    target_valid = set(target.get("valid", []))
    target_zero = target.get("zero", {})
    pairs = []
    for a, b in product(prev_visits, target_visits):
        if a["visit"] in prev_valid and b["visit"] in target_valid:
            pairs.append({
                "previous": a["visit"],
                "previous_team": a["team"],
                "target": b["visit"],
                "target_team": b["team"],
                "same_team": a["team"] == b["team"],
                "previous_observations": a["observations"],
                "target_observations": b["observations"],
                "target_zero": bool(target_zero.get(b["visit"], False)),
            })
    return pairs


def _flag_shared_previous(assessments: Sequence[UnitAssessment]) -> None:
    """同一旧样地的同一些部位不能同时支撑两个新样地。

    小区划不同（如 NE 与 NW）可以各用各的部位；重叠部位必须复勘分配。
    只改真正重叠的比较单元，完全不受影响的试点保持原结论。
    """
    users: dict[tuple[str, str, str], list[UnitAssessment]] = {}
    for assessment in assessments:
        if assessment.state not in ("comparable", "true_unobserved"):
            continue
        for visit in (assessment.previous or {}).get("valid", []):
            key = (assessment.previous.get("season", ""), assessment.previous.get("plot", ""), visit)
            users.setdefault(key, []).append(assessment)
    for key, group in users.items():
        units = {a.unit for a in group}
        if len(units) <= 1:
            continue
        conflicted: set[str] = set()
        for i, left in enumerate(group):
            for right in group[i + 1:]:
                if left.unit == right.unit:
                    continue
                if set(left.card.sections) & set(right.card.sections):
                    conflicted.update({left.unit, right.unit})
        for assessment in group:
            if assessment.unit not in conflicted:
                assessment.notes.append(
                    f"旧访问 {key[2]} 也出现在其他单元，但使用的小区划不同，仍按本卡部位比较")
                continue
            assessment.state = "review"
            assessment.category = CATEGORY_BY_STATE["review"]
            assessment.reason = (f"旧样地访问 {key[2]} 的重叠小区划同时被 "
                                 f"{', '.join(sorted(conflicted))} 使用，需复勘分配")
            assessment.blockers.append(f"旧访问 {key[2]} 的重叠部位不能同时支撑两个新样地")
            assessment.notes.append("保留各自合格访问，但不得重复计入两个比较单元")
