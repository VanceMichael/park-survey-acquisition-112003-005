"""命令行入口：把工作台视图输出为 JSON 或可读文本。"""
from __future__ import annotations

import json
from argparse import ArgumentParser

from .errors import WorkbenchError
from .workbench import JointWorkbench

VIEWS = ("digest", "committee", "field", "catalog", "checklist", "archivist", "evaluator",
         "unit", "archive-gap", "reconcile", "ready", "receipts")


def _print(value, pretty: bool) -> None:
    if pretty:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False))


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="联合评估收件工作台")
    parser.add_argument("--source", default="http://127.0.0.1:8040", help="站点/办公室数据源地址")
    parser.add_argument("--window", type=int, help="先把轮次推进到该 window 再读取")
    parser.add_argument("--view", choices=VIEWS, default="digest", help="要输出的视图")
    parser.add_argument("--station", help="站点（catalog/checklist/archivist 视图）")
    parser.add_argument("--season", help="工作季（catalog 视图）")
    parser.add_argument("--unit", help="比较单元（evaluator 视图）")
    parser.add_argument("--batch", help="评估批次（evaluator 视图）")
    parser.add_argument("--plot", help="样点（evaluator 视图）")
    parser.add_argument("--archive", help="整包编号（archive-gap/reconcile 视图）")
    parser.add_argument("--compact", action="store_true", help="单行 JSON")
    parser.add_argument("--state", help="工作台检查点文件（JSON），重启后沿用已收部件")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except WorkbenchError as error:
        print(json.dumps({"state": "error", "detail": str(error)}, ensure_ascii=False))
        return 2


def _run(args) -> int:
    workbench = JointWorkbench(args.source)
    saved_window = 0
    if args.state:
        from pathlib import Path

        if Path(args.state).exists():
            saved_window = workbench.load(args.state)
    target = args.window if args.window is not None else saved_window
    if target:
        workbench.advance(target)
    else:
        workbench.refresh()
    if args.view == "digest":
        value = workbench.digest()
    elif args.view == "committee":
        value = workbench.committee_view()
    elif args.view == "field":
        if not args.station:
            raise SystemExit("field 视图需要 --station")
        value = workbench.field_reconciliation(args.station)
    elif args.view == "catalog":
        if not args.station:
            raise SystemExit("catalog 视图需要 --station")
        seasons = [args.season] if args.season else workbench.seasons(args.station)
        value = {"window": workbench.window,
                 "editions": [workbench.catalog(args.station, season).to_dict() for season in seasons]}
    elif args.view == "checklist":
        if not args.station:
            raise SystemExit("checklist 视图需要 --station")
        value = workbench.checklist(args.station)
    elif args.view == "archivist":
        value = workbench.archivist_view(args.station)
    elif args.view == "evaluator":
        value = workbench.evaluator_view(args.unit, args.batch, args.plot)
    elif args.view == "unit":
        if not args.unit:
            raise SystemExit("unit 视图需要 --unit")
        value = workbench.evidence_summary(args.unit)
    elif args.view == "archive-gap":
        if not args.archive:
            raise SystemExit("archive-gap 视图需要 --archive")
        opened = workbench.fetch_archive(args.archive)
        value = {"opened": opened, "gap": workbench.archive_gap(args.archive)}
    elif args.view == "reconcile":
        if not args.archive:
            raise SystemExit("reconcile 视图需要 --archive")
        value = workbench.reconcile_archive(args.archive)
    elif args.view == "ready":
        value = {"window": workbench.window, "units": workbench.ready_units()}
    elif args.view == "receipts":
        value = {"receipts": workbench.receipt_history(),
                 "accepted": workbench.accepted_units()}
    _print(value, not args.compact)
    if args.state:
        workbench.save(args.state)
    return 0
