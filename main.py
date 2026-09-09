"""兼容原有命令的直连语音助手入口。"""

import asyncio

from vvc.direct.application import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n语音助手已退出。")
