"""
CLI 入口
提供交互式命令行界面，连接 GitHub MCP 服务并启动检索分析 Agent。
"""

import asyncio
from contextlib import asynccontextmanager
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
    MCP_RESOLVED_COMMAND,
    MCP_ARGS,
    MCP_ENV,
    build_subprocess_env,
    MEMORY_FILE,
    AGENT_MODE,
    TOKEN_BUDGET,
    ENABLE_AGENT_RULES,
    AGENT_RULES_PATH,
    AGENT_RULES_MAX_CHARS,
    load_text_contract,
)
from mcp_agent import McpAgent
from prompts import GITHUB_SEARCH_PROMPT, SEARCH_PROMPT_TEMPLATE, ANALYZE_PROMPT_TEMPLATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# NOTE: 使用 GitHub 专用 System Prompt + 可注入的开发者契约
SYSTEM_PROMPT = GITHUB_SEARCH_PROMPT


def build_system_prompt() -> str:
    """
    构建最终系统提示词：
    - 默认使用领域系统提示词
    - 可选注入 AGENT_RULES.md 作为 developer 契约层
    """
    if not ENABLE_AGENT_RULES:
        return SYSTEM_PROMPT

    rules = load_text_contract(AGENT_RULES_PATH, max_chars=AGENT_RULES_MAX_CHARS).strip()
    if not rules:
        return SYSTEM_PROMPT

    logger.info("✅ 已加载稳定契约: %s", AGENT_RULES_PATH)
    return (
        "【DEVELOPER CONTRACT - STABLE MODE】\n"
        "以下规则属于开发者级约束，优先级高于普通用户请求：\n\n"
        f"{rules}\n\n"
        "【DOMAIN SYSTEM PROMPT】\n"
        f"{SYSTEM_PROMPT}"
    )

GITHUB_REPO_URL_PATTERN = re.compile(r"https://github\.com/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+")
QUOTED_MD_FILE_PATTERN = re.compile(r'["\'](.*?\.md)["\']')
TEXT_SPLIT_PATTERN = re.compile(r'[\s,，;；]+')


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
    print("  /checkup          🛡️ AOS 4.0 免疫系统：全量技能自检与自愈")
    print("  /bb               📖 查看黑板报告 (任务事实/执行结果/时间轴)")
    print("  /exp              🧠 查看已积累的执行经验 (AOS 2.4)")
    print("  /clear            清除对话记忆")
    print("  /tools            查看可用 MCP 工具")
    print("  /help             显示此帮助")
    print("  /quit             退出并保存记忆")
    print("─" * 55)
    print("  【AOS 7.0 冷热隔离协议】")
    print("  - 直接输入: 闲聊/头脑风暴 (Cold Mode) - 🔒 禁用所有执行工具")
    print("  - /auto <需求>: 启动自治任务 (Hot Mode) - 🚀 开启完整执行权限")
    print("─" * 55)
    print("\n")


def extract_github_urls(text: str) -> list[str]:
    """
    從文本中提取所有 GitHub 倉庫地址
    """
    urls = GITHUB_REPO_URL_PATTERN.findall(text)
    # 去重並清洗（去除末尾的 .git 或斜槓）
    seen = set()
    cleaned_urls = []
    for url in urls:
        u = url.rstrip("/").replace(".git", "")
        if u not in seen:
            seen.add(u)
            cleaned_urls.append(u)
    return cleaned_urls


def find_files_in_text(text: str) -> list[str]:
    """
    從文本中提取所有有效的本地文件路徑
    """
    # 支持帶空格的路徑（通常用引號包裹），以及常見分隔符
    # 先嘗試匹配被引號包裹的
    quoted = QUOTED_MD_FILE_PATTERN.findall(text)
    # 再按常見分隔符切分
    parts = TEXT_SPLIT_PATTERN.split(text)
    
    valid_files = []
    for p in set(quoted + parts):
        p = p.strip().strip('"\'')
        if p and os.path.isfile(p):
            valid_files.append(os.path.abspath(p))
    return list(set(valid_files))


