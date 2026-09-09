import argparse
import base64
import json
import mimetypes
import os
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from vvc.rtc.api import RTCAPI
from vvc.rtc.token import create_rtc_token
from vvc.rtc.voice_chat import build_start_request
from vvc.shared.voiceprint_registry import VoiceprintRegistry


ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = ROOT / "web" / "dist"
ACTIVE_TASKS = {}
TASK_LOCK = threading.Lock()
VOICEPRINT_LOCK = threading.Lock()


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def public_config():
    registry = VoiceprintRegistry()
    registered_voiceprints = registry.load()
    return {
        "appId": require_env("RTC_APP_ID"),
        "roomId": os.getenv("RTC_ROOM_ID", "voice-room"),
        "userId": os.getenv("RTC_USER_ID", "user-1"),
        "botUserId": os.getenv("RTC_BOT_USER_ID", "voice-assistant"),
        "voiceprintCount": len(registered_voiceprints),
        "voiceprints": {
            voiceprint_id: name
            for name, voiceprint_id in registered_voiceprints.items()
        },
        "appKeyConfigured": bool(os.getenv("RTC_APP_KEY", "").strip()),
    }


def rtc_client():
    return RTCAPI(require_env("VOLC_ACCESS_KEY"), require_env("VOLC_SECRET_KEY"))


def response_summary(response):
    metadata = response.get("ResponseMetadata") or {}
    return {
        "requestId": metadata.get("RequestId"),
        "action": metadata.get("Action"),
        "result": response.get("Result"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "RTCVoiceChat/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            try:
                config = public_config()
                config["token"] = create_rtc_token(
                    config["appId"], require_env("RTC_APP_KEY"),
                    config["roomId"], config["userId"], 3600,
                )
                self.send_json(200, config)
            except Exception as exc:
                self.send_json(500, {"error": str(exc)})
            return
        self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/session/start":
                self.start_session(body)
            elif path == "/api/session/stop":
                self.stop_session(body)
            elif path == "/api/voiceprint/register":
                self.register_voiceprint(body)
            else:
                self.send_json(404, {"error": "接口不存在"})
        except Exception as exc:
            print(f"请求处理失败: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            self.send_json(400, {"error": str(exc)})

    def register_voiceprint(self, body):
        name = str(body.get("name", "")).strip()
        encoded_audio = body.get("audio")
        if not name or len(name) > 80:
            raise ValueError("请输入 1～80 个字符的姓名")
        if not isinstance(encoded_audio, str) or not encoded_audio:
            raise ValueError("缺少录音数据")
        try:
            wav_data = base64.b64decode(encoded_audio, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("录音数据格式无效") from exc
        if not wav_data.startswith(b"RIFF") or wav_data[8:12] != b"WAVE":
            raise ValueError("录音必须是 WAV 格式")
        if len(wav_data) > 1_500_000:
            raise ValueError("录音文件过大")

        with VOICEPRINT_LOCK:
            voiceprint_id = rtc_client().register_voiceprint(
                require_env("RTC_APP_ID"), name, wav_data
            )
            VoiceprintRegistry().save(name, voiceprint_id)
        self.send_json(200, {"name": name, "voiceprintId": voiceprint_id})

    def start_session(self, body):
        config = public_config()
        room_id = body.get("roomId", config["roomId"])
        user_id = body.get("userId", config["userId"])
        if room_id != config["roomId"] or user_id != config["userId"]:
            raise ValueError("RoomId/UserId 必须与服务端配置一致")
        with TASK_LOCK:
            existing = ACTIVE_TASKS.get(room_id)
            if existing:
                self.send_json(200, {"taskId": existing, "reused": True})
                return
            task_id = f"voice-{uuid.uuid4().hex[:12]}"
            request = build_start_request(
                room_id, user_id, task_id, VoiceprintRegistry().voiceprint_ids()
            )
            print(
                "启动参数摘要: "
                f"room={room_id}, user={user_id}, bot={config['botUserId']}, "
                f"voiceprints={len(VoiceprintRegistry().voiceprint_ids())}, "
                f"asr={request['Config']['ASRConfig']['ProviderParams']['Mode']}, "
                f"ttsResource={request['Config']['TTSConfig']['ProviderParams']['ResourceId']}, "
                f"llm={request['Config']['LLMConfig']['Mode']}"
            )
            response = rtc_client().start_voice_chat(request)
            ACTIVE_TASKS[room_id] = task_id
        self.send_json(200, {
            "taskId": task_id,
            "reused": False,
            "openApi": response_summary(response),
        })

    def stop_session(self, body):
        config = public_config()
        room_id = body.get("roomId", config["roomId"])
        task_id = body.get("taskId")
        with TASK_LOCK:
            task_id = task_id or ACTIVE_TASKS.get(room_id)
            if not task_id:
                self.send_json(200, {"stopped": False})
                return
            response = rtc_client().stop_voice_chat(config["appId"], room_id, task_id)
            if ACTIVE_TASKS.get(room_id) == task_id:
                ACTIVE_TASKS.pop(room_id, None)
        self.send_json(200, {"stopped": True, "openApi": response_summary(response)})

    def serve_static(self, request_path):
        if not WEB_DIST.exists():
            self.send_json(503, {"error": "网页尚未构建，请先运行 cd web; npm install; npm run build"})
            return
        relative = request_path.lstrip("/") or "index.html"
        target = (WEB_DIST / relative).resolve()
        if WEB_DIST.resolve() not in target.parents and target != WEB_DIST.resolve():
            self.send_json(403, {"error": "禁止访问"})
            return
        if not target.is_file():
            target = WEB_DIST / "index.html"
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="启动 RTC 单人语音实验网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"RTC 语音实验页面：http://localhost:{args.port}")
    print("按 Ctrl+C 退出。结束实验前请先在页面点击“结束对话”。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
