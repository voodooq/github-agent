"""
CLI 入口
提供交互式命令行界面，连接 GitHub MCP 服务并启动检索分析 Agent。
"""

import asyncio
import logging
import os
import re
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter, Completer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from config import (
    CLOUD_API_KEY,
    CLOUD_BASE_URL,
    CLOUD_MODEL,
    LOCAL_API_KEY,
    LOCAL_BASE_URL,
    LOCAL_MODEL,
    MCP_COMMAND,
    MCP_ARGS,
    MCP_ENV,
    MEMORY_FILE,
    AGENT_MODE,
    TOKEN_BUDGET,
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
    print("  /review <URL>     触发 Multi-Agent 专家团深度评审")
    print("  /deploy <URL>     一键部署项目到 Docker 沙盒")
    print("  /auto <需求>      🧠 全自治模式：AI 自主拆解、招聘、执行、验收")
    print("  /skills           查看动态技能注册表状态")
    print("  /schedule         📅 查看所有定时任务")
    print("  /wallet           💰 查看 CFO 财务简报（余额/燃烧率/生存模式）")
    print("  /inject <金额>     💵 向 Agent 钱包注资")
    print("  /prune            手动清理 Docker 垃圾镜像/容器")
    print("  /bb               📖 查看黑板报告 (任务事实/执行结果/时间轴)")
    print("  /exp              🧠 查看已积累的执行经验 (AOS 2.4)")
    print("  /clear            清除对话记忆")
    print("  /tools            查看可用 MCP 工具")
    print("  /help             显示此帮助")
    print("  /quit             退出并保存记忆")
    print("─" * 55)
    print("  也可以直接输入自然语言对话\n")


async def main():
    """主函数：初始化 Agent → 连接 MCP → 交互循环"""
    agent = McpAgent(
        cloud_config={
            "api_key": CLOUD_API_KEY,
            "base_url": CLOUD_BASE_URL,
            "model": CLOUD_MODEL,
        },
        local_config={
            "api_key": LOCAL_API_KEY,
            "base_url": LOCAL_BASE_URL,
            "model": LOCAL_MODEL,
        },
        systemPrompt=SYSTEM_PROMPT,
        mode=AGENT_MODE,
    )

    # 加载主对话记忆
    agent.loadMemory("main")

    # 构建 MCP 服务端参数，合并环境变量
    env = {**os.environ, **MCP_ENV} if MCP_ENV else None
    serverParams = StdioServerParameters(command=MCP_COMMAND, args=MCP_ARGS, env=env)

    print(f"\n🚀 OpenClaw Hybrid Agent 已启动 [模式: {AGENT_MODE}]")
    cloud_display = CLOUD_MODEL if agent.unified_client.cloud_available else "[已禁用 (未配置)]"
    local_display = LOCAL_MODEL if agent.unified_client.local_available else "[已禁用 (未配置)]"
    print(f"☁️  线上模型: {cloud_display}")
    print(f"🏠 本地模型: {local_display}")
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

            # 配置自动补全
            from prompts import EXPERT_REGISTRY
            
            # 基础命令映射
            commands = ["/search", "/analyze", "/review", "/deploy", "/auto", "/skills", "/schedule", "/wallet", "/inject", "/prune", "/help", "/clear", "/clear all", "/tools", "/exit", "/quit"]
            meta = {
                "/search": "搜索 GitHub 项目",
                "/analyze": "深入分析单个项目内容",
                "/review": "Multi-Agent 深度评审",
                "/deploy": "一键部署项目到 Docker 沙盒",
                "/auto": "🧠 全自治模式",
                "/skills": "查看动态技能注册表状态",
                "/schedule": "📅 查看定时任务",
                "/wallet": "💰 CFO 财务简报",
                "/inject": "💵 向 Agent 钱包注资",
                "/prune": "清理 Docker 资源",
                "/help": "显示帮助信息",
                "/clear": "清除主对话记忆",
                "/clear all": "全量清理所有 Agent 记忆文件 (核爆级)",
                "/tools": "列出所有可用工具",
                "/exit": "退出程序",
                "/quit": "退出程序",
            }
            
            # 动态添加每个 Agent 的清理命令提示
            for agent_id in EXPERT_REGISTRY:
                cmd = f"/clear {agent_id}"
                commands.append(cmd)
                # 简单从 prompt 提取第一句作为描述
                desc = EXPERT_REGISTRY[agent_id]["prompt"].split('。')[0].split('：')[0].replace("你是一个", "")
                meta[cmd] = f"清理【{desc}】的独立记忆"

            word_completer = WordCompleter(
                commands,
                meta_dict=meta,
                ignore_case=True,
                match_middle=True,
                sentence=True,
                pattern=re.compile(r"(/[a-zA-Z0-9_ ]*)"),  # 允许空格
            )

            class SlashCommandCompleter(Completer):
                def get_completions(self, document, complete_event):
                    # 仅在输入以 / 开头时触发提示，避免输入空格或普通文字时弹出菜单
                    if document.text.startswith('/'):
                        yield from word_completer.get_completions(document, complete_event)

            prompt_session = PromptSession(completer=SlashCommandCompleter(), complete_while_typing=True)

            # 交互循环
            while True:
                try:
                    # 使用 prompt_toolkit 异步获取输入，支持补全
                    userInput = await prompt_session.prompt_async("👤 You: ")
                    userInput = userInput.strip()
                except (EOFError, KeyboardInterrupt):
                    break

                if not userInput:
                    continue

                # 内置命令
                if userInput in ("/quit", "/exit"):
                    break
                elif userInput == "/clear":
                    agent.clearMemory("main")
                    print("🧹 主对话记忆已清除\n")
                    continue
                elif userInput == "/clear all":
                    agent.clearAllMemories()
                    print("💥 所有 Agent 记忆已全量清除 (含文件)\n")
                    continue
                elif userInput.startswith("/clear "):
                    ctx_id = userInput[7:].strip()
                    agent.clearMemory(ctx_id)
                    print(f"🧹 专家记忆 [{ctx_id}] 已清除\n")
                    continue
                elif userInput == "/tools":
                    print(f"🔧 可用工具: {', '.join(toolNames)}\n")
                    continue
                elif userInput == "/help":
                    printHelp()
                    continue
                elif userInput.startswith("/search "):
                    # 快捷搜索：将用户需求包装为搜索指令，强制云端（需要工具调用）
                    query = userInput[8:].strip()
                    if not query:
                        print("⚠️  请输入搜索需求，例如: /search 轻量级 Python WAF\n")
                        continue
                    userInput = SEARCH_PROMPT_TEMPLATE.format(user_query=query)
                    print(f"🔍 正在搜索: {query} [☁️ 云端模式]\n")
                    # 搜索需要 MCP 工具调用，必须走云端
                    try:
                        print(f"\n🤖 Agent: ", end="", flush=True)
                        async for chunk in agent.chat(userInput, tier="PREMIUM"):
                            print(chunk, end="", flush=True)
                        print("\n")
                    except Exception as e:
                        logger.error("Agent 处理异常: %s", e)
                        print(f"\n❌ 错误: {e}\n")
                    continue
                elif userInput.startswith("/analyze "):
                    # 快捷分析：将 URL 包装为分析指令，强制云端（需要工具调用）
                    repoUrl = userInput[9:].strip()
                    if not repoUrl:
                        print("⚠️  请输入仓库地址，例如: /analyze https://github.com/owner/repo\n")
                        continue
                    userInput = ANALYZE_PROMPT_TEMPLATE.format(repo_url=repoUrl)
                    print(f"📊 正在分析: {repoUrl} [☁️ 云端模式]\n")
                    # 分析需要 MCP 工具调用，必须走云端
                    try:
                        print(f"\n🤖 Agent: ", end="", flush=True)
                        async for chunk in agent.chat(userInput, tier="PREMIUM"):
                            print(chunk, end="", flush=True)
                        print("\n")
                    except Exception as e:
                        logger.error("Agent 处理异常: %s", e)
                        print(f"\n❌ 错误: {e}\n")
                    continue
                elif userInput.startswith("/review "):
                    # 专家团评审：自适应加载 -> 混合算力评审
                    repoUrl = userInput[8:].strip()
                    if not repoUrl:
                        print("⚠️  请输入仓库地址，例如: /review https://github.com/owner/repo\n")
                        continue
                    try:
                        # 触发混合算力专家团评审 (内部包含数据加载与脱水)
                        print(f"\n🤖 专家团综合评审报告：\n", end="", flush=True)
                        async for chunk in agent.multiAgentReview(repoUrl):
                            print(chunk, end="", flush=True)
                        print("\n")
                    except Exception as e:
                        logger.error("评审流程异常: %s", e)
                        print(f"\n❌ 错误: {e}\n")
                    continue
                elif userInput.startswith("/deploy "):
                    # 一键部署：自适应加载 -> 自动 Docker 配置 -> 沙盒运行
                    repoUrl = userInput[8:].strip()
                    if not repoUrl:
                        print("⚠️  请输入仓库地址，例如: /deploy https://github.com/owner/repo\n")
                        continue
                    try:
                        print(f"\n🚀 启动 Docker 沙盒部署流程：\n", end="", flush=True)
                        async for chunk in agent.deploy_project(repoUrl):
                            print(chunk, end="", flush=True)
                        print("\n")
                    except Exception as e:
                        logger.error("部署流程异常: %s", e)
                        print(f"\n❌ 错误: {e}\n")
                    continue
                elif userInput == "/bb":
                    # AOS 2.1: 查看黑板报告
                    print("\n📖 [黑板报告] 当前任务事实:")
                    print("─" * 50)
                    print(agent.blackboard.read_all())
                    print("─" * 50)
                    print(agent.blackboard.get_timeline())
                    print()
                    continue
                elif userInput == "/exp":
                    # AOS 2.4: 查看经验库
                    exps = agent.exp_engine.list_experiences()
                    print("\n🧠 [经验引擎] 当前已学习的执行模式:")
                    if not exps:
                        print("  (暂无成功经验，请先运行 /auto 任务)")
                    for e in exps:
                        print(f"  ⭐ {e['status']} | 置信度: {e['rate']} | 复用: {e['matches']}次 | 任务: {e['id']}")
                    print()
                    continue
                elif userInput.startswith("/auto "):
                    # AOS 2.0: 全自治模式
                    demand = userInput[6:].strip()
                    if not demand:
                        print("⚠️  请输入任务需求，例如: /auto 找到最火的 3 个 Python 量化框架\n")
                        continue
                    try:
                        async for chunk in agent.autonomous_execute(demand):
                            print(chunk, end="", flush=True)
                        print("\n")
                    except Exception as e:
                        logger.error("自治模式异常: %s", e)
                        print(f"\n❌ 自治任务执行失败: {e}\n")
                    continue
                elif userInput == "/skills":
                    # AOS 2.0: 查看技能状态
                    skills = agent.skill_manager.list_available()
                    print("\n📦 动态技能注册表:")
                    for s in skills:
                        icon = "🟢" if s["loaded"] else ("🟡" if s["always_loaded"] else "⚪")
                        print(f"  {icon} {s['name']}: {s['description']}")
                    print(f"  ───")
                    print(f"  🟢 已加载  🟡 自动加载  ⚪ 按需加载\n")
                    continue
                elif userInput == "/schedule":
                    # AOS Phase 3: 查看定时任务
                    tasks = agent.scheduler.list_tasks()
                    if not tasks:
                        print("\n📅 暂无定时任务。可以自然语言对话创建，例如: '每天早上8点提醒我吃药'\n")
                    else:
                        print(f"\n📅 定时任务 ({len(tasks)} 个):")
                        for t in tasks:
                            print(f"  ⏰ {t['task_id']}: {t['description']} | {t['cron']} | 下次: {t['next_trigger']} | 已执行: {t['run_count']}次")
                        print()
                    continue
                elif userInput == "/wallet":
                    # AOS AEA: CFO 财务简报
                    print(agent.economy.get_financial_report())
                    # 最近交易
                    txs = agent.economy.get_recent_transactions(5)
                    if txs:
                        print("📜 最近交易:")
                        for tx in txs:
                            print(f"  {tx['time']} | {tx['type']:>7} | {tx['amount']:>10} | {tx['description']}")
                    print()
                    continue
                elif userInput.startswith("/inject "):
                    # AOS AEA: 注资
                    try:
                        amount = float(userInput[8:].strip())
                        agent.economy.inject_funds(amount)
                        # 同步到黑板
                        for key, val in agent.economy.get_blackboard_facts().items():
                            agent.blackboard.write(key, val, author="CFO")
                        print(agent.economy.get_financial_report() + "\n")
                    except ValueError:
                        print("⚠️  请输入有效金额，例如: /inject 5.00\n")
                    continue
                elif userInput == "/prune":
                    print("🧹 正在清理 Docker 资源...")
                    try:
                        result = agent.docker_sandbox.system_prune()
                        print(f"{result}\n")
                    except Exception as e:
                        print(f"❌ 清理异常: {e}\n")
                    continue

                # Agent 日常对话：根据 AGENT_MODE 决定路由策略
                # TURBO 模式下日常对话也走云端（支持工具调用）
                chat_tier = "PREMIUM" if AGENT_MODE == "TURBO" else "LOCAL"
                try:
                    print(f"\n🤖 Agent: ", end="", flush=True)
                    async for chunk in agent.chat(userInput, tier=chat_tier):
                        print(chunk, end="", flush=True)
                    print("\n")
                except Exception as e:
                    logger.error("Agent 处理异常: %s", e)
                    print(f"\n❌ 错误: {e}\n")

    # 退出前保存所有记忆
    await agent.saveAllMemories()

    print("👋 再见！")


if __name__ == "__main__":
    asyncio.run(main())
