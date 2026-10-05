"""Run with python -m chatprint or the installed chatprint command."""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from .jev import JevError


def main():
    p = argparse.ArgumentParser(description="Chatprint · 微信聊天风格卡")
    sub = p.add_subparsers(dest="command")
    web = sub.add_parser("serve", help="启动本机网页")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true")
    analyze = sub.add_parser("analyze", help="分析导出文件；密钥通过环境变量或隐藏输入")
    analyze.add_argument("input", type=Path)
    analyze.add_argument("--limit", type=int, default=30)
    analyze.add_argument("--output", type=Path, required=True)
    report = sub.add_parser("report", help="只在本机统计并导出 Markdown")
    report.add_argument("input", type=Path)
    report.add_argument("--analyses", type=Path)
    report.add_argument("--output", type=Path, required=True)
    sub.add_parser("doctor", help="检查本机能力，不读取微信内容")
    args = p.parse_args()
    try:
        if args.command in (None, "serve"):
            from .server import serve
            serve(getattr(args, "port", 8765), not getattr(args, "no_browser", False))
        elif args.command == "doctor":
            from .wechat import cli_status
            print(json.dumps({"python": sys.version.split()[0], "jev_key_configured": bool(os.environ.get("TYPESAFE_API_KEY")), "wechat": cli_status()}, ensure_ascii=False, indent=2))
        else:
            from .importers import normalize_import
            data = normalize_import(args.input.read_text(encoding="utf-8-sig"), args.input.name)
            if args.command == "analyze":
                from .jev import JevClient, analyze_prepared, prepare_messages
                key = os.environ.get("TYPESAFE_API_KEY", "").strip() or getpass.getpass("TypeSafe API Key（隐藏输入）：").strip()
                selected = prepare_messages(data["messages"], args.limit)
                print(f"将向 TypeSafe 发送 {selected['selected']} 条自动脱敏文字及最多两条前文。", file=sys.stderr)
                result = analyze_prepared(JevClient(key), selected["messages"])
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                os.chmod(args.output, 0o600)
                if result["errors"]:
                    print(f"{len(result['errors'])} 条分析失败，详见结果文件。", file=sys.stderr)
                    return 1
            else:
                from .analytics import make_report
                analyses = json.loads(args.analyses.read_text(encoding="utf-8-sig")).get("analyses", []) if args.analyses else []
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(make_report(data["messages"], analyses, anonymize=True), encoding="utf-8")
                os.chmod(args.output, 0o600)
    except (ValueError, OSError, JevError) as exc:
        print(f"无法完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
