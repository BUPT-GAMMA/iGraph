#!/usr/bin/env python3
"""
iGraph 命令行入口
直接使用 iGraph 进行对话，不启动 HTTP 服务。
"""

import os
import sys
import uuid

from backend.agent.igraph import IGraph
from backend.logging.logger import Logger
from backend.runtime.dotenv_compat import load_dotenv_compat
from backend.runtime.proxy_compat import setup_no_proxy
from backend.runtime.env_required import require_env, require_int_env
from backend.runtime.process_supervisor import ProcessSupervisor

load_dotenv_compat()
setup_no_proxy()

BANNER = r"""
╔══════════════════════════════════════════════════════╗
║                    iGraph                            ║
║       Interactive Graph Analysis System              ║
║                                                      ║
║    支持: Claude / GPT / Qwen(OpenAI Compatible)      ║
║                                                      ║
║    直接对话即可 - 发送图数据自动加载，提问自动分析      ║
╚══════════════════════════════════════════════════════╝
"""

USAGE = """\
使用方式:
  1. 直接将图的边数据粘贴到对话中，Agent 会自动加载
  2. 图加载后，用自然语言提问进行分析

示例输入:
  "请加载以下图:
   A B 4
   A C 2
   B C 1
   B D 5
   C D 8
   D E 2
   E F 3"

  然后提问: "A到F的最短路径是什么？"

命令: quit/exit 退出
"""


def select_model() -> str:
    print("请选择模型:")
    print("  [1] claude-opus-4-6")
    print("  [2] claude-sonnet-4-6")
    print("  [3] gpt-5-4")
    print("  [4] gpt-5-2")
    print("  [5] qwen3.6-plus")
    print("  [6] qwen3.7-max")
    choice = input("\n请输入 1-6 [默认 4]: ").strip() or "4"
    mapping = {
        "1": "claude-opus-4-6",
        "2": "claude-sonnet-4-6",
        "3": "gpt-5-4",
        "4": "gpt-5-2",
        "5": "qwen3.6-plus",
        "6": "qwen3.7-max",
    }
    return mapping.get(choice, "gpt-5-2")


def main():
    print(BANNER)
    print(USAGE)

    model = select_model()
    mcp_port = require_int_env("MCP_PORT")
    graph_data_port = require_int_env("GRAPH_DATA_PORT")
    mcp_url = require_env("MCP_SERVER_URL")
    graph_data_url = require_env("GRAPH_DATA_SERVER_URL")

    base_dir = os.path.dirname(os.path.abspath(__file__))
    supervisor = ProcessSupervisor(
        base_dir=base_dir,
        graph_data_port=graph_data_port,
        mcp_port=mcp_port,
        graph_data_url=graph_data_url,
    )
    print("\n⏳ 正在启动 GraphData Server ...")
    print("⏳ 正在启动 MCP server ...")
    supervisor.start_all()

    print(f"⏳ 正在初始化 Agent (model={model}) ...")
    try:
        session_id = str(uuid.uuid4())[:8]
        logger = Logger()
        session_logger = logger.create_session_logger(
            session_id, provider="transparent_openai_sdk", model=model
        )
        agent = IGraph(
            session_logger=session_logger,
            model=model,
            mcp_url=mcp_url,
        )
        print(f"✅ Agent 就绪  |  会话 ID: {agent.session_id}")
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        sys.exit(1)

    print("\n" + "=" * 55)
    print(" 开始对话（直接粘贴图数据或输入问题）")
    print("=" * 55)

    while True:
        try:
            lines = []
            first_line = input("\n🧑 You > ").rstrip()
            if not first_line:
                continue

            if first_line.lower().strip() in ("quit", "exit", "q"):
                break

            lines.append(first_line)

            if _looks_like_multiline(first_line):
                print("   (检测到可能的多行输入，继续输入，空行结束)")
                while True:
                    extra = input("       > ").rstrip()
                    if not extra:
                        break
                    lines.append(extra)

            user_input = "\n".join(lines)

        except (EOFError, KeyboardInterrupt):
            break

        try:
            response = agent.chat(user_input)
            print(f"\n🤖 Agent:\n{response}")
        except Exception as e:
            print(f"\n❌ 出错: {e}")

    agent.cleanup()
    supervisor.stop_all()
    print("\n👋 再见!")


def _looks_like_multiline(line: str) -> bool:
    stripped = line.strip()
    if any(k in stripped for k in ["以下", "如下", "下面"]) and stripped[-1] in (":", "："):
        return True
    parts = stripped.split()
    if 2 <= len(parts) <= 3:
        try:
            if len(parts) == 3:
                float(parts[2])
            return True
        except ValueError:
            pass
    return False


if __name__ == "__main__":
    main()
