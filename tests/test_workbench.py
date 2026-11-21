"""联合评估收件工作台的行为测试（合成数据源）。"""
from __future__ import annotations

import json
import threading
import unittest
from zipfile import ZipFile

from source_service import create_source
from joint_workbench import JointWorkbench, SubmissionBlocked, WorkbenchError


class WorkbenchBase(unittest.TestCase):
    """共享夹具；不含 test_ 方法，不会被单独收集。"""

    def setUp(self):
        self.server = create_source()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.workbench = JointWorkbench(f"http://127.0.0.1:{self.server.server_port}")
        self.workbench.refresh()

    # -- 工具 ---------------------------------------------------------
    def advance(self, window):
        return self.workbench.advance(window)

    def grab(self, station, season, parts):
        if (station, str(season)) not in self.workbench.editions:
            try:
                self.workbench.refresh(station=station, season=season)
            except Exception:
                pass
        return {part: self.workbench.fetch_object(station, season, part)["state"] for part in parts}

    def collect_north_2026(self):
        self.advance(1)
        self.grab("north", "2026", ["V-N26-1", "O26-1", "V-N26-2"])
        self.advance(2)
        self.grab("north", "2026", ["O26-2", "P26-2", "V-N26-3", "V-N26-4", "O26-4"])
        self.advance(3)
        self.grab("north", "2026", ["P26-1", "P26-4"])

    def collect_south(self):
        self.advance(1)
        self.grab("south", "2026", ["V-S26-1", "SO26", "SP26"])
        self.advance(2)
        self.grab("south", "2026", ["SO26", "SP26"])

    def collect_all(self):
        self.workbench.fetch_archive("NF19")
        self.advance(1)
        self.workbench.fetch_archive("SF19")
        self.grab("north", "2026", ["V-N26-1", "O26-1", "V-N26-2"])
        self.grab("south", "2026", ["V-S26-1", "SO26", "SP26"])
        self.advance(2)
        self.grab("north", "2026", ["O26-2", "P26-2", "V-N26-3", "V-N26-4", "O26-4"])
        self.grab("south", "2026", ["SO26", "SP26"])
        self.advance(3)
        self.grab("north", "2026", ["P26-1", "P26-4"])
        self.advance(4)
        self.workbench.fetch_archive("NF26")
        self.advance(5)
        self.workbench.fetch_archive("NF26R")
        self.advance(6)

    # -- 目录和部件 ---------------------------------------------------
