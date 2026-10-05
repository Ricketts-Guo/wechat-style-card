"""Evaluate synthetic references through a running, user-configured local app.

No credentials are read by this script. Configure the key in the local UI first.
Reference agreement measures this small fixture, not real-world accuracy.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("评估接口返回重定向，已停止。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/jev_eval.json")
    parser.add_argument("--limit", type=int, default=36)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.url)
    if address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost") or address.username or address.password or address.query or address.fragment or address.path not in ("", "/"):
        parser.error("评估仅连接本机 Chatprint 服务。")
    if not 1 <= args.limit <= 200:
        parser.error("limit 必须在 1–200 之间。")
    base = args.url.rstrip("/")
    opener = urllib.request.build_opener(NoRedirect())

    def request(path, body=None):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=payload, headers={"Content-Type": "application/json"} if payload else {})
        try:
            with opener.open(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "请求失败")
            except (ValueError, AttributeError):
                detail = "请求失败"
            raise RuntimeError(detail) from None

    if not request("/api/settings")["configured"]:
        print("尚未配置 Jev Key。请在本机设置页填写；此脚本不会读取或保存密钥。")
        return 2
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    cases = fixture["cases"][:args.limit]
    messages, target_ids = [], []
    origin = datetime(2026, 10, 1, 9, tzinfo=timezone(timedelta(hours=8)))
    for case_number, case in enumerate(cases):
        chat = "synthetic-eval:" + case["id"]
        context = case.get("preceding_context", [])
        for index, row in enumerate(context):
            messages.append({"id": f"{case['id']}:context:{index}", "chat_id": chat,
                "sender_id": f"{case['id']}:{row.get('sender', 'context')}",
                "timestamp": (origin + timedelta(minutes=case_number, seconds=index)).isoformat(),
                "type": "text", "text": row["text"]})
        messages.append({"id": case["id"], "chat_id": chat, "sender_id": "synthetic-speaker:" + case["id"],
            "timestamp": (origin + timedelta(minutes=case_number, seconds=len(context))).isoformat(),
            "type": "text", "text": case["text"]})
        target_ids.append(case["id"])
    excluded = [row["id"] for row in messages if row["id"] not in target_ids]
    preview = request("/api/preview", {"messages": messages, "limit": len(cases), "exclude_ids": excluded})
    if set(row["id"] for row in preview["messages"]) != set(target_ids):
        raise RuntimeError("预览目标与评估集不一致，已停止，未调用 Jev。")
    task = request("/api/analyze", {"preview_id": preview["preview_id"], "selected_ids": target_ids})
    print(f"评估 {len(cases)} 条虚构文字，使用本机已配置的账户。", flush=True)
    deadline, completed = time.monotonic() + 1200, -1
    while True:
        job = request("/api/jobs/" + task["job_id"])
        if job["completed"] != completed:
            completed = job["completed"]
            print(f"完成 {completed}/{len(cases)}", flush=True)
        if job["status"] != "running":
            break
        if time.monotonic() > deadline:
            raise RuntimeError("等待超过 20 分钟，任务可能仍在运行；请在页面查看，勿重复提交。")
        time.sleep(.5)
    index = {row["message_id"]: row for row in job["analyses"]}
    comparisons, metrics = [], {}
    for case in cases:
        actual = index.get(case["id"])
        comparisons.append({"id": case["id"], "reference": {key: case["expected_" + key] for key in ("emotion", "intent", "style")},
            "actual": actual, "agreement": {key: bool(actual and actual.get(key) in case["expected_" + key]) for key in ("emotion", "intent", "style")}})
    for dimension in ("emotion", "intent", "style"):
        agreed = sum(row["agreement"][dimension] for row in comparisons)
        metrics[dimension] = {"agreed": agreed, "submitted": len(cases), "valid_results": len(index),
            "agreement_percent": round(agreed * 100 / len(cases), 1) if cases else 0}
    report = {"evaluation": "synthetic-reference-agreement", "date_utc": datetime.now(timezone.utc).isoformat(),
        "fixture": args.fixture.name, "fixture_metadata": fixture.get("metadata", {}),
        "models": job["models"], "metrics": metrics, "usage": job["usage"],
        "requests": job.get("requests", 0), "cached_requests": job.get("cached_requests", 0),
        "errors": job["errors"], "elapsed_seconds": job.get("elapsed_seconds"), "comparisons": comparisons,
        "limitation": "Small synthetic references with explicitly allowed ambiguous labels; not a production accuracy estimate."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"models": report["models"], "metrics": metrics, "usage": report["usage"], "errors": len(report["errors"])}, ensure_ascii=False), flush=True)
    return 0 if len(index) == len(cases) and not job["errors"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"评估未完成：{exc}")
        raise SystemExit(1)
