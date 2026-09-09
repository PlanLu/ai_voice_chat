import argparse
import base64
import json
import mimetypes
import os
import threading
import traceback
import uuid
from datetime import datetime
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
LATENCY_LOG = ROOT / "test.log"
ACTIVE_TASKS = {}
TASK_LOCK = threading.Lock()
VOICEPRINT_LOCK = threading.Lock()
LATENCY_LOCK = threading.Lock()


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


def _fmt_ms(value):
    return "—" if value is None else f"{value:.0f}"


def _turn_metrics(turn):
    """从一轮的 T0-T4 原始时间戳（ms）计算三项指标。

    指标1/2 仅在 T1/T2 来自真实用户字幕时才统计；由助手字幕兜底填充
    的样本（说明该轮未抓到用户字幕）会被剔除，以免污染均值。
    指标3 允许为负：负值说明 TTS 音频早于助手字幕上屏。
    aborted=True 的轮次（对话结束时未完成）三项指标均为 None。
    """
    if turn.get("aborted"):
        return None, None, None
    t0 = turn.get("t0")
    t1 = turn.get("t1")
    t2 = turn.get("t2")
    t3 = turn.get("t3")
    t4 = turn.get("t4")
    metric1 = (
        t1 - t0
        if t0 is not None and t1 is not None
        and turn.get("t0Source") == "volume" and turn.get("t1Source") == "user"
        else None
    )
    metric2 = (
        t3 - t2
        if t2 is not None and t3 is not None and turn.get("t2Source") == "user"
        else None
    )
    metric3 = t4 - t3 if t3 is not None and t4 is not None else None
    return metric1, metric2, metric3


def build_latency_report(payload):
    """把前端上报的各轮 T0-T4 汇总成 test.log 文本，并返回平均值。"""
    turns = payload.get("turns") or []
    samples = [[], [], []]
    detail_lines = []
    for turn in turns:
        metric1, metric2, metric3 = _turn_metrics(turn)
        for bucket, value in zip(samples, (metric1, metric2, metric3)):
            if value is not None:
                bucket.append(value)
        raw = (
            f"T0={_fmt_ms(turn.get('t0'))} T1={_fmt_ms(turn.get('t1'))} "
            f"T2={_fmt_ms(turn.get('t2'))} T3={_fmt_ms(turn.get('t3'))} "
            f"T4={_fmt_ms(turn.get('t4'))}"
        )
        if turn.get("aborted"):
            note = "  [注:对话结束时本轮未完成，已排除]"
        elif turn.get("t1Source") == "assistant-fallback" or turn.get("t2Source") == "assistant-fallback":
            note = "  [注:未抓到用户字幕，指标1/2 已排除]"
        elif turn.get("t0Source") == "subtitle":
            note = "  [注:T0 来自字幕兜底，指标1 已排除]"
        else:
            note = ""
        detail_lines.append(
            f"轮次 {turn.get('index', '?')}: "
            f"指标1={_fmt_ms(metric1)}ms 指标2={_fmt_ms(metric2)}ms 指标3={_fmt_ms(metric3)}ms"
            f"  [{raw}]{note}"
        )
    averages = [
        (sum(bucket) / len(bucket)) if bucket else None for bucket in samples
    ]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 64,
        f"RTC 网页模式延迟测试报告  {now}",
        f"房间: {payload.get('roomId', '-')}   总轮次: {len(turns)}",
        "说明: T0说话 T1 ASR首字 T2识别结束 T3回复首字 T4 TTS首帧（均为 ms）",
        "-" * 64,
    ]
    lines.extend(detail_lines or ["（本次无有效对话轮次）"])
    lines.extend([
        "-" * 64,
        f"平均:  指标1(说话→ASR首字)={_fmt_ms(averages[0])}ms (样本{len(samples[0])})  "
        f"指标2(识别结束→回复首字)={_fmt_ms(averages[1])}ms (样本{len(samples[1])})  "
        f"指标3(回复首字→TTS首帧)={_fmt_ms(averages[2])}ms (样本{len(samples[2])})",
        "=" * 64,
    ])
    summary = {
        "metric1": averages[0],
        "metric2": averages[1],
        "metric3": averages[2],
    }
    return "\n".join(lines), summary


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
            elif path == "/api/latency/report":
                self.report_latency(body)
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

    def report_latency(self, body):
        text, averages = build_latency_report(body)
        with LATENCY_LOCK:
            with open(LATENCY_LOG, "a", encoding="utf-8") as handle:
                handle.write(text + "\n\n")
        print(text)
        self.send_json(200, {
            "ok": True,
            "path": str(LATENCY_LOG),
            "totalTurns": len(body.get("turns") or []),
            "averages": averages,
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