class WorkbenchTest(WorkbenchBase):
    def test_catalog_pages_follow_edition_and_reject_stale_page(self):
        self.advance(1)
        edition = self.workbench.catalog("north", "2026")
        self.assertEqual(edition.edition, "NU26-a")
        self.assertEqual([m.id for m in edition.members],
                         ["V-N26-1", "O26-1", "P26-1", "V-N26-2", "O26-2", "P26-2"])
        self.assertEqual(edition.missing, ("N26-3", "N26-4"))
        self.assertEqual(edition.pages, 6)

    def test_open_catalog_advances_to_next_edition_without_mixing_pages(self):
        self.advance(2)
        edition = self.workbench.catalog("north", "2026")
        self.assertEqual(edition.edition, "NU26-b")
        self.assertEqual(edition.missing, ())
        self.assertEqual(len(edition.members), 10)

    def test_short_term_window_states_and_duplicate_merge(self):
        self.advance(1)
        self.assertEqual(self.grab("north", "2026", ["P26-1"])["P26-1"], "pending")
        self.advance(1)
        self.assertEqual(self.grab("north", "2026", ["V-N26-1", "O26-1"])["O26-1"], "received")
        self.assertEqual(self.grab("north", "2026", ["O26-1"])["O26-1"], "duplicate")
        self.advance(3)
        self.assertEqual(self.grab("north", "2026", ["O26-1"])["O26-1"], "expired")
        self.assertIn("O26-1", [r.part_id for r in self.workbench.parts.values()])

    def test_withdrawn_member_is_not_evidence(self):
        self.collect_north_2026()
        self.advance(4)
        self.assertEqual(self.workbench.catalog("north", "2026").edition, "NF26")
        self.assertTrue(self.workbench.current_member("north", "2026", "O26-1").withdrawn)
        report = self.workbench.evaluator_view("U-East")["units"][0]["target"]
        parts = {o["part"] for v in report["visits"] for o in v["observations"]}
        self.assertNotIn("O26-1", parts)

    def test_archive_lane_is_per_window_and_does_not_reopen_same_edition(self):
        self.assertEqual(self.workbench.fetch_archive("NF19")["state"], "sealed")
        self.assertEqual(self.workbench.fetch_archive("SF19")["state"], "lane_busy")
        self.assertEqual(self.workbench.fetch_archive("NF19")["state"], "cached")
        self.advance(1)
        self.assertEqual(self.workbench.fetch_archive("SF19")["state"], "sealed")

    def test_archive_reconciles_against_members_and_classifies_gaps(self):
        self.workbench.fetch_archive("NF19")
        gap = self.workbench.archive_gap("NF19")
        self.assertIn("补齐缺件", gap["verdict"])
        self.advance(1)
        for part in ["V-N26-1", "O26-1", "V-N26-2"]:
            self.workbench.fetch_object("north", "2026", part)
        self.advance(2)
        for part in ["O26-2", "P26-2", "V-N26-3", "V-N26-4", "O26-4"]:
            self.workbench.fetch_object("north", "2026", part)
        self.advance(3)
        for part in ["P26-1", "P26-4"]:
            self.workbench.fetch_object("north", "2026", part)
        self.advance(4)
        self.workbench.fetch_archive("NF26")
        gap = self.workbench.archive_gap("NF26")
        self.assertEqual(gap["withdrawn"], ["O26-1"])
        self.assertEqual(gap["short_term_loss"], [])
        self.assertIn("撤回原件", gap["verdict"])
        self.advance(5)
        self.workbench.fetch_archive("NF26R")
        gap = self.workbench.archive_gap("NF26R")
        self.assertEqual(gap["filled"], ["O26-1R"])
        self.assertIn("补齐缺件", gap["verdict"])

    def test_archive_member_names_not_in_catalog_are_reported_extra(self):
        snapshot = self.workbench.fetch_archive("NF19")
        self.assertIsInstance(snapshot, dict)

    # -- 评估 ---------------------------------------------------------
    def test_missing_presence_is_missing_evidence_not_zero(self):
        self.advance(1)
        self.grab("north", "2026", ["V-N26-1", "O26-1", "V-N26-2", "P26-2", "O26-2"])
        self.workbench.fetch_archive("NF19")
        self.advance(6)
        east = self.workbench.assessment("U-East")
        self.assertEqual(east.state, "missing_evidence")
        self.assertIn("O26-1R", east.reason)

    def test_comparable_unit_requires_replacement_part_identity(self):
        self.collect_all()
        east = self.workbench.assessment("U-East")
        self.assertEqual(east.state, "comparable")
        self.assertEqual(east.card.id, "R2b")
        self.assertEqual({p["target"] for p in east.__dict__.get("pairs", [])}, set())
        report = self.workbench.evaluator_view("U-East")["units"][0]
        self.assertEqual(len(report["pairs"]), 2)
        self.assertEqual({p["target"] for p in report["pairs"]}, {"N26-1", "N26-2"})

    def test_replaced_observation_is_historical_only(self):
        self.collect_all()
        report = self.workbench.evaluator_view("U-East")["units"][0]
        parts = {o["part"] for v in report["target"]["visits"] for o in v["observations"]}
        self.assertNotIn("O26-1", parts)
        self.assertIn("O26-1R", parts)

    def test_cancelled_visit_is_not_visited_and_keeps_statement(self):
        self.collect_all()
        other = self.workbench.assessment("U-Other")
        self.assertEqual(other.state, "not_visited")
        self.assertIn("取消", other.reason)
        self.assertIn("N26-3", other.target["cancelled"])

    def test_true_unobserved_needs_confirmed_visit_and_coverage(self):
        self.collect_all()
        south = self.workbench.assessment("U-South")
        self.assertEqual(south.state, "true_unobserved")
        self.assertEqual(south.category, "true_unobserved")

    def test_method_mismatch_is_incomparable_and_context_card_too(self):
        self.collect_all()
        west = self.workbench.assessment("U-West")
        self.assertEqual(west.state, "incomparable")
        self.assertIn("trail", " ".join(west.target["reasons"]))
        self.assertIn("N26-4", west.target["method_mismatch"])

    def test_review_card_is_not_comparable_until_revised(self):
        self.advance(3)
        east = self.workbench.assessment("U-East")
        self.assertEqual(east.card.id, "R2a")
        self.assertEqual(east.state, "review")
        self.assertEqual(east.category, "incomparable")

    def test_zero_without_presence_is_missing_evidence(self):
        self.workbench.fetch_archive("SF19")
        self.advance(1)
        self.grab("south", "2026", ["V-S26-1", "SO26"])
        self.advance(6)
        south = self.workbench.assessment("U-South")
        self.assertEqual(south.state, "missing_evidence")
        self.assertIn("凭据", south.reason)

    def test_missing_observation_is_missing_evidence(self):
        self.workbench.fetch_archive("SF19")
        self.advance(1)
        self.grab("south", "2026", ["V-S26-1", "SP26"])
        self.advance(6)
        south = self.workbench.assessment("U-South")
        self.assertEqual(south.state, "missing_evidence")
        self.assertIn("观测", south.reason)

    # -- 档案员清点 ---------------------------------------------------
    def test_checklist_uses_expected_visit_rounds_not_counts(self):
        self.advance(1)
        self.grab("north", "2026", ["V-N26-1", "O26-1", "V-N26-2"])
        checklist = self.workbench.checklist("north")
        season = next(row for row in checklist["seasons"] if row["season"] == "2026")
        rounds = {row["visit"]: row for row in season["visit_rounds"]}
        self.assertEqual(list(rounds), ["N26-1", "N26-2", "N26-3", "N26-4"])
        self.assertFalse(rounds["N26-1"]["presence"])
        self.assertIn("presence", rounds["N26-1"]["to_supply"])
        self.assertIn("visit", rounds["N26-3"]["to_supply"])

    def test_archivist_view_points_at_archive_and_parts(self):
        self.advance(1)
        self.grab("north", "2026", ["V-N26-1"])
        view = self.workbench.archivist_view("north")
        season = next(row for row in view["stations"][0]["seasons"] if row["season"] == "2026")
        chase = {row["part"]: row for row in season["chase"]}
        self.assertIn("O26-1", chase)
        self.assertNotIn("V-N26-1", chase)
        self.assertEqual(chase["O26-1"]["edition"], "NU26-a")

    # -- 办公室受理范围 -----------------------------------------------
    def test_submission_respects_office_accepted_range(self):
        self.collect_all()
        with self.assertRaises(SubmissionBlocked):
            self.workbench.submit("ERX", ["U-East"])
        self.assertIn("ER1", str(self.workbench.prepare_submission("ERX", ["U-East"])))

    def test_changed_unit_shows_original_basis_and_withdrawal(self):
        self.collect_all()
        east = self.workbench.assessment("U-East")
        self.assertIsNotNone(east.impact)
        self.assertIn("R2", east.impact["original_basis"]["card"])
        self.assertIn("R2b", " ".join(east.impact["changes"]))
        self.assertTrue(east.impact["withdraw_conclusions"])

    def test_unaffected_units_keep_original_receipt(self):
        self.collect_all()
        self.assertIsNone(self.workbench.assessment("U-South").impact)

    def test_withdraw_only_affected_unit_then_resubmit(self):
        self.collect_all()
        withdrawn = self.workbench.withdraw_changed("ERD", "U-East", "ER1")
        self.assertEqual(withdrawn["submission"]["units"][0]["state"], "withdrawn")
        self.assertEqual(withdrawn["submission"]["units"][0]["previous"], "ER1")
        self.assertIn("O19-1", withdrawn["submission"]["source"]["units"][0]["materials"])
        self.assertNotIn("O26-1R", withdrawn["submission"]["source"]["units"][0]["materials"])
        resubmitted = self.workbench.submit("ERD2", ["U-East"])
        self.assertEqual(resubmitted["submission"]["units"][0]["state"], "accepted")
        self.assertEqual(resubmitted["submission"]["units"][0]["card"], "R2b")

    def test_submission_request_is_idempotent(self):
        self.collect_all()
        first = self.workbench.submit("ERS", ["U-South"])
        again = self.workbench.submit("ERS", ["U-South"])
        self.assertTrue(again.get("replayed"))
        self.assertEqual(first["submission"], again["submission"])
        with self.assertRaises(SubmissionBlocked):
            self.workbench.submit("ERS", ["U-East"])

    def test_ready_units_skip_accepted_and_lock_affected(self):
        self.collect_all()
        ready = {row["unit"] for row in self.workbench.ready_units()}
        self.assertEqual(ready, {"U-South"})

    def test_supply_missing_follows_checklist(self):
        self.advance(1)
        report = self.workbench.supply_missing("north", "2026")
        states = {row["part"]: row["state"] for row in report}
        self.assertEqual(states["P26-1"], "pending")
        self.assertEqual(states["V-N26-1"], "received")

    def test_checklist_does_not_chase_cancelled_visit(self):
        self.collect_all()
        checklist = self.workbench.checklist("north")
        season = next(row for row in checklist["seasons"] if row["season"] == "2026")
        visit = next(row for row in season["visit_rounds"] if row["visit"] == "N26-3")
        self.assertTrue(visit["cancelled"])
        self.assertEqual(visit["to_supply"], [])

    def test_archive_reconcile_without_reopening(self):
        self.collect_all()
        report = self.workbench.reconcile_archive("NF26R")
        self.assertEqual(report["summary"]["still_missing"], 0)
        # 11 份目录成员中 O26-1 已撤回，只对 10 份今次有效部件求和。
        self.assertEqual(report["summary"]["same"], 10)
        self.assertEqual(self.workbench.fetch_archive("NF26R")["state"], "cached")

    def test_next_submission_can_proceed_for_other_unit(self):
        self.collect_all()
        submitted = self.workbench.submit("ER-A", ["U-South"])
        self.assertEqual(submitted["submission"]["units"][0]["state"], "accepted")
        # U-East is affected by the revised card and still needs its own withdrawal cycle.
        with self.assertRaises(SubmissionBlocked):
            self.workbench.submit("ER-B", ["U-East"])


