"""联合评估收件工作台核心。"""
from __future__ import annotations

import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import BadZipFile, ZipFile

from .errors import SourceError, SubmissionBlocked, WorkbenchError
from .evidence import Inventory, UnitAssessment, assess_units, pairs_for
from .models import ArchiveSnapshot, Attempt, Card, Edition, Member, PartReceipt, SubmissionRecord
from .source import SourceClient


ACCEPTABLE_STATES = ("comparable", "true_unobserved")


class JointWorkbench:
    """把站点档案、部件收件和办公室回条对到一起。"""

    def __init__(self, base: str, timeout: float = 5.0):
        self.source = SourceClient(base, timeout)
        self.window = 0
        self.campaigns: list[dict[str, Any]] = []
        self.cards: list[Card] = []
        self.editions: dict[tuple[str, str], Edition] = {}
        # 换版后旧整包的成员身份仍要能核对，保留已读目录历史。
        self.edition_history: dict[str, Edition] = {}
        self.edition_windows: dict[tuple[str, str], int] = {}
        self.parts: dict[tuple[str, str, str], PartReceipt] = {}
        self.attempts: list[Attempt] = []
        self.archives: dict[str, ArchiveSnapshot] = {}
        self.submissions: dict[str, SubmissionRecord] = {}
        self.office_receipts: list[dict[str, Any]] = []
        self.last_refresh: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 轮次和刷新
    # ------------------------------------------------------------------
    def advance(self, window: int) -> dict[str, Any]:
        if not isinstance(window, int) or isinstance(window, bool):
            raise WorkbenchError("window 必须是整数")
        if window < self.window:
            raise WorkbenchError("不能回拨 window")
        if window != self.window:
            self.source.advance(window)
            self.window = window
        return self.refresh()

    def refresh(self, station: str | None = None, season: str | None = None) -> dict[str, Any]:
        root = self.source.window()
        self.window = int(root["window"])
        self.campaigns = self.source.campaigns()
        self.cards = [Card.from_raw(row) for row in self.source.cards()]
        self.office_receipts = self.source.receipts()
        self.last_refresh = {"window": self.window, "archives_left": root.get("archives_left")}
        # 代次会随轮次替换；每次刷新都重读目录，避免用旧页拼出的清单。
        # 带 station/season 过滤时只重读这些目录，其余站的已读目录保留。
        if station or season:
            for key in [k for k in self.editions
                        if (not station or k[0] == station) and (not season or k[1] == str(season))]:
                self.editions.pop(key, None)
                self.edition_windows.pop(key, None)
        else:
            self.editions = {}
            self.edition_windows = {}
        editions = []
        for (st, se) in self._declared_pairs():
            if station and st != station:
                continue
            if season and str(se) != str(season):
                continue
            try:
                editions.append(self.load_edition(st, se))
            except SourceError as error:
                if error.status != 404:
                    raise
        return {
            "window": self.window,
            "archives_left": root.get("archives_left"),
            "editions": [e.to_dict() for e in editions],
            "cards": [card.to_dict() for card in self.cards],
        }

    def _declared_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for campaign in self.campaigns:
            station = campaign["station"]
            for season in campaign.get("seasons", {}):
                key = (station, str(season))
                if key not in pairs:
                    pairs.append(key)
        for key in self.editions:
            if key not in pairs:
                pairs.append(key)
        return sorted(pairs)

    def stations(self) -> list[str]:
        return sorted({station for campaign in self.campaigns for station in [campaign["station"]]})

    def seasons(self, station: str) -> list[str]:
        pairs = {season for (st, season) in self._declared_pairs() if st == station}
        return sorted(pairs)

    # ------------------------------------------------------------------
    # 目录：分页、代次替换、完整性
    # ------------------------------------------------------------------
    def load_edition(self, station: str, season: str) -> Edition:
        season = str(season)
        edition_name = None
        after = 0
        pages: list[dict[str, Any]] = []
        restarts = 0
        seen: set[str] = set()
        while True:
            status, body = self.source.catalog_page(station, season, edition_name, after)
            if status == 409:
                # 代次在翻页途中被替换：旧页拼出的清单没有完整性承诺，只能从新代次重来。
                restarts += 1
                edition_name = body["edition"]
                after = 0
                pages = []
                seen = set()
                if restarts > 8:
                    raise WorkbenchError("目录代次反复替换，无法得到完整清单")
                continue
            if status != 200:
                raise SourceError(status, body, "/catalog")
            if edition_name is None:
                edition_name = body["edition"]
            if body["edition"] != edition_name:
                restarts += 1
                edition_name = body["edition"]
                after = 0
                pages = []
                seen = set()
                continue
            for member in body["members"]:
                if member["id"] in seen:
                    raise WorkbenchError(f"目录 {edition_name} 重复声明部件 {member['id']}")
                seen.add(member["id"])
                pages.append(member)
            nxt = body.get("next")
            if nxt is None:
                edition = Edition(
                    station=station,
                    season=season,
                    edition=edition_name,
                    known=int(body["known"]),
                    state=body["state"],
                    missing=tuple(body.get("missing") or ()),
                    archive=body.get("archive"),
                    members=tuple(Member.from_catalog(station, season, edition_name, m) for m in pages),
                    pages=len(pages),
                    restarts=restarts,
                )
                self.editions[(station, season)] = edition
                self.edition_windows[(station, season)] = self.window
                if edition.archive:
                    self.edition_history[edition.archive.rsplit("/", 1)[-1]] = edition
                return edition
            if nxt <= after:
                raise WorkbenchError("目录分页没有前进")
            after = int(nxt)

    def catalog(self, station: str, season: str) -> Edition:
        key = (station, str(season))
        edition = self.editions.get(key)
        # 同一 window 内目录代次不会变；换轮次必须重读，封期以现版目录为准。
        if edition is None or self.edition_windows.get(key) != self.window:
            return self.load_edition(station, season)
        return edition

    # ------------------------------------------------------------------
    # 部件：按声明身份和摘要收取
    # ------------------------------------------------------------------
    def fetch_object(self, station: str, season: str, part_id: str) -> dict[str, Any]:
        season = str(season)
        edition = self.catalog(station, season)
        member = edition.member(part_id)
        if member is None:
            raise WorkbenchError(f"目录 {edition.edition} 没有部件 {part_id}")
        if member.withdrawn:
            self._attempt(station, season, part_id, "withdrawn", "目录已确认撤回")
            return {"part": part_id, "state": "withdrawn"}
        if not member.href:
            self._attempt(station, season, part_id, "no_href", "目录没有短期入口")
            return {"part": part_id, "state": "no_href"}
        status, raw, _ = self.source.object(member.href.rsplit("/", 1)[-1])
        if status == 404:
            self._attempt(station, season, part_id, "pending", "短期入口尚未开放")
            return {"part": part_id, "state": "pending"}
        if status == 410:
            self._attempt(station, season, part_id, "expired", "短期入口已关闭")
            return {"part": part_id, "state": "expired"}
        if status == 200:
            return self._store(member, raw, source="object")
        raise SourceError(status, raw, member.href)

    def _store(self, member: Member, raw: bytes, source: str) -> dict[str, Any]:
        digest = sha256(raw).hexdigest()
        key = member.key
        verified = digest == member.sha256
        existing = self.parts.get(key)
        if existing is not None:
            if existing.sha256 == digest:
                # 重复交包合并真正同一部件，保留最早可核查的那份。
                self._attempt(member.station, member.season, member.id, "duplicate",
                              f"与已收部件摘要相同，来源 {source}")
                return {"part": member.id, "state": "duplicate", "sha256": digest}
            self._attempt(member.station, member.season, member.id, "conflict",
                          f"后到摘要 {digest} 与先收 {existing.sha256} 不同，不覆盖")
            return {"part": member.id, "state": "conflict", "sha256": digest,
                    "kept": existing.sha256}
        receipt = PartReceipt(
            station=member.station,
            season=member.season,
            part_id=member.id,
            visit=member.visit,
            kind=member.kind,
            sha256=digest,
            data=raw,
            source=source,
            window=self.window,
            edition=member.edition,
            verified=verified,
            note="" if verified else "摘要与目录声明不符",
        )
        if verified:
            self.parts[key] = receipt
            self._attempt(member.station, member.season, member.id, "received", source)
            return {"part": member.id, "state": "received", "sha256": digest, "source": source}
        self._attempt(member.station, member.season, member.id, "digest_mismatch",
                      f"声明 {member.sha256} 实收 {digest}")
        return {"part": member.id, "state": "digest_mismatch", "sha256": digest,
                "declared": member.sha256}

    def _attempt(self, station: str, season: str, part_id: str, status: str, detail: str = "") -> None:
        self.attempts.append(Attempt(station, str(season), part_id, self.window, status, detail))

    # ------------------------------------------------------------------
    # 整包：每轮名额、包内核对、与逐次提交的差异
    # ------------------------------------------------------------------
    def fetch_archive(self, archive_id: str, force: bool = False) -> dict[str, Any]:
        # 已经开过的同一整包不重复占用本轮名额；只有目录换版才需要再开。
        cached = self.archives.get(archive_id)
        if cached is not None and not force:
            # 封期包不会在后续轮次改变内容；重复开包只会浪费本轮名额。
            return {"archive": archive_id, "state": "cached",
                    "detail": "已核对过该封期包，不重复领取",
                    "gap": self.archive_gap(archive_id),
                    "snapshot": cached.to_dict()}
        status, raw, headers = self.source.archive(archive_id)
        if status == 404:
            return {"archive": archive_id, "state": "pending"}
        if status == 429:
            return {"archive": archive_id, "state": "lane_busy",
                    "detail": "本轮整包名额已用尽，下一轮再领"}
        if status != 200:
            raise SourceError(status, raw, f"/archive/{archive_id}")
        edition = self._edition_for_archive(archive_id)
        if edition is None:
            return {"archive": archive_id, "state": "unmatched",
                    "detail": "没有目录把该包声明为整季档案"}
        try:
            with ZipFile(BytesIO(raw)) as pack:
                names = set(pack.namelist())
                members = {name: pack.read(name) for name in names}
        except (BadZipFile, OSError) as error:
            raise WorkbenchError(f"整包 {archive_id} 无法展开: {error}") from error
        result = self._ingest_archive(archive_id, edition, members)
        result["gap"] = self.archive_gap(archive_id)
        return result

    def _edition_for_archive(self, archive_id: str) -> Edition | None:
        for edition in self.editions.values():
            if edition.archive and edition.archive.endswith("/" + archive_id):
                return edition
        return self.edition_history.get(archive_id)

    def latest_edition_for(self, station: str, season: str) -> Edition | None:
        return self.editions.get((station, str(season)))

    def _ingest_archive(self, archive_id: str, edition: Edition, members: Mapping[str, bytes]) -> dict[str, Any]:
        declared = {m.member: m for m in edition.members}
        delta: list[dict[str, Any]] = []
        added: dict[str, str] = {}
        digests: dict[str, str] = {}
        missing: list[str] = []
        extra: list[str] = []
        for name, blob in sorted(members.items()):
            member = declared.get(name)
            if member is not None and member.withdrawn:
                delta.append({"member": name, "part": member.id, "state": "withdrawn",
                              "detail": "目录已撤回该部件，包内文件只留历史核对，不纳入今次收件"})
                continue
            if member is None:
                extra.append(name)
                delta.append({"member": name, "state": "extra", "detail": "目录未声明，未纳入收件"})
                continue
            digest = sha256(blob).hexdigest()
            digests[member.id] = digest
            before = self.parts.get(member.key)
            result = self._store(member, blob, source="archive")
            if result["state"] == "received" and before is None:
                added[member.id] = digest
                delta.append({"member": name, "part": member.id, "state": "filled",
                              "detail": "整包补上了此前未收到的声明部件"})
            elif result["state"] == "duplicate":
                delta.append({"member": name, "part": member.id, "state": "same",
                              "detail": "与已收部件摘要相同，合并"})
            elif result["state"] == "conflict":
                delta.append({"member": name, "part": member.id, "state": "conflict",
                              "detail": "与已收真实材料摘要不同，保留先收件"})
            elif result["state"] == "digest_mismatch":
                delta.append({"member": name, "part": member.id, "state": "digest_mismatch",
                              "detail": "摘要与目录声明不符，未纳入"})
        for name, member in declared.items():
            if member.withdrawn:
                if self.parts.get(member.key):
                    delta.append({"member": name, "part": member.id, "state": "withdrawn",
                                  "detail": "目录已撤回该部件；历史收件保留，不用于今次比较"})
                continue
            if name in members:
                continue
            missing.append(member.id)
            held = self.parts.get(member.key)
            if held:
                delta.append({"member": name, "part": member.id, "state": "retained",
                              "detail": "包内没有，但此前已收到可核查原件"})
            else:
                delta.append({"member": name, "part": member.id, "state": "short_term_loss",
                              "detail": "封期包未带该声明部件，且无先收原件，按短期传输缺失追收"})
        snapshot = ArchiveSnapshot(
            archive_id=archive_id,
            station=edition.station,
            season=edition.season,
            edition=edition.edition,
            window=self.window,
            state=edition.state,
            members=added,
            missing=tuple(missing),
            extra=tuple(extra),
            delta=delta,
            digests=digests,
            note="整包只证明其本站本季目录内实际带到的部件",
        )
        self.archives[archive_id] = snapshot
        return snapshot.to_dict()

    def reconcile_archive(self, archive_id: str) -> dict[str, Any]:
        """不重开整包，把后到的补交段与包内已核对成员逐件比较。"""
        snapshot = self.archives.get(archive_id)
        if snapshot is None:
            return {"archive": archive_id, "state": "not_opened", "rows": []}
        # 先按包所属代次核对，避免把后来换版新增的部件算成这个包漏交。
        edition = self.edition_history.get(archive_id)
        current = self.latest_edition_for(snapshot.station, snapshot.season)
        rows = []
        if edition is not None:
            for member in edition.current_parts:
                receipt = self.parts.get(member.key)
                in_pack = snapshot.digests.get(member.id)
                if receipt is None:
                    state = "still_missing" if in_pack is None else "in_pack_only"
                elif in_pack is None:
                    state = "supplement_outside_pack"
                elif in_pack == receipt.sha256:
                    state = "same"
                else:
                    state = "conflict"
                rows.append({
                    "part": member.id,
                    "visit": member.visit,
                    "kind": member.kind,
                    "in_pack": in_pack is not None,
                    "received": receipt is not None,
                    "state": state,
                })
        newer = []
        if current is not None and edition is not None and current.edition != edition.edition:
            pack_ids = {m.id for m in edition.members}
            newer = [m.id for m in current.current_parts if m.id not in pack_ids]
        return {
            "archive": archive_id,
            "edition": snapshot.edition,
            "current_edition": current.edition if current else snapshot.edition,
            "newer_edition_parts": newer,
            "state": "reconciled",
            "rows": rows,
            "summary": {
                "same": sum(1 for r in rows if r["state"] == "same"),
                "supplement_outside_pack": sum(1 for r in rows if r["state"] == "supplement_outside_pack"),
                "still_missing": sum(1 for r in rows if r["state"] == "still_missing"),
                "in_pack_only": sum(1 for r in rows if r["state"] == "in_pack_only"),
                "conflict": sum(1 for r in rows if r["state"] == "conflict"),
            },
        }

    def archive_gap(self, archive_id: str) -> dict[str, Any]:
        """封期包与先前逐次提交的差异分类。"""
        snapshot = self.archives.get(archive_id)
        if snapshot is None:
            return {"archive": archive_id, "state": "not_opened"}
        filled = [row["part"] for row in snapshot.delta if row["state"] == "filled"]
        withdrawn = [row["part"] for row in snapshot.delta if row["state"] == "withdrawn"]
        short = [row["part"] for row in snapshot.delta if row["state"] == "short_term_loss"]
        conflicts = [row["part"] for row in snapshot.delta if row["state"] == "conflict"]
        retained = [row["part"] for row in snapshot.delta if row["state"] == "retained"]
        return {
            "archive": archive_id,
            "station": snapshot.station,
            "season": snapshot.season,
            "edition": snapshot.edition,
            "state": snapshot.state,
            "filled": filled,
            "withdrawn": withdrawn,
            "short_term_loss": short,
            "conflicts": conflicts,
            "retained": retained,
            "verdict": self._archive_verdict(filled, withdrawn, short, conflicts, retained),
            "delta": list(snapshot.delta),
        }

    @staticmethod
    def _archive_verdict(filled, withdrawn, short, conflicts, retained=()) -> str:
        flags = []
        if filled:
            flags.append("补齐缺件")
        if withdrawn:
            flags.append("撤回原件")
        if short:
            flags.append("短期传输缺失")
        if conflicts:
            flags.append("摘要冲突")
        if retained:
            flags.append("包内缺件但先收原件保留")
        return "、".join(flags) if flags else "与先前逐次提交一致"

    # ------------------------------------------------------------------
    # 评估
    # ------------------------------------------------------------------
    def inventory(self) -> Inventory:
        # 只把当前代次目录中仍有身份、且摘要对得上的部件交给评估。
        return Inventory(self.campaigns, list(self.editions.values()), self.parts, self.cards)

    def assessments(self) -> list[UnitAssessment]:
        rows = assess_units(self.inventory())
        accepted = self.accepted_units()
        receipts = self.receipt_history()
        for row in rows:
            row.office = accepted.get(row.unit)
            row.impact = self._impact_for(row, receipts)
        return rows

    def assessment(self, unit: str) -> UnitAssessment:
        for row in self.assessments():
            if row.unit == unit:
                return row
        raise WorkbenchError(f"没有比较单元 {unit}")

    def evaluator_view(self, unit: str | None = None, batch: str | None = None,
                       plot: str | None = None) -> dict[str, Any]:
        rows = self.assessments()
        if unit:
            rows = [row for row in rows if row.unit == unit]
        if batch:
            rows = [row for row in rows if batch in self._batch_keys(row)]
        if plot:
            rows = [row for row in rows if plot in (row.card.previous, row.card.target)]
        return {
            "window": self.window,
            "units": [self._unit_report(row) for row in rows],
            "summary": self._summary(rows),
        }

    def _unit_report(self, row: UnitAssessment) -> dict[str, Any]:
        value = row.to_dict()
        value["pairs"] = pairs_for(row)
        value["materials"] = self._materials_for(row)
        return value

    def _materials_for(self, row: UnitAssessment) -> list[str]:
        found: list[str] = []
        for side in (row.previous, row.target):
            if not side:
                continue
            season = str(side.get("season", ""))
            for visit in side.get("visits", []):
                if visit.get("visit_part") and self._verified_part(row.station, season, visit["visit_part"]):
                    found.append(visit["visit_part"])
                for part in visit.get("presence_parts", []):
                    if self._verified_part(row.station, season, part):
                        found.append(part)
                for part in visit.get("observation_parts", []):
                    if self._verified_part(row.station, season, part):
                        found.append(part)
        for part_id in row.card.requires:
            if self._verified_part_any_season(row.station, part_id):
                found.append(part_id)
        return sorted(set(found))

    def _verified_part_any_season(self, station: str | None, part_id: str) -> bool:
        if not station:
            return False
        return any(self._verified_part(station, season, part_id)
                   for season in self.seasons(station))

    def _verified_part(self, station: str | None, season: str, part_id: str) -> bool:
        if not station:
            return False
        member = self.current_member(station, season, part_id)
        receipt = self.parts.get((station, season, part_id))
        return bool(member and not member.withdrawn and receipt and receipt.verified
                    and receipt.sha256 == member.sha256)

    @staticmethod
    def _batch_keys(row: UnitAssessment) -> set[str]:
        keys: set[str] = set()
        explicit = row.card.raw.get("batch")
        if explicit:
            keys.add(str(explicit))
        if row.station:
            keys.add(row.station)
            for side in (row.previous, row.target):
                if side and side.get("season"):
                    keys.add(f"{row.station}/{side['season']}")
        return keys

    @staticmethod
    def _summary(rows: Sequence[UnitAssessment]) -> dict[str, Any]:
        summary = {"comparable": 0, "true_unobserved": 0, "incomparable": 0,
                   "missing_evidence": 0, "not_visited": 0}
        states: dict[str, int] = {}
        for row in rows:
            summary[row.category] = summary.get(row.category, 0) + 1
            states[row.state] = states.get(row.state, 0) + 1
        summary["states"] = states
        return summary

    def ready_units(self) -> list[dict[str, Any]]:
        """已经具备依据、且办公室尚未受理的单元；评估员可以就此继续送审。"""
        accepted = self.accepted_units()
        rows = []
        for row in self.assessments():
            if row.state not in ACCEPTABLE_STATES or row.unit in accepted:
                continue
            missing = [p for p in row.card.requires if p not in self._materials_for(row)]
            if missing:
                continue
            rows.append({"unit": row.unit, "card": row.card.id, "state": row.state,
                         "station": row.station, "materials": self._materials_for(row)})
        return rows

    def supply_missing(self, station: str, season: str | None = None) -> list[dict[str, Any]]:
        """按清点单把当前还能领到的声明部件补收一遍。"""
        results = []
        for name in self.seasons(station):
            if season and name != str(season):
                continue
            edition = self.catalog(station, name)
            for item in self._chase_list(edition):
                try:
                    outcome = self.fetch_object(station, name, item["part"])
                except SourceError as error:
                    outcome = {"part": item["part"], "state": f"error:{error.status}"}
                results.append({"station": station, "season": name, **outcome})
        return results

    def committee_view(self) -> dict[str, Any]:
        """委员会三问：哪对复测真的做过、哪处没到、哪处按计划找过但未观察到。"""
        repeated, absent, unobserved, unresolved = [], [], [], []
        for row in self.assessments():
            report = self._unit_report(row)
            # 真实未观测也是有效复测，但委员会要单列"按计划找过、结果为零"。
            if row.state == "true_unobserved":
                unobserved.append({
                    "unit": row.unit,
                    "card": row.card.id,
                    "station": row.station,
                    "pairs": report["pairs"],
                    "materials": report["materials"],
                })
            elif row.state in ACCEPTABLE_STATES and report["pairs"]:
                repeated.append({
                    "unit": row.unit,
                    "card": row.card.id,
                    "station": row.station,
                    "pairs": report["pairs"],
                    "state": row.state,
                    "materials": report["materials"],
                })
            elif row.state == "not_visited":
                absent.append({
                    "unit": row.unit,
                    "card": row.card.id,
                    "station": row.station,
                    "reason": row.reason,
                    "statements": [n for side in (row.target, row.previous) if side
                                   for v in side.get("visits", []) for n in v.get("notes", [])],
                })
            elif row.state == "incomparable":
                unresolved.append({"unit": row.unit, "card": row.card.id, "state": row.state,
                                   "reason": row.reason})
            else:
                unresolved.append({"unit": row.unit, "card": row.card.id, "state": row.state,
                                   "reason": row.reason, "blockers": list(row.blockers)})
        return {
            "window": self.window,
            "repeated": repeated,
            "not_visited": absent,
            "true_unobserved": unobserved,
            "unresolved": unresolved,
        }

    def field_reconciliation(self, station: str) -> dict[str, Any]:
        """把一个站两轮的外业事实按样点对齐，先确认收全再谈比较。"""
        inventory = self.inventory()
        rows = []
        for season in self.seasons(station):
            for visit in inventory.visits_of(station, season):
                rows.append({
                    "season": season,
                    "visit": visit.visit,
                    "plot": visit.plot,
                    "team": visit.team,
                    "method": visit.method,
                    "outcome": visit.outcome,
                    "sections": list(visit.sections),
                    "record": visit.visit_received,
                    "presence": visit.confirmed,
                    "observation": len(visit.observations),
                    "cancelled": visit.cancelled,
                    "gaps": [gap for gap in ("visit", "presence", "observation")
                             if gap in visit.missing or (gap == "visit" and not visit.visit_received)
                             or (gap == "presence" and not visit.confirmed and not visit.cancelled)
                             or (gap == "observation" and not visit.observations and not visit.cancelled)],
                    "notes": list(visit.notes),
                })
        return {"station": station, "window": self.window, "visits": rows}

    # ------------------------------------------------------------------
    # 档案员视角：还须追哪个目录、包、部件
    # ------------------------------------------------------------------
    def archivist_view(self, station: str | None = None) -> dict[str, Any]:
        inventory = self.inventory()
        stations = [station] if station else self.stations()
        output = []
        for name in stations:
            seasons = []
            for season in self.seasons(name):
                try:
                    edition = self.catalog(name, season)
                except SourceError as error:
                    if error.status == 404:
                        continue
                    raise
                chase = self._chase_list(edition)
                visits = inventory.visits_of(name, season)
                expected = list(inventory.expected.get((name, season), ()))
                recorded = {v.visit for v in visits if v.visit_part}
                seasons.append({
                    "season": season,
                    "edition": edition.edition,
                    "state": edition.state,
                    "archive": edition.archive,
                    "archive_gap": self.archive_gap(edition.archive.rsplit("/", 1)[-1])
                    if edition.archive else None,
                    "declared_missing_visits": list(edition.missing),
                    "expected_visits": expected,
                    "visits_without_record": [v for v in expected if v not in recorded],
                    "visits_without_presence": [v.visit for v in visits if not v.confirmed and not v.cancelled],
                    "chase": chase,
                })
            output.append({"station": name, "seasons": seasons})
        return {"window": self.window, "stations": output}

    def _chase_list(self, edition: Edition) -> list[dict[str, Any]]:
        chase = []
        for member in edition.members:
            if member.withdrawn:
                continue
            receipt = self.parts.get(member.key)
            last = next((a for a in reversed(self.attempts)
                         if (a.station, a.season, a.part_id) == member.key), None)
            if receipt:
                continue
            chase.append({
                "part": member.id,
                "visit": member.visit,
                "kind": member.kind,
                "member": member.member,
                "href": member.href,
                "edition": edition.edition,
                "archive": edition.archive,
                "last_state": last.status if last else "not_attempted",
                "last_detail": last.detail if last else "尚未领取",
            })
        return chase

    # ------------------------------------------------------------------
    # 办公室回条与送审
    # ------------------------------------------------------------------
    def receipt_history(self) -> list[dict[str, Any]]:
        return list(self.office_receipts)

    def accepted_units(self) -> dict[str, dict[str, Any]]:
        accepted: dict[str, dict[str, Any]] = {}
        # 按回条轮次稳定排序后依序应用受理/撤下，最终状态取最后一次操作。
        for receipt in sorted(self.office_receipts, key=lambda row: row.get("known", 0)):
            for row in receipt.get("units", []):
                if row.get("state") == "accepted":
                    accepted[row["unit"]] = {
                        "receipt": receipt["id"],
                        "materials": list(row.get("materials", [])),
                        "card": row.get("card"),
                        "known": receipt.get("known"),
                    }
                elif row.get("state") == "withdrawn" and row["unit"] in accepted:
                    accepted.pop(row["unit"], None)
        return accepted

    def _impact_for(self, row: UnitAssessment, receipts: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        current = self.accepted_units().get(row.unit)
        if not current:
            return None
        history = self.submissions.get(current["receipt"])
        snapshot = history.snapshot.get("units", {}).get(row.unit) if history else None
        if snapshot is None:
            # 重启后仍能从办公室原回条还原送审资料；结论按原卡在当时的状态解释。
            snapshot = {
                "state": None,
                "card": current.get("card"),
                "materials": list(current.get("materials", [])),
                "conclusion": f"回条 {current['receipt']} 按卡 {current.get('card')} 受理",
            }
        changed = []
        if snapshot.get("state") and snapshot["state"] != row.state:
            changed.append(f"结论由 {snapshot['state']} 变为 {row.state}")
        if snapshot.get("card") and snapshot["card"] != row.card.id:
            changed.append(f"对应卡由 {snapshot['card']} 变为 {row.card.id}")
        for part_id in snapshot.get("materials", []):
            if not self._verified_anywhere(row.station, part_id):
                changed.append(f"原送审部件 {part_id} 不再可核查")
        if row.state not in ACCEPTABLE_STATES:
            changed.append(f"当前依据不再支持原结论（{row.reason}）")
        if not changed:
            return None
        old_card = snapshot.get("card")
        conclusions = [snapshot.get("conclusion")] if snapshot.get("conclusion") else []
        if old_card and old_card != row.card.id:
            conclusions.append(f"不再按对应卡 {old_card} 主张该单元可比")
        return {
            "unit": row.unit,
            "receipt": current["receipt"],
            "status": "office_confirmation_pending",
            "changes": changed,
            "original_basis": snapshot,
            "withdraw_conclusions": conclusions,
            "note": "办公室确认撤回前，原批仍按送审时的依据解释；重送只处理受影响单元",
        }

    def current_member(self, station: str, season: str, part_id: str) -> Member | None:
        edition = self.editions.get((station, str(season)))
        if edition is None:
            try:
                edition = self.catalog(station, season)
            except SourceError:
                return None
        return edition.member(part_id)

    def _verified_anywhere(self, station: str | None, part_id: str) -> bool:
        # 只有当前目录仍承认、且摘要对得上的部件才算可核查；撤回件只保留历史用途。
        pairs = [(station, season) for season in self.seasons(station)] if station else [
            (st, se) for (st, se) in self.editions
        ]
        for st, se in pairs:
            member = self.current_member(st, se, part_id)
            receipt = self.parts.get((st, se, part_id))
            if member and not member.withdrawn and receipt and receipt.verified \
                    and receipt.sha256 == member.sha256:
                return True
        return False

    def prepare_submission(self, request_id: str, units: Sequence[str],
                           card_overrides: Mapping[str, str] | None = None,
                           accept_legacy: bool = False) -> dict[str, Any]:
        """按当前依据和办公室接收范围组装一次送审。"""
        assessments = {row.unit: row for row in self.assessments()}
        accepted = self.accepted_units()
        overrides = dict(card_overrides or {})
        rows = []
        problems: list[dict[str, Any]] = []
        for unit in units:
            row = assessments.get(unit)
            if row is None:
                problems.append({"unit": unit, "problem": "没有该比较单元"})
                continue
            if unit in accepted and not overrides.get(unit) and not accept_legacy:
                problems.append({"unit": unit, "problem": f"办公室已受理（{accepted[unit]['receipt']}），"
                                                          "须先按原回条撤下或只重送受影响单元"})
                continue
            if accept_legacy and unit in accepted:
                # 办公室已按旧卡受理：原批仍按送审时依据解释，只有受影响单元才需撤下重送。
                if not row.impact:
                    problems.append({"unit": unit, "problem": "原受理未受影响，保持原批即可"})
                    continue
            if row.state not in ACCEPTABLE_STATES:
                problems.append({"unit": unit, "problem": f"当前为 {row.state}：{row.reason}"})
                continue
            card = overrides.get(unit, row.card.id)
            materials = self._materials_for(row)
            missing = [part for part in row.card.requires if part not in materials]
            if missing:
                problems.append({"unit": unit, "problem": "缺少修订要求的部件",
                                 "missing": missing})
                continue
            rows.append({
                "unit": unit,
                "action": "submit",
                "card": card,
                "materials": materials,
                "state_at_submission": row.state,
                "conclusion": row.reason,
                # 不能把样点名放进 previous：办公室用它指先前回条编号。
                "target_plot": row.card.target,
                "previous_plot": row.card.previous,
            })
        return {"receipt_request": request_id, "units": rows, "problems": problems}

    def submit(self, request_id: str, units: Sequence[str],
               card_overrides: Mapping[str, str] | None = None,
               allow_partial: bool = True,
               accept_legacy: bool = False) -> dict[str, Any]:
        # 同一提交编号必须对应原内容；重放先于重新组装，避免已受理单元被挡在外面。
        prior = self._known_submission(request_id)
        if prior is not None:
            original_units = [row["unit"] for row in prior.get("source", {}).get("units", [])]
            if sorted(original_units) != sorted(set(units)):
                raise SubmissionBlocked("提交编号已用于不同内容", [{"request": request_id,
                                                                   "original_units": original_units}])
            return {"submission": prior, "prepared": None, "replayed": True}
        prepared = self.prepare_submission(request_id, units, card_overrides, accept_legacy)
        if prepared["problems"] and not allow_partial:
            raise SubmissionBlocked("送审范围含未具备依据或被办公室挡住的单元",
                                    prepared["problems"])
        if not prepared["units"]:
            raise SubmissionBlocked("没有可送审单元", prepared["problems"])
        payload = {"receipt_request": request_id, "units": prepared["units"]}
        snapshot = {
            "window": self.window,
            "units": {
                row["unit"]: {
                    "state": row["state_at_submission"],
                    "card": row["card"],
                    "materials": row["materials"],
                    "conclusion": row["conclusion"],
                    "previous": row["previous_plot"],
                    "target": row["target_plot"],
                } for row in prepared["units"]
            },
        }
        status, body = self.source.submit(payload)
        if status == 409:
            raise SubmissionBlocked("办公室以同一编号保存了不同内容，未覆盖", [body])
        if status not in (200, 201):
            raise SourceError(status, body, "/office/submit")
        record = SubmissionRecord(request_id, self.window, payload, body, snapshot)
        self.submissions[request_id] = record
        self.office_receipts = self.source.receipts()
        return {"submission": body, "prepared": prepared}

    def evidence_summary(self, unit: str) -> dict[str, Any]:
        """把一个单元的结论拆成站点可补交的事实清单。"""
        row = self.assessment(unit)
        missing: list[str] = []
        for side in (row.previous, row.target):
            if not side:
                continue
            for key in ("evidence_gap", "no_coverage"):
                missing.extend(f"{side['season']}:{visit}:{key}" for visit in side.get(key, []))
            for visit in side.get("visits", []):
                for note in visit.get("notes", []):
                    missing.append(f"{side['season']}:{visit['visit']}:{note}")
        # 待追的声明部件：评估已点名，但当前目录里还没有可核查原件。
        chase = []
        if row.station:
            for part_id in row.card.requires:
                for season in self.seasons(row.station):
                    edition = self.catalog(row.station, season)
                    member = edition.member(part_id)
                    if member and not member.withdrawn and not self._verified_part(
                            row.station, season, part_id):
                        chase.append({"season": season, "part": part_id,
                                      "edition": edition.edition,
                                      "archive": edition.archive})
                        break
        return {
            "unit": unit,
            "state": row.state,
            "category": row.category,
            "reason": row.reason,
            "blockers": list(row.blockers),
            "missing_evidence": missing,
            "chase_parts": chase,
            "pairs": pairs_for(row),
            "materials": self._materials_for(row),
            "pairs_expected": row.state in ACCEPTABLE_STATES,
        }

    def _known_submission(self, request_id: str) -> dict[str, Any] | None:
        for receipt in self.office_receipts:
            if receipt.get("id") == request_id:
                return receipt
        record = self.submissions.get(request_id)
        return record.response if record else None

    def withdraw_changed(self, request_id: str, unit: str, previous_receipt: str,
                         card_overrides: Mapping[str, str] | None = None) -> dict[str, Any]:
        """只撤下受影响的已受理单元，带上原送审依据。"""
        assessment = self.assessment(unit)
        history = self.submissions.get(previous_receipt)
        snapshot = history.snapshot.get("units", {}).get(unit) if history else None
        if snapshot is None:
            # 兼容重启后的工作台：从办公室原回条里取材料。
            for receipt in self.office_receipts:
                if receipt.get("id") != previous_receipt:
                    continue
                for row in receipt.get("units", []):
                    if row.get("unit") == unit and row.get("state") == "accepted":
                        snapshot = {
                            "state": "accepted",
                            "card": row.get("card"),
                            "materials": row.get("materials", []),
                            "conclusion": f"原回条 {previous_receipt} 受理",
                        }
        if snapshot is None:
            raise SubmissionBlocked("找不到该单元的已受理依据", [{"unit": unit,
                                                                 "receipt": previous_receipt}])
        row = {
            "unit": unit,
            "action": "withdraw",
            "previous": previous_receipt,
            "materials": snapshot.get("materials", []),
        }
        payload = {"receipt_request": request_id, "units": [row]}
        status, body = self.source.submit(payload)
        if status != 200:
            raise SourceError(status, body, "/office/submit")
        record = SubmissionRecord(request_id, self.window, payload, body,
                                  {"window": self.window,
                                   "units": {unit: {**snapshot, "state": "withdrawn",
                                                    "withdrawn_by": request_id}}})
        self.submissions[request_id] = record
        self.office_receipts = self.source.receipts()
        return {"submission": body, "withdrew": row}

    def resubmit(self, request_id: str, unit: str, previous_receipt: str,
                 card_overrides: Mapping[str, str] | None = None) -> dict[str, Any]:
        """只重送受影响单元：先按原回条撤下同一单元，再用当前依据重送。"""
        assessment = self.assessment(unit)
        if assessment.state not in ACCEPTABLE_STATES:
            raise SubmissionBlocked(f"{unit} 当前为 {assessment.state}，还不能重送",
                                    [{"unit": unit, "reason": assessment.reason}])
        if assessment.impact is None:
            raise SubmissionBlocked(f"{unit} 的原受理未受影响，保持原批即可",
                                    [{"unit": unit}])
        withdraw = self.withdraw_changed(request_id + "-W", unit, previous_receipt, card_overrides)
        try:
            submit = self.submit(request_id, [unit], card_overrides, allow_partial=False)
        except SubmissionBlocked:
            # 撤下已生效；重送失败时明确交代，避免误以为原批仍在。
            raise
        return {"withdraw": withdraw, "submit": submit}

    # ------------------------------------------------------------------
    # 清点单
    # ------------------------------------------------------------------
    def checklist(self, station: str) -> dict[str, Any]:
        inventory = self.inventory()
        seasons = []
        for season in self.seasons(station):
            try:
                edition = self.catalog(station, season)
            except SourceError as error:
                if error.status == 404:
                    continue
                raise
            visits = inventory.visits_of(station, season)
            expected = list(inventory.expected.get((station, season), ()))
            recorded = {v.visit for v in visits if v.visit_part}
            rows = []
            for visit_id in expected:
                evidence = next((v for v in visits if v.visit == visit_id), None)
                cancelled = bool(evidence and evidence.cancelled)
                rows.append({
                    "visit": visit_id,
                    "declared": visit_id in {m.visit for m in edition.members},
                    "record": bool(evidence and evidence.visit_received),
                    "presence": bool(evidence and evidence.confirmed),
                    "observation": bool(evidence and evidence.observations),
                    "cancelled": cancelled,
                    "missing": [] if cancelled else (list(evidence.missing) if evidence
                                                     else ["visit", "presence", "observation"]),
                    # 取消到访有站点说明即可结项，不再按未交凭据追收。
                    "to_supply": [] if cancelled else self._visit_gaps(evidence),
                    "statement": list(evidence.notes) if evidence else [],
                })
            seasons.append({
                "season": season,
                "edition": edition.edition,
                "state": edition.state,
                "archive": edition.archive,
                "visit_rounds": rows,
                "unexpected_records": sorted({v.visit for v in visits if v.visit_received} - set(expected)),
                "declared_missing": list(edition.missing),
            })
        returned = []
        for receipt in self.office_receipts:
            for row in receipt.get("units", []):
                if row.get("state") != "returned":
                    continue
                missing = row.get("missing")
                visit = self._visit_of_part(station, missing) if missing else None
                returned.append({"receipt": receipt["id"], "unit": row["unit"],
                                 "missing": missing, "visit": visit,
                                 "chase_part": missing,
                                 "state": self._return_state(missing)})
        return {"station": station, "window": self.window, "seasons": seasons,
                "office_returns": returned}

    def _visit_of_part(self, station: str, part_id: str) -> str | None:
        for season in self.seasons(station):
            member = self.current_member(station, season, part_id)
            if member is not None:
                return member.visit
        return None

    def _return_state(self, part_id: str | None) -> str:
        if not part_id:
            return "unknown"
        for receipt in self.parts.values():
            if receipt.part_id == part_id:
                return "received"
        for attempt in reversed(self.attempts):
            if attempt.part_id == part_id:
                return attempt.status
        return "not_attempted"

    @staticmethod
    def _visit_gaps(evidence) -> list[str]:
        if evidence is None:
            return ["visit", "presence", "observation"]
        if evidence.cancelled:
            return []
        gaps = []
        if not evidence.visit_received:
            gaps.append("visit")
        if not evidence.confirmed:
            gaps.append("presence")
        if not evidence.observations:
            gaps.append("observation")
        return gaps

    # ------------------------------------------------------------------
    # 持久化：重启后保留已收部件与办公室原回条依据
    # ------------------------------------------------------------------
    def save(self, path: str | Path) -> Path:
        payload = {
            "version": 1,
            "window": self.window,
            "parts": [receipt.to_dict(include_data=True) for receipt in self.parts.values()],
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "archives": [snapshot.to_dict() for snapshot in self.archives.values()],
            "submissions": {key: record.to_dict() for key, record in self.submissions.items()},
        }
        target = Path(path)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return target

    def load(self, path: str | Path) -> int:
        """载入检查点；返回检查点记录的 window，调用方决定是否推进数据源。"""
        import base64

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        saved_window = int(payload.get("window", self.window))
        self.window = saved_window
        for row in payload.get("parts", []):
            data = base64.b64decode(row.pop("data"))
            receipt = PartReceipt(
                station=row["station"], season=str(row["season"]), part_id=row["part_id"],
                visit=row["visit"], kind=row["kind"], sha256=row["sha256"], data=data,
                source=row["source"], window=row["window"], edition=row["edition"],
                verified=row["verified"], note=row.get("note", ""),
            )
            self.parts[receipt.key] = receipt
        for row in payload.get("attempts", []):
            self.attempts.append(Attempt(**row))
        for row in payload.get("archives", []):
            row = dict(row)
            row["missing"] = tuple(row.get("missing", ()))
            row["extra"] = tuple(row.get("extra", ()))
            snapshot = ArchiveSnapshot(**row)
            self.archives[snapshot.archive_id] = snapshot
        for key, row in payload.get("submissions", {}).items():
            self.submissions[key] = SubmissionRecord(
                request_id=row["request_id"], window=row["window"],
                request=row["request"], response=row["response"], snapshot=row["snapshot"],
            )
        return saved_window

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------
    def digest(self) -> dict[str, Any]:
        rows = []
        for station in self.stations():
            seasons = []
            for season in self.seasons(station):
                try:
                    edition = self.catalog(station, season)
                except SourceError as error:
                    if error.status != 404:
                        raise
                    continue
                seasons.append({
                    "season": season,
                    "edition": edition.edition,
                    "parts_received": sum(1 for key in self.parts if key[:2] == (station, season)),
                    "parts_declared": len(edition.members),
                })
            rows.append({"station": station, "seasons": seasons})
        return {
            "window": self.window,
            "archives_left": self.last_refresh.get("archives_left"),
            "stations": rows,
            "units": [
                {"unit": row.unit, "state": row.state, "category": row.category,
                 "card": row.card.id, "reason": row.reason,
                 "office": row.office, "impact": bool(row.impact)}
                for row in self.assessments()
            ],
        }