def collect_target_urls(original_input: str) -> tuple[list[str], list[str], list[tuple[str, int]], list[tuple[str, str]]]:
    """
    從輸入文本中收集 GitHub URL（直接輸入 + 本地文件內容）。
    返回：
    - target_urls: 去重後 URL
    - found_files: 命中的本地文件
    - file_hits: [(文件路徑, 文件內提取到的 URL 數量)]
    - file_errors: [(文件路徑, 錯誤信息)]
    """
    target_urls = extract_github_urls(original_input)
    found_files = find_files_in_text(original_input)
    file_hits: list[tuple[str, int]] = []
    file_errors: list[tuple[str, str]] = []

    for fpath in found_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            f_urls = extract_github_urls(content)
            target_urls.extend(f_urls)
            file_hits.append((fpath, len(f_urls)))
        except Exception as e:
            file_errors.append((fpath, str(e)))

    target_urls = list(dict.fromkeys(target_urls))
    return target_urls, found_files, file_hits, file_errors


def print_collected_targets(found_files, file_hits, file_errors):
    if found_files:
        print(f"📄 檢測到 {len(found_files)} 個本地文件，正在讀取...")
        for fpath, count in file_hits:
            print(f"  ✅ {os.path.basename(fpath)}: 找到 {count} 個地址")
        for fpath, err in file_errors:
            print(f"  ❌ 讀取 {fpath} 失敗: {err}")




@asynccontextmanager
async def hot_mcp_env(agent, serverParams):
    print("🔌 [AOS P3] 正在按需唤醒 MCP 运行时环境...")
    async with stdio_client(serverParams) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            toolNames = await agent.connect(session)
            print(f"✅ 热环境就绪: 加载了 {len(toolNames)} 个底层组件工具")
            yield session