if __name__ == "__main__":
    unittest.main()


class WorkbenchEdgeCaseTest(unittest.TestCase):
    """不依赖完整季包，直接对内存证据做规则测试。"""

    def make(self, members, parts, cards):
        from joint_workbench.evidence import Inventory
        from joint_workbench.models import Card, Edition, Member, PartReceipt
        from hashlib import sha256

        edition_members = []
        receipts = {}
        for raw in members:
            member = Member(
                station=raw["station"], season=str(raw["season"]), edition="E",
                id=raw["id"], visit=raw["visit"], kind=raw["kind"],
                member=raw.get("member", raw["id"] + ".json"),
                sha256=raw.get("sha256", "x"),
                introduced=raw.get("introduced", 0), state=raw.get("state", "declared"),
                replaces=raw.get("replaces"),
            )
            edition_members.append(member)
            if raw.get("body") is not None:
                blob = json.dumps(raw["body"]).encode()
                digest = sha256(blob).hexdigest()
                member = Member(**{**member.__dict__, "sha256": digest})
                edition_members[-1] = member
                receipts[member.key] = PartReceipt(
                    station=member.station, season=member.season, part_id=member.id,
                    visit=member.visit, kind=member.kind, sha256=digest, data=blob,
                    source="object", window=0, edition="E",
                )
        editions = {}
        for member in edition_members:
            editions.setdefault((member.station, member.season), []).append(member)
        edition_objs = [Edition(station=k[0], season=k[1], edition="E", known=0, state="open",
                                missing=(), archive=None, members=tuple(v))
                        for k, v in editions.items()]
        return Inventory([], edition_objs, receipts, [Card.from_raw(c) for c in cards])

    def test_overlapping_sections_between_two_targets_trigger_review(self):
        from joint_workbench.evidence import assess_units

        members = [
            {"station": "n", "season": 2019, "id": "V1", "visit": "A", "kind": "visit",
             "body": {"visit": "A", "plot": "OLD", "team": "t", "method": "line",
                      "outcome": "visited", "sections": ["NE"]}},
            {"station": "n", "season": 2019, "id": "P1", "visit": "A", "kind": "presence",
             "body": {"visit": "A", "confirmed": True}},
            {"station": "n", "season": 2019, "id": "O1", "visit": "A", "kind": "observation",
             "body": {"visit": "A", "sections": {"NE": 1}}},
            {"station": "n", "season": 2026, "id": "V2", "visit": "B", "kind": "visit",
             "body": {"visit": "B", "plot": "NEW1", "team": "t1", "method": "line",
                      "outcome": "visited", "sections": ["NE"]}},
            {"station": "n", "season": 2026, "id": "P2", "visit": "B", "kind": "presence",
             "body": {"visit": "B", "confirmed": True}},
            {"station": "n", "season": 2026, "id": "O2", "visit": "B", "kind": "observation",
             "body": {"visit": "B", "sections": {"NE": 1}}},
            {"station": "n", "season": 2026, "id": "V3", "visit": "C", "kind": "visit",
             "body": {"visit": "C", "plot": "NEW2", "team": "t2", "method": "line",
                      "outcome": "visited", "sections": ["NE"]}},
            {"station": "n", "season": 2026, "id": "P3", "visit": "C", "kind": "presence",
             "body": {"visit": "C", "confirmed": True}},
            {"station": "n", "season": 2026, "id": "O3", "visit": "C", "kind": "observation",
             "body": {"visit": "C", "sections": {"NE": 1}}},
        ]
        cards = [
            {"id": "C1", "unit": "U1", "previous": "OLD", "target": "NEW1",
             "sections": ["NE"], "method": "line", "state": "comparable"},
            {"id": "C2", "unit": "U2", "previous": "OLD", "target": "NEW2",
             "sections": ["NE"], "method": "line", "state": "comparable"},
        ]
        rows = {row.unit: row for row in assess_units(self.make(members, {}, cards))}
        self.assertEqual(rows["U1"].state, "review")
        self.assertEqual(rows["U2"].state, "review")

    def test_disjoint_sections_keep_both_units(self):
        from joint_workbench.evidence import assess_units

        members = [
            {"station": "n", "season": 2019, "id": "V1", "visit": "A", "kind": "visit",
             "body": {"visit": "A", "plot": "OLD", "team": "t", "method": "line",
                      "outcome": "visited", "sections": ["NE", "NW"]}},
            {"station": "n", "season": 2019, "id": "P1", "visit": "A", "kind": "presence",
             "body": {"visit": "A", "confirmed": True}},
            {"station": "n", "season": 2019, "id": "O1", "visit": "A", "kind": "observation",
             "body": {"visit": "A", "sections": {"NE": 1, "NW": 2}}},
        ]
        for season, visit, plot, section in ((2026, "B", "NEW1", "NE"), (2026, "C", "NEW2", "NW")):
            members += [
                {"station": "n", "season": season, "id": f"V{visit}", "visit": visit, "kind": "visit",
                 "body": {"visit": visit, "plot": plot, "team": "t", "method": "line",
                          "outcome": "visited", "sections": [section]}},
                {"station": "n", "season": season, "id": f"P{visit}", "visit": visit, "kind": "presence",
                 "body": {"visit": visit, "confirmed": True}},
                {"station": "n", "season": season, "id": f"O{visit}", "visit": visit, "kind": "observation",
                 "body": {"visit": visit, "sections": {section: 1}}},
            ]
        cards = [
            {"id": "C1", "unit": "U1", "previous": "OLD", "target": "NEW1",
             "sections": ["NE"], "method": "line", "state": "comparable"},
            {"id": "C2", "unit": "U2", "previous": "OLD", "target": "NEW2",
             "sections": ["NW"], "method": "line", "state": "comparable"},
        ]
        rows = {row.unit: row for row in assess_units(self.make(members, {}, cards))}
        self.assertEqual(rows["U1"].state, "comparable")
        self.assertEqual(rows["U2"].state, "comparable")

    def test_requires_replacement_part_cannot_be_covered_by_old_identical_value(self):
        from joint_workbench.evidence import assess_units

        members = [
            {"station": "n", "season": 2019, "id": "V1", "visit": "A", "kind": "visit",
             "body": {"visit": "A", "plot": "OLD", "team": "t", "method": "line",
                      "outcome": "visited", "sections": ["NE"]}},
            {"station": "n", "season": 2019, "id": "P1", "visit": "A", "kind": "presence",
             "body": {"visit": "A", "confirmed": True}},
            {"station": "n", "season": 2019, "id": "O1", "visit": "A", "kind": "observation",
             "body": {"visit": "A", "sections": {"NE": 1}}},
            {"station": "n", "season": 2026, "id": "V2", "visit": "B", "kind": "visit",
             "body": {"visit": "B", "plot": "NEW", "team": "t", "method": "line",
                      "outcome": "visited", "sections": ["NE"]}},
            {"station": "n", "season": 2026, "id": "P2", "visit": "B", "kind": "presence",
             "body": {"visit": "B", "confirmed": True}},
            {"station": "n", "season": 2026, "id": "O2", "visit": "B", "kind": "observation",
             "body": {"visit": "B", "sections": {"NE": 1}}},
            {"station": "n", "season": 2026, "id": "O3", "visit": "B", "kind": "observation",
             "state": "withdrawn", "body": {"visit": "B", "sections": {"NE": 1}}},
        ]
        cards = [{"id": "C1", "unit": "U1", "previous": "OLD", "target": "NEW",
                  "sections": ["NE"], "method": "line", "state": "comparable",
                  "requires": ["O3"]}]
        row = assess_units(self.make(members, {}, cards))[0]
        self.assertEqual(row.state, "missing_evidence")
        self.assertIn("O3", row.reason)

    def test_zero_in_one_required_section_is_not_true_unobserved(self):
        from joint_workbench.evidence import assess_units

        members = [
            {"station": "n", "season": 2019, "id": "V1", "visit": "A", "kind": "visit",
             "body": {"visit": "A", "plot": "OLD", "team": "t", "method": "line",
                      "outcome": "visited", "sections": ["NE", "NW"]}},
            {"station": "n", "season": 2019, "id": "P1", "visit": "A", "kind": "presence",
             "body": {"visit": "A", "confirmed": True}},
            {"station": "n", "season": 2019, "id": "O1", "visit": "A", "kind": "observation",
             "body": {"visit": "A", "sections": {"NE": 1, "NW": 0}}},
            {"station": "n", "season": 2026, "id": "V2", "visit": "B", "kind": "visit",
             "body": {"visit": "B", "plot": "NEW", "team": "t", "method": "line",
                      "outcome": "visited", "sections": ["NE", "NW"]}},
            {"station": "n", "season": 2026, "id": "P2", "visit": "B", "kind": "presence",
             "body": {"visit": "B", "confirmed": True}},
            {"station": "n", "season": 2026, "id": "O2", "visit": "B", "kind": "observation",
             "body": {"visit": "B", "sections": {"NE": 0, "NW": 3}}},
        ]
        cards = [{"id": "C1", "unit": "U1", "previous": "OLD", "target": "NEW",
                  "sections": ["NE", "NW"], "method": "line", "state": "comparable"}]
        row = assess_units(self.make(members, {}, cards))[0]
        self.assertEqual(row.state, "comparable")


