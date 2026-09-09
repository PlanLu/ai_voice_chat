# RTC 多人声纹语音对话

这是最小版本，支持注册 1～3 个人的长期声纹，并在 RTC AI 对话中开启多人声纹识别。

## 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

## 2. 配置

把 `.env.example` 中新增的 RTC、ASR、TTS 和方舟参数填写到 `.env`。

## 3. 注册声纹

每个人分别录制一次，建议在安静环境中连续说 10 秒：

```powershell
python register_voiceprint.py 张三
python register_voiceprint.py 李四
```

注册成功后，姓名和服务端声纹 ID 会保存在本地 `voiceprints.json`。该文件已加入 `.gitignore`。

## 4. 启动对话任务

```powershell
python rtc_voice_chat.py start --room voice-room --user user-1
```

随后 RTC 客户端必须使用相同的 `RTC_APP_ID`、RoomId 和 UserId 加入房间并发布麦克风音频。服务端会同时进行 ASR、多人声纹匹配、LLM 回复和 TTS 播放。

官方多人声纹识别一次最多传入 3 个声纹 ID。

## 5. 显示实名字幕

RTC 客户端收到字幕回调后，把消息 JSON 交给 `format_subtitle()`，它会把声纹 ID 转为姓名：

```text
张三：今天天气好吗？
李四：我的问题跟你一样。
助手：今天天气很好。
```

`rtc_voice_chat.py format --message '{...}'` 可用于手工验证字幕解析。实际 GUI 或 RTC 客户端应直接调用 `format_subtitle()`。

## 当前边界

本仓库是 Python 命令行程序，火山 RTC 的麦克风入房和字幕回调由 RTC 客户端 SDK 提供，不是普通 WebSocket。这里已经完成服务端任务、声纹注册、声纹配置和实名字幕转换；运行完整通话时仍需在客户端接入对应平台的火山 RTC SDK。
