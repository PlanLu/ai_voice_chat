# AI 语音助手

一个基于 Python、豆包语音、OpenAI 兼容大模型和火山引擎 RTC 的中文语音对话项目，同时提供本机直连和 RTC 网页两条独立链路。

## 功能概览

### 直连模式

```text
本机麦克风 → 豆包流式 ASR → OpenAI 兼容 LLM → 豆包双向流式 TTS → 本机扬声器
```

- 流式语音识别与说话人分离
- 服务端 VAD 判停
- ASR 热词
- LLM 流式回复
- 双向流式语音合成和播放
- 播放结束缓冲，避免回复尾音被声卡截断

### RTC 网页模式

```text
浏览器 RTC 客户端 ←→ 火山 RTC 房间 ←→ 云端 AI 对话任务
                                         ASR → LLM → TTS
```

- 浏览器采集并发布麦克风音频
- 自动订阅和播放 AI 助手音频
- Python 服务端生成 RTC Token
- Python 服务端启动和停止 `VoiceChat` 云端任务
- 实时对话与运行日志分区显示
- 用户和助手聊天气泡、实时字幕更新和自动滚动
- RTC 会话状态管理
- 网页和命令行声纹注册
- 最多同时匹配 3 个已注册声纹
- 声纹匹配成功后将当前气泡的“用户”更新为注册姓名

Web 页面只是 RTC 媒体客户端的一种实现。RTC 房间不需要提前创建，但必须有客户端负责入房、发布麦克风音频和订阅 AI 音频。单独调用 `StartVoiceChat` 只能启动云端任务，不能代替 RTC 客户端。

## 环境要求

- Python 3.10+
- Node.js 20+
- 麦克风和扬声器
- 已开通豆包语音服务
- RTC 模式还需要开通火山 RTC、AI 音视频互动和声纹服务

## 安装

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

首次使用 RTC 网页时安装并构建前端：

```powershell
cd web
npm install
npm run build
cd ..
```

前端代码修改后需要重新执行 `npm run build`。如果浏览器仍显示旧页面，可按 `Ctrl+F5` 强制刷新。

## 配置

复制 `.env.example` 后，在项目根目录的 `.env` 中填写实际配置。

```dotenv
# 直连模式使用的 OpenAI 兼容大模型
LLM_MODEL=
OPENAI_API_KEY=
OPENAI_BASE_URL=

# 豆包语音
DOUBAO_SPEECH_API_KEY=
DOUBAO_SPEECH_RESOURCE_ID=
DOUBAO_ASR_HOTWORD_TABLE_ID=
DOUBAO_TTS_RESOURCE_ID=seed-tts-2.0
DOUBAO_TTS_VOICE_TYPE=zh_female_vv_uranus_bigtts

# 火山 RTC
VOLC_ACCESS_KEY=
VOLC_SECRET_KEY=
RTC_APP_ID=
RTC_APP_KEY=
RTC_ROOM_ID=voice-room
RTC_USER_ID=user-1
RTC_BOT_USER_ID=voice-assistant
RTC_TASK_ID=
VOICEPRINT_SCORE=40

# RTC StartVoiceChat 相关配置
DOUBAO_ASR_APP_ID=
DOUBAO_ASR_ACCESS_TOKEN=
DOUBAO_TTS_APP_ID=
DOUBAO_TTS_ACCESS_TOKEN=
ARK_ENDPOINT_ID=
```

安全注意事项：

- `.env` 包含密钥，不能提交到版本库。
- `RTC_APP_KEY` 只能保存在服务端，不能放进浏览器代码。
- `voiceprints.json` 包含声纹 ID 映射，应作为敏感数据妥善保管。
- Web 服务默认只监听 `127.0.0.1`。没有增加身份认证前，不建议通过 `--host 0.0.0.0` 暴露到局域网或公网。

## ASR 热词

在豆包语音控制台的“自学习平台 → 热词管理”中创建热词文件，然后设置：

```dotenv
DOUBAO_ASR_HOTWORD_TABLE_ID=热词表ID
```

直连和 RTC 模式都会读取该配置。留空表示不启用热词，热词表必须属于当前 ASR 使用的语音应用。

## 运行直连模式

```powershell
python main.py
```

程序会持续监听麦克风，按 `Ctrl+C` 退出。直连模式中的说话人编号只在当前 ASR 会话内有效，不代表注册声纹身份。

## 运行 RTC 网页模式

先确认前端已经构建，然后执行：

```powershell
python rtc_web_server.py
```

浏览器访问：

```text
http://localhost:8765
```

基本流程：

1. 页面向 Python 服务端获取 RTC 配置和 Token。
2. 浏览器申请麦克风权限并加入 RTC 房间。
3. 浏览器发布用户音频。
4. Python 服务端调用 `StartVoiceChat` 启动 AI 助手。
5. AI 助手进房后，浏览器播放回复并显示实时字幕。