class WorkbenchPersistenceTest(unittest.TestCase):
    def test_checkpoint_restores_parts_archives_and_office_basis(self):
        import tempfile
        from pathlib import Path

        server = create_source()
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        workbench = JointWorkbench(base)
        workbench.refresh()
        workbench.fetch_archive("NF19")
        workbench.advance(1)
        for part in ["V-S26-1", "SO26", "SP26"]:
            workbench.fetch_object("south", "2026", part)
        workbench.fetch_archive("SF19")
        workbench.advance(6)
        submitted = workbench.submit("ER-P", ["U-South"])
        self.assertEqual(submitted["submission"]["units"][0]["state"], "accepted")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            workbench.save(path)
            restored = JointWorkbench(base)
            self.assertEqual(restored.load(path), 6)
            restored.refresh()
            self.assertIn(("south", "2026", "SP26"), restored.parts)
            self.assertEqual(restored.fetch_archive("NF19")["state"], "cached")
            self.assertIn("U-South", restored.accepted_units())
            self.assertEqual(restored.submit("ER-P", ["U-South"])["replayed"], True)


class WorkbenchScopeTest(WorkbenchBase):
    """办公室范围与站点隔离的补充断言。"""

    def test_archive_does_not_settle_other_station(self):
        self.advance(1)
        self.workbench.fetch_archive("NF19")
        self.assertFalse(any(key[0] == "south" for key in self.workbench.parts))
        self.assertEqual(self.workbench.assessment("U-South").state, "missing_evidence")

    def test_two_teams_on_same_target_are_both_kept(self):
        self.collect_all()
        report = self.workbench.evaluator_view("U-East")["units"][0]
        teams = {v["team"] for v in report["target"]["visits"]}
        self.assertEqual(teams, {"北甲", "北乙"})
        self.assertEqual(len(report["target"]["valid"]), 2)

    def test_other_unit_can_be_submitted_while_one_is_disputed(self):
        self.collect_all()
        # U-East 已受理但对应卡改判，仍在等待办公室确认撤下。
        self.assertIsNotNone(self.workbench.assessment("U-East").impact)
        submitted = self.workbench.submit("ER-OTHER", ["U-South"])
        self.assertEqual(submitted["submission"]["units"][0]["state"], "accepted")
        # 同一轮内仍可继续处理下一个具备依据的单元，不必等整站争议解完。
        self.assertEqual(self.workbench.accepted_units()["U-South"]["receipt"], "ER-OTHER")

    def test_returned_unit_points_to_visit_and_part_state(self):
        self.advance(2)
        returns = self.workbench.checklist("south")["office_returns"]
        self.assertEqual(returns[0]["visit"], "S26-1")
        self.assertEqual(returns[0]["chase_part"], "SP26")
        self.assertEqual(returns[0]["state"], "not_attempted")
        self.grab("south", "2026", ["SP26"])
        self.assertEqual(self.workbench.checklist("south")["office_returns"][0]["state"], "received")

    def test_digest_lists_units_by_plot_and_station(self):
        self.collect_all()
        by_plot = self.workbench.evaluator_view(plot="N-08-W")["units"]
        self.assertEqual([row["unit"] for row in by_plot], ["U-West"])
        by_station = self.workbench.evaluator_view(batch="south")["units"]
        self.assertEqual([row["unit"] for row in by_station], ["U-South"])


