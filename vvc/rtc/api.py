import json
from datetime import datetime

from volcengine.ApiInfo import ApiInfo
from volcengine.Credentials import Credentials
from volcengine.ServiceInfo import ServiceInfo
from volcengine.base.Service import Service


class RTCAPI:
    """调用火山引擎 RTC OpenAPI，并由官方 SDK 完成 AK/SK 签名。"""

    VERSION = "2025-06-01"
    ACTION_VERSIONS = {
        "RegisterVoicePrint": "2024-12-01",
        "ListVoicePrint": "2024-12-01",
    }

    def __init__(self, access_key, secret_key):
        service_info = ServiceInfo(
            "rtc.volcengineapi.com",
            {"Accept": "application/json"},
            Credentials("", "", "rtc", "cn-beijing"),
            10,
            30,
        )
        actions = {
            name: ApiInfo(
                "POST",
                "/",
                {
                    "Action": name,
                    "Version": self.ACTION_VERSIONS.get(name, self.VERSION),
                },
                {},
                {},
            )
            for name in (
                "RegisterVoicePrint",
                "ListVoicePrint",
                "StartVoiceChat",
                "StopVoiceChat",
            )
        }
        self._service = Service(service_info, actions)
        self._service.set_ak(access_key)
        self._service.set_sk(secret_key)

    def call(self, action, body):
        """发送 JSON 请求并统一处理空响应和服务端错误。"""
        started_at = datetime.now().strftime("%H:%M:%S")
        print(f"[{started_at}] RTC OpenAPI -> {action}")
        raw = self._service.json(action, {}, json.dumps(body, ensure_ascii=False))
        if not raw:
            raise RuntimeError(f"RTC {action} 返回了空响应")

        response = json.loads(raw)
        metadata = response.get("ResponseMetadata") or {}
        error = metadata.get("Error")
        if error:
            code = error.get("Code", "Unknown")
            message = error.get("Message", "")
            request_id = metadata.get("RequestId", "-")
            print(f"RTC {action} 失败: code={code}, requestId={request_id}, message={message}")
            raise RuntimeError(f"RTC {action} 失败: {code} - {message}")
        request_id = metadata.get("RequestId", "-")
        print(f"RTC {action} 已受理: requestId={request_id}")
        return response

    def register_voiceprint(self, app_id, name, wav_data):
        """上传一段 WAV 并返回服务端生成的长期声纹 ID。"""
        import base64

        response = self.call(
            "RegisterVoicePrint",
            {
                "AppId": app_id,
                "Audio": base64.b64encode(wav_data).decode("ascii"),
                "AudioName": name,
                "MetaInfo": name,
            },
        )
        print(f"声纹注册成功: {response}")
        voiceprint_id = response.get("Result") or _find_value(response, "VoicePrintId")
        if not voiceprint_id:
            raise RuntimeError("声纹注册成功响应中没有声纹 ID")
        return voiceprint_id

    def start_voice_chat(self, body):
        return self.call("StartVoiceChat", body)

    def stop_voice_chat(self, app_id, room_id, task_id):
        return self.call(
            "StopVoiceChat",
            {"AppId": app_id, "RoomId": room_id, "TaskId": task_id},
        )


def _find_value(value, key):
    """兼容 RTC 响应中 Result 的不同嵌套层级。"""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_value(child, key)
            if found is not None:
                return found
    return None
