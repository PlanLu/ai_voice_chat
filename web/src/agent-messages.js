import { deepFind } from './feeds.js';

const AI_ERROR_DESCRIPTIONS = {
  1003001: 'ASR 实例化失败：检查 ASR Provider、ResourceId 和服务配置',
  1003006: 'ASR 建联失败：检查 ASR AppId、AccessToken、服务开通和网络权限',
  1004001: 'LLM 实例化失败：检查方舟 EndPointId 和模型配置',
  1004005: 'LLM 建联失败：检查方舟 Endpoint、API 权限和网络连接',
  1005001: 'TTS 实例化失败：检查 TTS Provider、ResourceId 和音色配置',
  1005005: 'TTS 建联失败：检查 TTS AppId、Token、ResourceId 和音色权限',
};

function decodeTlv(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 8) return null;
  const bytes = new Uint8Array(buffer);
  const type = new TextDecoder().decode(bytes.slice(0, 4));
  const length = new DataView(buffer).getUint32(4, false);
  if (length > buffer.byteLength - 8) return null;
  const text = new TextDecoder().decode(bytes.slice(8, 8 + length));
  return { type, payload: JSON.parse(text) };
}

function describeAiError(code, reason = '') {
  const description = AI_ERROR_DESCRIPTIONS[code]
    || 'AI 任务初始化失败：请根据错误码查询官方文档';
  const authorization = /requested resource not granted/i.test(reason)
    ? '；检测到资源未授权，请检查 RTC 跨服务授权 VoiceChatRoleForRTC'
    : '';
  return `${description}${authorization}`;
}

export function createAgentMessageHandler({ getConfig, conversation, log, onError }) {
  return (event) => {
    try {
      const decoded = decodeTlv(event.message);
      if (decoded?.type === 'subv' && Array.isArray(decoded.payload?.data)) {
        for (const subtitle of decoded.payload.data) {
          conversation.render(subtitle, getConfig());
        }
        return;
      }

      if (decoded?.type === 'conv') {
        const stage = decoded.payload?.Stage || {};
        const stageCode = Number(stage.Code ?? decoded.payload?.Code);
        const errorInfo = decoded.payload?.ErrorInfo || stage.ErrorInfo || {};
        const errorCode = Number(
          errorInfo.Code
          ?? errorInfo.ErrorCode
          ?? decoded.payload?.ErrorCode
          ?? 0
        );
        const reason = errorInfo.Reason || errorInfo.Message || decoded.payload?.Message || '';
        const description = stage.Description || '无描述';
        if (errorCode > 0) {
          const diagnosis = describeAiError(errorCode, reason);
          log.add('AI 任务错误', {
            errorCode: errorCode || stageCode,
            diagnosis,
            reason: reason || description,
            taskId: decoded.payload?.TaskId,
          });
          onError(`AI 任务失败（${errorCode || stageCode}）：${diagnosis}`);
        } else {
          log.add(`AI 状态：${description}`, {
            stageCode,
            taskId: decoded.payload?.TaskId,
          });
        }
        return;
      }

      log.add(`收到 ${decoded?.type || '未知'} 消息`, decoded?.payload || {
        bytes: event.message?.byteLength || 0,
      });
    } catch (error) {
      log.add('消息解析失败', {
        message: error.message,
        bytes: event.message?.byteLength || 0,
      });
    }
  };
}

export { decodeTlv, deepFind };