页面包括：

- **实时对话**：按时间从上到下显示用户和助手消息；同一分句逐字更新同一个气泡。
- **运行日志**：显示进房、退房、任务状态和错误诊断。
- **麦克风静音**：停止或恢复发布用户音频。
- **添加声纹**：通过弹窗录制 20 秒音频，并显示进度和录音音量。
- **结束对话**：先停止云端 AI 任务，再退出并销毁本地 RTC 连接。

### 正确结束对话

使用结束后应点击页面上的“结束对话”。正常停止会依次完成：

```text
StopVoiceChat → leaveRoom → stopAudioCapture → destroyEngine
```

只有服务端明确确认云端任务停止后，页面才会显示：

```text
已结束，云端 AI 任务和 RTC 连接均已停止
```

如果 `StopVoiceChat` 失败，页面会退出本地 RTC 房间以停止真人侧通话，但保留任务 ID，并允许再次点击“结束对话”重试。不要仅依赖直接关闭浏览器，因为浏览器退出时的停止请求只能尽力发送，不能保证到达服务端。

## 注册声纹

### 网页注册

在 RTC 页面点击“添加声纹”，输入姓名并连续说话 20 秒。录音完成后会自动上传和注册。

### 命令行注册

```powershell
python register_voiceprint.py 张三
```

默认录制 20 秒，也可以指定 15～30 秒：

```powershell
python register_voiceprint.py 张三 --seconds 25
```

注册映射保存在项目根目录的 `voiceprints.json`。声纹属于敏感生物特征数据，采集前应取得本人授权。

声纹匹配规则：

- 未匹配时，对话气泡显示“用户”。
- 当前分句说话过程中匹配成功，会立即把当前气泡更新为注册姓名。
- 已经结束且未匹配的历史气泡不会被追溯修改。
- 页面顶部“用户”显示的是 RTC UserId，不会替换成声纹姓名。

## RTC 命令行任务

命令行入口只负责管理云端 AI 任务，不负责 RTC 客户端入房、麦克风采集和音频播放。

```powershell
# 启动任务
python rtc_voice_chat.py start --room voice-room --user user-1

# 停止任务
python rtc_voice_chat.py stop --room voice-room --task 任务ID

# 格式化字幕消息
python rtc_voice_chat.py format --message '{"Text":"你好","VoicePrintId":"声纹ID"}'
```

## 测试

运行 Python 单元测试：

```powershell
python -m unittest -v
```

验证前端生产构建：

```powershell
cd web
npm run build
```

## 项目结构

```text
main.py                         直连模式兼容入口
rtc_web_server.py               RTC Web 服务兼容入口
rtc_voice_chat.py               RTC 任务管理兼容入口
register_voiceprint.py          声纹注册兼容入口

vvc/
├─ direct/
│  ├─ application.py            直连链路编排
│  ├─ asr_client.py             豆包流式 ASR
│  ├─ llm_client.py             OpenAI 兼容 LLM
│  ├─ tts_client.py             豆包双向流式 TTS
│  ├─ recorder.py               麦克风录音和本地 VAD
│  └─ audio_player.py           PCM 音频播放
├─ rtc/
│  ├─ web_server.py             RTC HTTP 服务和接口
│  ├─ voice_chat.py             VoiceChat 请求配置
│  ├─ api.py                    RTC OpenAPI 客户端
│  ├─ token.py                  RTC Token 生成
│  └─ register_voiceprint.py    声纹注册实现
└─ shared/
   └─ voiceprint_registry.py    姓名与声纹 ID 映射

web/
├─ index.html
└─ src/
   ├─ main.js                   页面流程编排
   ├─ api.js                    后端 API 请求
   ├─ session-state.js          RTC 会话状态机
   ├─ rtc-session.js            RTC 客户端生命周期
   ├─ agent-messages.js         字幕和 AI 状态消息解析
   ├─ feeds.js                  对话窗口和运行日志
   ├─ voiceprint-recorder.js    声纹录音与 WAV 编码
   └─ style.css

tests/                          Python 单元测试
voiceprints.json                本地声纹映射（不会提交到 Git）
```

根目录脚本用于保持原有启动命令不变，具体实现位于 `vvc` 包中。

## 常见问题

### 页面显示的还是旧版本

重新构建并强制刷新：

```powershell
cd web
npm run build
```

然后在浏览器中按 `Ctrl+F5`。

### AI 任务已受理，但助手没有进房

检查：

- RTC AppId、AppKey、RoomId 和 UserId
- AI 音视频互动方案是否开通
- RTC 跨服务授权
- ASR、LLM 和 TTS 服务配置
- 声纹是否至少注册一个且不超过 3 个

### 点击结束对话后提示停止失败

此时浏览器已经退出 RTC 房间，但云端任务尚未确认停止。保持 Python 服务运行，并再次点击“结束对话”。运行日志会保留具体错误信息。
