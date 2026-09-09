import os

from dotenv import load_dotenv
from openai import AsyncOpenAI


load_dotenv()


class LLMClient:
    """负责调用大模型，并把回复以增量文本流的形式向外提供。"""

    def __init__(self):
        """创建异步 OpenAI 兼容客户端。"""
        self.client = AsyncOpenAI()

    async def stream(self, messages):
        """根据对话历史生成回复，并逐块产出新增文本。"""
        response = await self.client.chat.completions.create(
            model=os.getenv("LLM_MODEL"),
            messages=messages,
            stream=True,
            extra_body={"enable_thinking": False},
        )

        async for chunk in response:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content or ""
            if text:
                yield text
