"""
CLI 入口
提供交互式命令行界面，连接 GitHub MCP 服务并启动检索分析 Agent。
"""

import asyncio
import logging
import os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    MCP_COMMAND,
    MCP_ARGS,
    MCP_ENV,
    MEMORY_FILE,
)
from mcp_agent import McpAgent
from prompts import GITHUB_SEARCH_PROMPT, SEARCH_PROMPT_TEMPLATE, ANALYZE_PROMPT_TEMPLATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# NOTE: 使用 GitHub 专用 System Prompt 替代通用 prompt
SYSTEM_PROMPT = GITHUB_SEARCH_PROMPT


def printHelp():
    """打印帮助信息"""
    print("─" * 55)
    print("  /search <需求>    搜索匹配开源项目并排序")
    print("  /analyze <URL>    精准分析指定 GitHub 仓库")
    print("  /clear            清除对话记忆")
    print("  /tools            查看可用 MCP 工具")
    print("  /help             显示此帮助")
    print("  /quit             退出并保存记忆")
    print("─" * 55)
    print("  也可以直接输入自然语言对话\n")


async def main():
    """主函数：初始化 Agent → 连接 MCP → 交互循环"""
    agent = McpAgent(
        apiKey=LLM_API_KEY,
        baseUrl=LLM_BASE_URL,
        model=LLM_MODEL,
        systemPrompt=SYSTEM_PROMPT,
    )

    # 恢复历史记忆
    if MEMORY_FILE and os.path.exists(MEMORY_FILE):
        agent.loadMemory(MEMORY_FILE)

    # 构建 MCP 服务端参数，合并环境变量
    env = {**os.environ, **MCP_ENV} if MCP_ENV else None
    serverParams = StdioServerParameters(command=MCP_COMMAND, args=MCP_ARGS, env=env)

    print(f"\n🔍 GitHub 检索分析 Agent 已启动 (模型: {LLM_MODEL})")
    print(f"📡 MCP 服务: {MCP_COMMAND} {' '.join(MCP_ARGS)}")
    printHelp()

    async with stdio_client(serverParams) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            toolNames = await agent.connect(session)
            print(f"✅ 已加载 {len(toolNames)} 个工具: {', '.join(toolNames[:10])}")
            if len(toolNames) > 10:
                print(f"   ...及其他 {len(toolNames) - 10} 个工具")
            print()

            # 交互循环
            while True:
                try:
                    userInput = input("👤 You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break

                if not userInput:
                    continue

                # 内置命令
                if userInput == "/quit":
                    break
                elif userInput == "/clear":
                    agent.clearMemory()
                    print("🗑️  记忆已清除\n")
                    continue
                elif userInput == "/tools":
                    print(f"🔧 可用工具: {', '.join(toolNames)}\n")
                    continue
                elif userInput == "/help":
                    printHelp()
                    continue
                elif userInput.startswith("/search "):
                    # 快捷搜索：将用户需求包装为搜索指令
                    query = userInput[8:].strip()
                    if not query:
                        print("⚠️  请输入搜索需求，例如: /search 轻量级 Python WAF\n")
                        continue
                    userInput = SEARCH_PROMPT_TEMPLATE.format(user_query=query)
                    print(f"🔍 正在搜索: {query}\n")
                elif userInput.startswith("/analyze "):
                    # 快捷分析：将 URL 包装为分析指令
                    repoUrl = userInput[9:].strip()
                    if not repoUrl:
                        print("⚠️  请输入仓库地址，例如: /analyze https://github.com/owner/repo\n")
                        continue
                    userInput = ANALYZE_PROMPT_TEMPLATE.format(repo_url=repoUrl)
                    print(f"📊 正在分析: {repoUrl}\n")

                # Agent 对话
                try:
                    reply = await agent.chat(userInput)
                    print(f"\n🤖 Agent: {reply}\n")
                except Exception as e:
                    logger.error("Agent 处理异常: %s", e)
                    print(f"\n❌ 错误: {e}\n")

    # 退出前保存记忆
    if MEMORY_FILE:
        agent.saveMemory(MEMORY_FILE)
        print(f"💾 记忆已保存到 {MEMORY_FILE}")

    print("👋 再见！")


if __name__ == "__main__":
    asyncio.run(main())
