"""Loopback-only web app. No database, telemetry, credential file or third-party JS."""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import __version__
from .analytics import make_report, summarize
from .demo import make_demo
from .importers import normalize_import
from .jev import ENDPOINT, MODEL, JevClient, analyze_prepared, prepare_messages
from . import wechat

MAX_BODY = 30 * 1024 * 1024
MAX_MESSAGES = 50000
WEB = Path(__file__).parent / "web"


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.client = JevClient(os.environ["TYPESAFE_API_KEY"]) if os.environ.get("TYPESAFE_API_KEY", "").strip() else None
        self.jobs: dict[str, dict] = {}
        self.previews: dict[str, dict] = {}

    def settings(self) -> dict:
        with self.lock:
            return {"configured": self.client is not None, "model": MODEL, "endpoint": ENDPOINT, "version": __version__}

    def configure(self, key: str) -> dict:
        if not isinstance(key, str) or len(key) > 4096:
            raise ValueError("API Key 格式无效。")
        with self.lock:
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise ValueError("请等当前分析结束，再更改 API Key。")
            self.client = JevClient(key) if key.strip() else None
        return self.settings()

    def start(self, prepared: list[dict]) -> str:
        with self.lock:
            if not self.client:
                raise ValueError("请先在 Jev 设置中填写你自己的 API Key。")
            if not prepared:
                raise ValueError("当前范围没有可分析的文字消息。")
            if any(j["status"] == "running" for j in self.jobs.values()):
                raise ValueError("已有分析正在进行，请等它完成或取消。")
            while len(self.jobs) >= 12:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = secrets.token_urlsafe(18)
            job = {"status": "running", "completed": 0, "total": len(prepared), "analyses": [], "errors": [],
                "usage": {"input_tokens": 0, "output_tokens": 0}, "models": [], "cancel_requested": False, "started_at": time.time()}
            self.jobs[job_id] = job
            client = self.client

        def work():
            def update(result):
                with self.lock:
                    job.update({k: v for k, v in result.items() if k != "cancelled"})
                    job["completed"] = len(job["analyses"]) + len(job["errors"])
            try:
                result = analyze_prepared(client, prepared, progress=update, cancelled=lambda: job["cancel_requested"])
                update(result)
                with self.lock:
                    job["status"] = "cancelled" if result.get("cancelled") else ("failed" if not result["analyses"] and result["errors"] else "completed")
                    job["elapsed_seconds"] = round(time.time() - job["started_at"], 2)
            except Exception:
                with self.lock:
                    job["status"] = "failed"
                    finished = {row.get("message_id") for row in job["analyses"] + job["errors"]}
                    job["errors"].extend({"message_id": m["id"], "error": "分析遇到内部错误，已停止；消息与密钥未写入日志。"}
                                         for m in prepared if m["id"] not in finished)
                    job["completed"] = len(job["analyses"]) + len(job["errors"])
                    job["elapsed_seconds"] = round(time.time() - job["started_at"], 2)
        threading.Thread(target=work, daemon=True).start()
        return job_id

    def preview(self, messages, limit=30, exclude_ids=None):
        if exclude_ids is not None and (not isinstance(exclude_ids, list) or len(exclude_ids) > MAX_MESSAGES or any(not isinstance(i, str) for i in exclude_ids)):
            raise ValueError("跳过目标需要消息 ID 数组。")
        prepared = prepare_messages(messages, limit, anonymize=True, exclude_ids=exclude_ids)
        with self.lock:
            now = time.time()
            self.previews = {key: value for key, value in self.previews.items() if now - value["created"] < 600}
            while len(self.previews) >= 12:
                self.previews.pop(next(iter(self.previews)))
            preview_id = secrets.token_urlsafe(18)
            self.previews[preview_id] = {"created": now, "messages": prepared["messages"]}
        return {**prepared, "preview_id": preview_id}

    def approved(self, preview_id, selected_ids):
        if not isinstance(preview_id, str) or not isinstance(selected_ids, list) or not selected_ids or len(selected_ids) > 200 or any(not isinstance(i, str) for i in selected_ids):
            raise ValueError("请先预览并选择需要分析的文字。")
        with self.lock:
            snapshot = self.previews.get(preview_id)
            if not snapshot or time.time() - snapshot["created"] >= 600:
                raise ValueError("预览已过期，请重新打开预览后分析。")
            wanted = set(selected_ids)
            valid = {m["id"] for m in snapshot["messages"]}
            if not wanted <= valid:
                raise ValueError("选择包含预览之外的消息，请重新预览。")
            return [m for m in snapshot["messages"] if m["id"] in wanted]

    def start_approved(self, preview_id, selected_ids):
        # A lost HTTP response or repeated click must not start a second paid job.
        with self.lock:
            chosen = self.approved(preview_id, selected_ids)
            snapshot = self.previews[preview_id]
            selection = tuple(sorted(set(selected_ids)))
            previous = snapshot.setdefault("jobs", {}).get(selection)
            if previous in self.jobs:
                return previous, len(chosen)
            job_id = self.start(chosen)
            snapshot["jobs"][selection] = job_id
            return job_id, len(chosen)