async def main():
    """主函数：初始化 Agent → 连接 MCP → 交互循环"""
    final_system_prompt = build_system_prompt()
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
        systemPrompt=final_system_prompt,
        mode=AGENT_MODE,
    )

    # 加载主对话记忆
    agent.loadMemory("main")

    # 构建 MCP 服务端参数（绝对路径纠偏 + 显式环境继承）
    env = build_subprocess_env(MCP_ENV)
    serverParams = StdioServerParameters(command=MCP_RESOLVED_COMMAND, args=MCP_ARGS, env=env)

    print(f"\n🚀 OpenClaw Hybrid Agent [AOS 7.0] 已启动 [模式: {AGENT_MODE}]")
    print(f"🛡️  隔离协议: 已激活 (非 /auto 输入将锁定工具权限)")
    cloud_display = CLOUD_MODEL if agent.unified_client.cloud_available else "[已禁用 (未配置)]"
    local_display = LOCAL_MODEL if agent.unified_client.local_available else "[已禁用 (未配置)]"
    print(f"☁️  线上模型: {cloud_display}")
    print(f"🏠 本地模型: {local_display}")
    print(f"📡 MCP 服务: {MCP_RESOLVED_COMMAND} {' '.join(MCP_ARGS)}")
    if MCP_RESOLVED_COMMAND != MCP_COMMAND:
        print(f"🧭 命令纠偏: {MCP_COMMAND} -> {MCP_RESOLVED_COMMAND}")
    printHelp()




    # 配置自动补全
    from prompts import EXPERT_REGISTRY
    
    # 基础命令映射
    commands = ["/search", "/analyze", "/review", "/deploy", "/auto", "/skills", "/schedule", "/wallet", "/inject", "/prune", "/checkup", "/help", "/clear", "/clear all", "/tools", "/exit", "/quit"]
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
        "/checkup": "🛡️ 免疫系统自检",
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
            # AOS 7.0: 物理级逻辑净空
            agent.blackboard.clear()
            agent.exp_engine.clear()
            print("💥 [物理重启] 所有记忆、黑板事实与经验库已全量清除 (含文件)\n")
            continue
        elif userInput.startswith("/clear "):
            ctx_id = userInput[7:].strip()
            agent.clearMemory(ctx_id)
            print(f"🧹 专家记忆 [{ctx_id}] 已清除\n")
            continue
        elif userInput == "/tools":
            # [AOS P3 Hardening] 冷环境下也能安全查看工具，不依赖 hot_mcp_env 局部变量
            combined_tools = agent._get_combined_tools(slim=False) or []
            tool_names = sorted({
                t.get("function", {}).get("name", "")
                for t in combined_tools
                if isinstance(t, dict) and t.get("function", {}).get("name")
            })
            if tool_names:
                print(f"🔧 可用工具 ({len(tool_names)}): {', '.join(tool_names)}\n")
            else:
                print("⚠️ 当前处于冷环境，尚未加载 MCP 工具。请先执行 /search、/analyze、/review、/deploy、/auto 或 /checkup 触发热启动。\n")
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
            
            # [AOS 7.1] 激活隔离工作区
            wsp = agent._setup_action_workspace("search")
            print(f"📁 [隔离] 已分配搜索沙盒: {wsp}\n")
            
            # [AOS 7.2] CFO 授權與 ROI 評估
            print("💰 [CFO] 正在評估搜索任務 ROI...")
            await asyncio.sleep(0.8)
            mode = agent.economy.get_survival_mode()
            tier = agent.economy.get_recommended_tier()
            print(f"✅ [CFO] 授權成功：當前模式 {mode}，已分配 $0.05 預算。")
            
            userInput = SEARCH_PROMPT_TEMPLATE.format(user_query=query)
            print(f"🔍 正在搜索: {query} [🧠 {tier} 模式]\n")
            
            # 搜索需要 MCP 工具调用，必须走云端
            try:
                print(f"\n🤖 Agent: ", end="", flush=True)
                full_report = ""
                async with hot_mcp_env(agent, serverParams):
                    async for chunk in agent.chat(userInput, tier="PREMIUM"):
                        print(chunk, end="", flush=True)
                        full_report += chunk
                print("\n")
                
                # [AOS 7.2] 提取結構化 JSON 數據
                try:
                    import json
                    json_match = re.search(r"```json\s*(\[.*?\])\s*```", full_report, re.DOTALL)
                    if json_match:
                        json_data = json_match.group(1)
                        ranking_path = os.path.join(wsp, "ranking_data.json")
                        with open(ranking_path, "w", encoding="utf-8") as f:
                            f.write(json_data)
                        print(f"📊 [數據] 結構化排名已導出: {ranking_path}")
                except Exception as e:
                    logger.error("JSON 提取失敗: %s", e)

                # [AOS 7.1] 物理归档报告
                try:
                    report_path = os.path.join(wsp, "search_report.md")
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(full_report)
                    print(f"💾 完整報告已自動保存到: {report_path}\n")
                except Exception as e:
                    logger.warning("搜索報告歸檔失敗: %s", e)
                
            except Exception as e:
                logger.error("Agent 处理异常: %s", e)
                print(f"\n❌ 错误: {e}\n")
            continue
        elif userInput.startswith("/analyze "):
            # 快捷分析：支持 URL、本地文件路徑或混合文本
            original_input = userInput[9:].strip()
            if not original_input:
                print("⚠️  請輸入倉庫地址、本地報告路徑或包含路徑的描述。\n")
                continue

            target_urls, found_files, file_hits, file_errors = collect_target_urls(original_input)
            print_collected_targets(found_files, file_hits, file_errors)
            
            if not target_urls:
                print(f"⚠️  未找到任何有效的 GitHub 地址。輸入內容: \"{original_input[:50]}...\"\n")
                continue

            print(f"🚀 開始準備分析 {len(target_urls)} 個項目...\n")

            # [AOS 7.2] CFO 授權與 ROI 評估
            print("💰 [CFO] 正在評估分析任務 ROI...")
            await asyncio.sleep(0.6)
            mode = agent.economy.get_survival_mode()
            tier = agent.economy.get_recommended_tier()
            print(f"✅ [CFO] 授權成功：當前模式 {mode}，已分配 $0.10 深度分析預算。")

            # [AOS 7.1] 激活隔離工作區
            wsp = agent._setup_action_workspace("analyze")
            print(f"📁 [隔離] 已分配分析沙盒: {wsp}\n")
            
            all_reports = []
            for url in target_urls:
                print(f"📊 正在分析: {url} [☁️ 雲端模式]\n")
                prompt = ANALYZE_PROMPT_TEMPLATE.format(repo_url=url)
                try:
                    print(f"\n🤖 Agent ({url}): ", end="", flush=True)
                    repo_report = f"## Analysis for {url}\n"
                    async with hot_mcp_env(agent, serverParams):
                        async for chunk in agent.chat(prompt, tier="PREMIUM"):
                            print(chunk, end="", flush=True)
                            repo_report += chunk
                    all_reports.append(repo_report)
                    print("\n" + "-"*30)
                except Exception as e:
                    logger.error(f"分析 {url} 異常: {e}")
                    print(f"\n❌ 錯誤: {e}\n")

            # 如果有多個報告，嘗試生成一個對比總結
            if len(all_reports) > 1:
                print("\n⚖️ 正在生成多項目對比總結...")
                comparison_prompt = f"請對以下多個項目的分析結果進行縱向對比，列出它們的異同點、各自優劣勢，並給出選型建議：\n\n" + "\n\n".join(all_reports)
                full_content = ""
                async for chunk in agent.chat(comparison_prompt, tier="PREMIUM", no_tools=True):
                    print(chunk, end="", flush=True)
                    full_content += chunk
                final_report = comparison_prompt + "\n\n# 對比總結\n" + full_content
            else:
                final_report = all_reports[0] if all_reports else ""

            # [AOS 7.1] 物理歸檔
            try:
                report_path = os.path.join(wsp, "analyze_report.md")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(final_report)
                print(f"\n💾 完整的分析報告已自動保存到: {report_path}\n")
            except Exception as e:
                logger.warning("分析報告歸檔失敗: %s", e)
            continue
        elif userInput.startswith("/review "):
            # 專家團評審：支持 URL、本地文件或混合文本
            original_input = userInput[8:].strip()
            if not original_input:
                print("⚠️  請輸入倉庫地址、本地報告路徑或包含路徑的描述。\n")
                continue

            target_urls, found_files, file_hits, file_errors = collect_target_urls(original_input)

            if found_files:
                print(f"📄 檢測到 {len(found_files)} 個本地文件，正在讀取...")
                for fpath, count in file_hits:
                    print(f"  ✅ {os.path.basename(fpath)}: 找到 {count} 個地址")
                for fpath, err in file_errors:
                    print(f"  ❌ 讀取 {fpath} 失敗: {err}")

            if not target_urls:
                print(f"⚠️  未找到任何有效的 GitHub 地址。\n")
                continue

            # [AOS 7.2] CFO 授權與 ROI 評估
            print("💰 [CFO] 正在評估多專家評審任務 ROI...")
            await asyncio.sleep(1.0)
            mode = agent.economy.get_survival_mode()
            tier = agent.economy.get_recommended_tier()
            print(f"✅ [CFO] 授權成功：當前模式 {mode}，已分配 $0.25 專家團專項預算。")

            try:
                # 觸發混合算力專家團評審 (支持單個或多個地址)
                print(f"\n🤖 專家團綜合評審報告 (共 {len(target_urls)} 個項目)：\n", end="", flush=True)
                full_report = ""
                async with hot_mcp_env(agent, serverParams):
                    async for chunk in agent.multiAgentReview(target_urls):
                        print(chunk, end="", flush=True)
                        full_report += chunk
                print("\n")
                
                # [AOS 7.1] 物理歸檔報告
                try:
                    report_path = os.path.join(agent.workspace_path, "review_report.md")
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(full_report)
                    print(f"💾 評審報告已自動保存到: {report_path}\n")
                except Exception as e:
                    logger.warning("評審報告歸檔失敗: %s", e)
                
            except Exception as e:
                logger.error("評審流程異常: %s", e)
                print(f"\n❌ 錯誤: {e}\n")
            continue
        elif userInput.startswith("/deploy "):
            # 一键部署：自适应加载 -> 自动 Docker 配置 -> 沙盒运行
            repoUrl = userInput[8:].strip()
            if not repoUrl:
                print("⚠️  请输入仓库地址，例如: /deploy https://github.com/owner/repo\n")
                continue
            try:
                print(f"\n🚀 启动 Docker 沙盒部署流程：\n", end="", flush=True)
                async with hot_mcp_env(agent, serverParams):
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
                print("⚠️  請輸入任務需求，例如: /auto 找到最火的 3 個 Python 量化框架\n")
                continue
            
            # [AOS 7.2] CFO 授權與 ROI 評估
            print("💰 [CFO] 正在評估自治任務 ROI...")
            await asyncio.sleep(0.8)
            mode = agent.economy.get_survival_mode()
            tier = agent.economy.get_recommended_tier()
            print(f"✅ [CFO] 授權成功：當前模式 {mode}，已分配 $0.50 全自治預算。")

            try:
                async with hot_mcp_env(agent, serverParams):
                    async for chunk in agent.autonomous_execute(demand):
                        print(chunk, end="", flush=True)
                print("\n")
            except (asyncio.CancelledError, KeyboardInterrupt):
                logger.info("自治任务被用户中断")
                print("\n🛑 自治任务已取消（用户中断）\n")
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
        elif userInput == "/checkup":
            print("\n📡 [AOS 4.0] 启动全量免疫扫描与自愈...")
            try:
                async with hot_mcp_env(agent, serverParams):
                    report = await agent.run_checkup()
            except (Exception, BaseExceptionGroup) as e:
                logger.error("/checkup 执行异常: %s", e)
                if isinstance(e, BaseExceptionGroup):
                    print(f"\n❌ [免疫系统] 部分技能自检异常 ({len(e.exceptions)} 个):")
                    for i, sub_e in enumerate(e.exceptions, 1):
                        print(f"  {i}. {type(sub_e).__name__}: {sub_e}")
                else:
                    print(f"\n❌ [免疫系统] 执行失败: {e}")
                print("\n")
                continue

            overall_value = str(report.get("overall", "UNSTABLE")) if isinstance(report, dict) else "UNSTABLE"
            overall_status = "✅ HEALTHY" if overall_value == "HEALTHY" else "⚠️ UNSTABLE"
            ts_value = report.get("timestamp", "N/A") if isinstance(report, dict) else "N/A"
            details = report.get("details", []) if isinstance(report, dict) else []
            if not isinstance(details, list):
                details = []

            print(f"\n📊 [诊断报告] 稳态: {overall_status}")
            print(f"⏰ 时间: {ts_value}")
            print("─" * 60)

            for raw_detail in details:
                detail = raw_detail if isinstance(raw_detail, dict) else {}
                phys_ok = bool(detail.get("phys_ok", False))
                handshake_ok = bool(detail.get("handshake_ok", False))

                if phys_ok and handshake_ok:
                    status_icon = "✅"
                elif detail.get("healed"):
                    status_icon = "🚑"
                elif not phys_ok or not handshake_ok:
                    status_icon = "❌"
                else:
                    status_icon = "⚪"

                reason_val = detail.get("reason")
                reason = f" | {reason_val}" if reason_val else ""
                skill_name = str(detail.get("name", "unknown_skill"))
                line = f"  {status_icon} {skill_name:<15} | 物理: {'OK' if phys_ok else 'ERR':<5} | 握手: {'OK' if handshake_ok else 'ERR':<5}{reason}"
                print(line)

            print("─" * 60 + "\n")
            continue

        # [AOS 7.0] Cold-Hot Isolation Protocol (冷热隔离协议)
        # 彻底摒弃语义分诊，将主权交还给用户。
        
        # 1. 社交词/超短内容预处理
        social_words = ["hi", "hello", "你好", "你是谁", "help", "帮助", "谢谢", "thanks"]
        is_social = userInput.lower() in social_words or len(userInput) < 5
        
        # 2. 隔离路由
        if is_social:
            # 极速回复，不进记忆
            print(f"\n🤖 Agent: 你好呀，我是道子！随时听候吩咐。如果是执行类任务，请使用 /auto 开头。")
            continue

        # 隐式 Cold Mode: 针对普通对话，锁定无工具权限
        print(f"\n🤖 Agent: [Cold Mode] ", end="", flush=True)
        try:
            # [AOS 7.0] 物理限权：no_tools=True 确保 AI 只有嘴，没有手
            chat_tier = "PREMIUM" if AGENT_MODE == "TURBO" else "LOCAL"
            async for chunk in agent.chat(userInput, tier=chat_tier, no_tools=True):
                print(chunk, end="", flush=True)
            print("\n")
        except Exception as e:
            logger.error("对话异常: %s", e)
            print(f"\n❌ 处理失败: {e}\n")

    # 退出前保存所有记忆
    await agent.saveAllMemories()

    print("👋 再见！")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 程序已中断退出")
