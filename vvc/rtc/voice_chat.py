import argparse
import json
import os
import uuid

from dotenv import load_dotenv

from vvc.rtc.api import RTCAPI, _find_value
from vvc.shared.voiceprint_registry import VoiceprintRegistry


SYSTEM_PROMPT = (
    "你是一个自然、友好的中文语音助手。你正在与多人交谈，回答要简短、亲切，"
    "并结合每位说话人的姓名理解上下文。"
)


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def build_start_request(room_id, user_id, task_id, voiceprint_ids):
    """构造开启 ASR、LLM、TTS 和多人声纹识别的最小配置。"""
    if not voiceprint_ids:
        raise RuntimeError("请先至少注册一个声纹")
    if len(voiceprint_ids) > 3:
        raise RuntimeError("RTC 多人声纹识别一次最多支持 3 个声纹 ID")

    asr_provider_params = {
        "Mode": "bigmodel",
        "ApiResourceId": require_env("DOUBAO_SPEECH_RESOURCE_ID"),
        "StreamMode": 2,
    }
    hotword_table_id = os.getenv("DOUBAO_ASR_HOTWORD_TABLE_ID")
    if hotword_table_id:
        asr_provider_params["boosting_table_id"] = hotword_table_id

    return {
        "AppId": require_env("RTC_APP_ID"),
        "RoomId": room_id,
        "TaskId": task_id,
        "Config": {
            "ASRConfig": {
                "Provider": "volcano",
                "ProviderParams": asr_provider_params,
            },
            "TTSConfig": {
                "Provider": "volcano_bidirection",
                "ProviderParams": {
                    "audio": {
                        "voice_type": require_env("DOUBAO_TTS_VOICE_TYPE"),
                        "speech_rate": 0,
                    },
                    "ResourceId": require_env("DOUBAO_TTS_RESOURCE_ID"),
                },
            },
            "LLMConfig": {
                "Mode": "ArkV3",
                "ModelName": "doubao-seed-2-0-lite-260428",
                "MaxTokens": 512,
                "Temperature": 0.3,
                "TopP": 0.8,
                "ThinkingType": "disabled",
                "SystemMessages": [SYSTEM_PROMPT],
                "HistoryLength": 10,
            },
            "SubtitleConfig": {"DisableRTSSubtitle": False},
        },
        "AgentConfig": {
            "TargetUserId": [user_id],
            "UserId": require_env("RTC_BOT_USER_ID"),
            "EnableConversationStateCallback": True,
            "VoicePrint": {
                "Mode": 2,
                "IdList": voiceprint_ids,
                "Score": int(os.getenv("VOICEPRINT_SCORE", "40")),
            },
        },
    }


def format_subtitle(message, registry):
    """把 RTC 字幕中的声纹 ID 转成姓名；非字幕消息返回 None。"""
    text = _find_value(message, "text") or _find_value(message, "Text")
    if not text:
        return None

    voiceprint_id = (
        _find_value(message, "VoicePrintId")
        or _find_value(message, "voiceprintId")
        or _find_value(message, "voiceprint_id")
        or _find_value(message, "voice_print_id")
    )
    name = (
        _find_value(message, "voiceprintName")
        or _find_value(message, "VoicePrintName")
        or (registry.name_for(str(voiceprint_id).strip()) if voiceprint_id else None)
    )
    return f"{name or '未知说话人'}：{text}"


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="管理 RTC 多人声纹语音对话任务")
    parser.add_argument("action", choices=("start", "stop", "format"))
    parser.add_argument("--room", default=os.getenv("RTC_ROOM_ID", "voice-room"))
    parser.add_argument("--user", default=os.getenv("RTC_USER_ID", "user-1"))
    parser.add_argument("--task", default=os.getenv("RTC_TASK_ID"))
    parser.add_argument("--message", help="format 模式下的 RTC 字幕 JSON")
    args = parser.parse_args()

    registry = VoiceprintRegistry()

    if args.action == "format":
        if not args.message:
            raise RuntimeError("format 模式需要 --message")
        print(format_subtitle(json.loads(args.message), registry))
        return

    task_id = args.task or f"voice-{uuid.uuid4().hex[:12]}"
    client = RTCAPI(
        require_env("VOLC_ACCESS_KEY"),
        require_env("VOLC_SECRET_KEY"),
    )
    if args.action == "start":
        body = build_start_request(
            args.room, args.user, task_id, registry.voiceprint_ids()
        )
        client.start_voice_chat(body)
        print(f"RTC 对话任务已启动，room={args.room}，task={task_id}")
        print("请让 RTC 客户端使用同一 AppId、RoomId、UserId 加入房间。")
    else:
        if not args.task:
            raise RuntimeError("stop 模式需要 --task")
        client.stop_voice_chat(require_env("RTC_APP_ID"), args.room, args.task)
        print("RTC 对话任务已停止。")


if __name__ == "__main__":
    main()