class WorkbenchResubmitTest(WorkbenchBase):
    def test_resubmit_round_trip_only_touches_affected_unit(self):
        self.collect_all()
        self.workbench.submit("ER-OK", ["U-South"])
        result = self.workbench.resubmit("ER-EAST", "U-East", "ER1")
        self.assertEqual(result["withdraw"]["submission"]["units"][0]["state"], "withdrawn")
        self.assertEqual(result["submit"]["submission"]["units"][0]["state"], "accepted")
        self.assertEqual(self.workbench.accepted_units()["U-South"]["receipt"], "ER-OK")
        self.assertEqual(self.workbench.accepted_units()["U-East"]["receipt"], "ER-EAST")

    def test_resubmit_refuses_unaffected_unit(self):
        self.collect_all()
        with self.assertRaises(SubmissionBlocked):
            self.workbench.resubmit("ER-OK", "U-South", "ER1")


class WorkbenchCommitteeTest(WorkbenchBase):
    def test_committee_answers_three_questions(self):
        self.collect_all()
        view = self.workbench.committee_view()
        repeated = {row["unit"] for row in view["repeated"]}
        self.assertIn("U-East", repeated)
        self.assertNotIn("U-West", repeated)
        self.assertEqual([row["unit"] for row in view["not_visited"]], ["U-Other"])
        self.assertEqual([row["unit"] for row in view["true_unobserved"]], ["U-South"])
        self.assertTrue(view["unresolved"])

    def test_field_reconciliation_lists_each_visit_with_gaps(self):
        self.advance(1)
        self.grab("north", "2026", ["V-N26-1"])
        report = self.workbench.field_reconciliation("north")
        by_visit = {row["visit"]: row for row in report["visits"]}
        self.assertTrue(by_visit["N26-1"]["record"])
        self.assertFalse(by_visit["N26-1"]["presence"])
        self.assertIn("presence", by_visit["N26-1"]["gaps"])
        self.assertFalse(by_visit["N26-4"]["record"])
