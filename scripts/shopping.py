#!/usr/bin/env python3
"""淘宝天猫算账助手：离线方案比较与可恢复任务。Python 3.10+，无需第三方依赖。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from workflow import (VERSION, InputError, SearchLimit, check, new_state, load_json,
                      task_lock, read_state, save_state, update_state, comparison,
                      record_purchase, view_state, render_state, render_comparison,
                      fingerprint, now)
from taobao import MARKET, PROFILE_VERSION
from browser_import import import_into_state, load_snapshot, render_import


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--version", action="version", version=VERSION)
    sub = root.add_subparsers(dest="command", required=True)
    for name, help_text in (("new", "创建任务，默认空清单"), ("show", "读取清单、待补问和有效结果"),
                            ("update", "增量修改资料并废止旧结果"), ("compare", "重算原清单、少花钱、少操作"),
                            ("purchase", "记录已完成购买、扣券并更新剩余清单"),
                            ("import-cart", "把浏览器购物车快照导入任务")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("task", type=Path, help="任务目录，包含 session.json")
        command.add_argument("--format", choices=("json", "text"), default="json")
        if name == "new":
            command.add_argument("--input", type=Path, help="可选的初始购物资料JSON")
            command.add_argument("--title")
        elif name == "show":
            command.add_argument("--history", action="store_true")
        elif name == "update":
            command.add_argument("--patch", type=Path, required=True)
        elif name == "purchase":
            command.add_argument("--plan", choices=("original", "cheapest", "fewest_checkouts"), required=True)
            command.add_argument("--order", type=int, required=True)
            command.add_argument("--receipt", type=Path, required=True)
        elif name == "compare":
            command.add_argument("--time-limit", type=float, default=15)
        elif name == "import-cart":
            command.add_argument("--snapshot", type=Path, required=True,
                                 help="浏览器采集的 cart-snapshot JSON")
    solve = sub.add_parser("solve", help="无任务文件的单次方案比较")
    solve.add_argument("input", type=Path)
    solve.add_argument("--approve", action="append", default=[], metavar="ITEM_ID", help="用户同意的具体加购品，可重复")
    solve.add_argument("--prefer", choices=("cheapest", "fewest_checkouts"), default="cheapest")
    solve.add_argument("--time-limit", type=float, default=15)
    solve.add_argument("--format", choices=("json", "text"), default="json")
    demo = sub.add_parser("demo", help="运行390/345/375元假设案例，不写入任务")
    demo.add_argument("--format", choices=("json", "text"), default="text")
    demo.add_argument("--discounts", action="store_true", help="运行比例折扣、红包与积分叠加的假设案例")
    return root


def exit_status(result):
    return {"needs_information": 4, "search_limit": 3, "invalid_input": 2}.get(result.get("status"), 0)


def safe_compare(state, time_limit):
    try:
        return comparison(state, time_limit=time_limit)
    except (InputError, SearchLimit) as exc:
        return {"status": "search_limit" if isinstance(exc, SearchLimit) else "invalid_input",
                "market": MARKET, "rule_profile": PROFILE_VERSION,
                "error": str(exc), "revision": state["revision"], "input_fingerprint": fingerprint(state),
                "generated_at": now(), "data_as_of": state["data_as_of"],
                "plans": {}, "choices": [], "questions": []}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in ("solve", "demo"):
            path = (Path(__file__).resolve().parents[1] / ("fixtures/discounts.json" if args.discounts else "fixtures/demo-cart.json")
                    if args.command == "demo" else args.input)
            state = new_state(load_json(path), title="假设案例" if args.command == "demo" else None)
            approved = ([] if args.discounts else ["cloth"]) if args.command == "demo" else args.approve
            state["preferences"].update(allow_addons=bool(approved), approved_optional_ids=approved,
                                         preferred=getattr(args, "prefer", "cheapest"))
            output = safe_compare(state, getattr(args, "time_limit", 15))
            text = render_comparison(output)
            code = exit_status(output)
        else:
            if args.command not in ("new", "import-cart"):
                check((args.task / "session.json").is_file(), "任务不存在，请先new；原目录未修改")
            with task_lock(args.task) as directory:
                if args.command == "new":
                    check(not (directory / "session.json").exists(), "任务已存在，请show或选择新目录")
                    state = new_state(load_json(args.input) if args.input else None, args.title)
                    save_state(directory, state)
                elif args.command == "import-cart":
                    snapshot = load_snapshot(args.snapshot)
                    existing = (read_state(directory)
                                if (directory / "session.json").exists() else None)
                    state, report = import_into_state(existing, snapshot)
                    save_state(directory, state)
                else:
                    state = read_state(directory)
                    if args.command == "update":
                        state = update_state(state, load_json(args.patch))
                        save_state(directory, state)
                    elif args.command == "compare":
                        state["program_version"] = VERSION
                        state["latest_result"] = safe_compare(state, args.time_limit)
                        save_state(directory, state)
                    elif args.command == "purchase":
                        state = record_purchase(state, args.plan, args.order, load_json(args.receipt))
                        save_state(directory, state)
                if args.command == "compare":
                    output = state["latest_result"]
                    text = render_comparison(output)
                    code = exit_status(output)
                elif args.command == "import-cart":
                    output = dict(report, task=str(directory), revision=state["revision"])
                    text = render_import(report, directory)
                    code = 0
                else:
                    output = view_state(state, history=getattr(args, "history", False))
                    text = render_state(state)
                    code = 0
        sys.stdout.write(text if args.format == "text" else json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return code
    except (InputError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"status": "invalid_input", "error": str(exc)}, ensure_ascii=False) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