def _messages(body: dict) -> list[dict]:
    rows = body.get("messages")
    if not isinstance(rows, list) or len(rows) > MAX_MESSAGES:
        raise ValueError(f"需要 messages 数组，最多 {MAX_MESSAGES} 条消息。")
    if any(not isinstance(m, dict) for m in rows):
        raise ValueError("消息列表中存在非对象记录。")
    # Reuse the import boundary, even for browser JSON, to keep normalization consistent.
    return normalize_import(json.dumps({"messages": rows}, ensure_ascii=False), "normalized.json")["messages"]


def _analyses(body: dict) -> list[dict]:
    rows = body.get("analyses", [])
    if not isinstance(rows, list) or len(rows) > MAX_MESSAGES or any(not isinstance(a, dict) for a in rows):
        raise ValueError("分析结果格式无效。")
    return rows


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state=None):
        self.state = state or AppState()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "Chatprint"

    def log_message(self, *args):
        pass  # Private query parameters, conversation content and keys never enter logs.

    def _safe_request(self):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            self._json({"error": "仅允许本机访问。"}, 403); return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed}:
            self._json({"error": "不接受其他网页发起的请求。"}, 403); return False
        return True

    def _headers(self, code, content_type, size):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()

    def _json(self, data, code=200):
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def do_GET(self):
        if not self._safe_request():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/settings":
                self._json(self.server.state.settings())
            elif path == "/api/demo":
                data = make_demo()
                data["analyses"] = data.pop("demo_analyses", data.get("analyses", []))
                data["analysis_source"] = "demo"
                self._json(data)
            elif path == "/api/wechat/status":
                self._json(wechat.cli_status())
            elif path == "/api/wechat/sessions":
                self._json(wechat.sessions())
            elif path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                with self.server.state.lock:
                    job = self.server.state.jobs.get(job_id)
                    self._json(job if job else {"error": "没有找到该分析任务。"}, 200 if job else 404)
            elif path.startswith("/api/"):
                self._json({"error": "接口不存在。"}, 404)
            else:
                name = "index.html" if path == "/" else unquote(path).lstrip("/")
                file = (WEB / name).resolve()
                if not file.is_relative_to(WEB.resolve()) or file.suffix not in (".html", ".css", ".js", ".svg", ".png", ".ico") or not file.is_file():
                    self._json({"error": "页面不存在。"}, 404); return
                data = file.read_bytes()
                mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                self._headers(200, mime + ("; charset=utf-8" if mime.startswith("text/") or file.suffix == ".js" else ""), len(data))
                self.wfile.write(data)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json({"error": "读取失败，请检查文件或重试。"}, 500)

    def do_POST(self):
        if not self._safe_request():
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json({"error": "请求必须使用 JSON。"}, 415); return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                self._json({"error": "数据为空或超过 30 MB。"}, 413); return
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("请求必须是 JSON 对象。")
            path = urlsplit(self.path).path
            if path == "/api/import":
                content = body.get("content")
                if not isinstance(content, str):
                    raise ValueError("缺少文件内容。")
                data = normalize_import(content, str(body.get("filename", "chat.json")))
                if len(data["messages"]) > MAX_MESSAGES:
                    raise ValueError("单次最多导入 50000 条消息，请拆分文件。")
                self._json(data)
            elif path == "/api/summary":
                self._json(summarize(_messages(body), _analyses(body), body.get("filters", {})))
            elif path == "/api/settings":
                self._json(self.server.state.configure(body.get("api_key", "")))
            elif path in ("/api/preview", "/api/analyze"):
                if body.get("anonymize", True) is not True:
                    raise ValueError("当前版本仅允许预览并分析自动脱敏后的文字。")
                if path == "/api/preview":
                    preview = self.server.state.preview(_messages(body), body.get("limit", 30), body.get("exclude_ids"))
                    self._json(preview)
                else:
                    if "preview_id" in body:
                        job_id, selected = self.server.state.start_approved(body["preview_id"], body.get("selected_ids"))
                    else:
                        chosen = prepare_messages(_messages(body), body.get("limit", 30), anonymize=True)["messages"]
                        job_id, selected = self.server.state.start(chosen), len(chosen)
                    self._json({"job_id": job_id, "selected": selected}, 202)
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                with self.server.state.lock:
                    job = self.server.state.jobs.get(job_id)
                    if not job:
                        self._json({"error": "没有找到该任务。"}, 404); return
                    job["cancel_requested"] = True
                    self._json({"ok": True, "note": "当前已发出的请求会完成，后续批次将停止。"})
            elif path == "/api/report":
                self._json({"markdown": make_report(_messages(body), _analyses(body), anonymize=body.get("anonymize", True) is not False, filters=body.get("filters", {}))})
            elif path == "/api/wechat/history":
                self._json(wechat.history(body.get("chat"), body.get("limit", 1000)))
            else:
                self._json({"error": "接口不存在。"}, 404)
        except (ValueError, KeyError, TypeError) as exc:
            self._json({"error": str(exc) if not isinstance(exc, TypeError) else "字段类型无效，请检查输入。"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json({"error": "处理失败，未写入消息或密钥。"}, 500)


def serve(port=8765, open_browser=True):
    server = LocalServer(("127.0.0.1", port))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Chatprint {__version__} · {url}", flush=True)
    print("关闭此窗口或按 Ctrl+C 结束。消息和设置仅保存在本次运行的内存中。", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
