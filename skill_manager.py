"""
AOS 2.0 动态技能管理器 (Skill Manager)
按需加载/卸载 MCP 服务进程，替代硬编码 .env 配置。
支持防僵尸进程保护和技能热插拔。
"""

import asyncio
import logging
import os
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tool_converter import convertMcpToolsToOpenai

logger = logging.getLogger(__name__)

# 技能注册表文件路径
REGISTRY_PATH = os.path.join(os.path.dirname(__file__), ".agents", "skills_registry.yaml")


class LoadedSkill:
    """已加载的技能实例，封装 MCP 进程和会话"""

    def __init__(self, name: str, session: ClientSession, tools: list, runner_task: asyncio.Task, stop_event: asyncio.Event, last_args: list = None):
        self.name = name
        self.session = session
        self.tools = tools
        self.runner_task = runner_task
        self.stop_event = stop_event
        self.last_args = last_args or []


class SkillManager:
    """
    动态技能管理器。
    从 skills_registry.yaml 读取技能白名单，按需启动/关闭 MCP 服务进程。
    """

    def __init__(self):
        self.registry: list[dict] = []
        self.loaded_skills: dict[str, LoadedSkill] = {}
        self._load_registry()

    def _load_registry(self) -> None:
        """从 YAML 文件加载技能注册表"""
        if not os.path.exists(REGISTRY_PATH):
            logger.warning("⚠️ 技能注册表未找到: %s", REGISTRY_PATH)
            return
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            self.registry = data.get("skills", [])
            logger.info("📦 [技能管理器] 已加载 %d 个技能定义", len(self.registry))
        except Exception as e:
            logger.error("技能注册表加载失败: %s", e)

    def list_available(self) -> list[dict]:
        """
        列出所有可用技能（含加载状态）。
        返回格式：[{"name": "...", "description": "...", "loaded": bool}]
        """
        result = []
        for skill in self.registry:
            result.append({
                "name": skill["name"],
                "description": skill.get("description", ""),
                "always_loaded": skill.get("always_loaded", False),
                "loaded": skill["name"] in self.loaded_skills,
            })
        return result

    def get_skill_config(self, name: str) -> dict | None:
        """根据名称查找技能配置"""
        for skill in self.registry:
            if skill["name"] == name:
                return skill
        return None

    async def _skill_runner(self, name: str, server_params: StdioServerParameters, session_future: asyncio.Future, stop_event: asyncio.Event):
        """
        [AOS 2.7] 核心改进：隔离的技能运行器。
        确保 stdio_client 的 __aenter__ 和 __aexit__ 都在同一个 Task 中执行，彻底解决 anyio task-mismatch 报错。
        """
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    # 1. 初始化
                    await asyncio.wait_for(session.initialize(), timeout=60.0)
                    
                    # 2. 获取工具列表并传回主逻辑
                    mcp_tools = await session.list_tools()
                    session_future.set_result((session, mcp_tools.tools))
                    
                    # 3. 阻塞，直到收到卸载信号
                    await stop_event.wait()
                    
                    # 4. 退出上下文（会自动触发 session 和 stdio_client 的 __aexit__）
                    logger.debug("🔌 [技能运行器] 收到停止信号，正在释放技能: %s", name)
        except asyncio.CancelledError:
            logger.debug("🔌 [技能运行器] 任务被显式取消: %s", name)
        except Exception as e:
            if not session_future.done():
                session_future.set_exception(e)
            else:
                logger.error("🚫 [技能运行器] 运行时异常 (%s): %s", name, e)
        finally:
            logger.debug("🔌 [技能运行器] 任务已终结: %s", name)

    async def load_skill(self, name: str, workspace_path: str | None = None) -> dict:
        """
        动态加载指定技能的 MCP 服务。
        [AOS 2.7] 支持 workspace_path 硬约束：如果是 filesystem 技能，强制锁死其目录。
        """
        if name in self.loaded_skills:
            # AOS 2.8.4: 路径校验逻辑。如果当前已加载技能的路径与请求的 workspace_path 不符，强制重载。
            # 这是为了处理 always_loaded 技能（如 filesystem）在任务切换时需要重新锚定沙箱的问题。
            current_args = getattr(self.loaded_skills[name], "last_args", [])
            if name == "filesystem" and workspace_path:
                target_abs = os.path.abspath(workspace_path)
                if not current_args or current_args[0] != target_abs:
                    logger.info("🔄 [AOS 2.8.4] 检测到工作区变更，正在为技能 '%s' 重新锚定物理路径...", name)
                    await self.unload_skill(name)
                else:
                    return {"status": "already_loaded", "tools": len(self.loaded_skills[name].tools)}
            else:
                return {"status": "already_loaded", "tools": len(self.loaded_skills[name].tools)}

        config = self.get_skill_config(name)
        if not config:
            return {"status": "error", "message": f"技能 '{name}' 未在注册表中找到"}

        print(f"🔌 [技能管理器] 正在加载技能: {name}...")

        try:
            # 环境与路径处理
            env_config = {}
            for key, val in config.get("env", {}).items():
                if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                    env_var = val[2:-1]
                    env_config[key] = os.getenv(env_var, "")
                else:
                    env_config[key] = val

            args = config.get("args", [])
            
            # [AOS 2.7+] 硬约束隔离：强制重定向 filesystem 技能的根目录
            if name == "filesystem" and workspace_path:
                # filesystem server 通常接受路径作为参数
                # 我们将配置中的 "." 或缺省路径替换为物理隔离的工作区
                args = [os.path.abspath(workspace_path)]
                logger.info("📁 [隔离] 已将 filesystem 技能物理锁死在工作区: %s", workspace_path)
            elif name == "filesystem":
                # 回退：如果没有提供 workspace_path，默认使用当前目录（绝对路径锚定）
                new_args = []
                for arg in args:
                    if arg == ".":
                        new_args.append(os.path.abspath("."))
                    else:
                        new_args.append(arg)
                args = new_args

            server_params = StdioServerParameters(
                command=config["command"],
                args=args,
                env=env_config if env_config else None,
            )

            # 启动隔离的 Runner Task
            session_future = asyncio.get_event_loop().create_future()
            stop_event = asyncio.Event()
            runner_task = asyncio.create_task(
                self._skill_runner(name, server_params, session_future, stop_event),
                name=f"SkillRunner-{name}"
            )

            # 等待初始化完成并获取成果
            try:
                session, raw_tools = await session_future
            except Exception as e:
                runner_task.cancel()
                raise e

            openai_tools = convertMcpToolsToOpenai(raw_tools)
            tool_names = [t.name for t in raw_tools]

            # 注册到内存
            self.loaded_skills[name] = LoadedSkill(
                name=name,
                session=session,
                tools=openai_tools,
                runner_task=runner_task,
                stop_event=stop_event,
                last_args=args # 保存本次启动的参数，供下次校验使用
            )

            print(f"✅ [技能管理器] 技能 '{name}' 已加载，提供 {len(tool_names)} 个工具: {tool_names}")
            return {"status": "loaded", "tools": tool_names}

        except Exception as e:
            logger.error("技能 '%s' 加载过程中出现异常: %s", name, e)
            return {"status": "error", "message": str(e)}

    async def unload_skill(self, name: str) -> dict:
        """
        安全卸载指定技能。
        [AOS 2.7] 通过信号通知隔离任务优雅结束，确保 anyio 作用域正确闭合。
        """
        if name not in self.loaded_skills:
            return {"status": "not_loaded", "message": f"技能 '{name}' 未加载"}

        skill = self.loaded_skills[name]
        try:
            # 发送停止信号并等待任务结束
            skill.stop_event.set()
            # 给予一定缓冲时间
            await asyncio.wait_for(skill.runner_task, timeout=10.0)
            print(f"🔌 [技能管理器] 技能 '{name}' 已安全卸载")
        except asyncio.TimeoutError:
            logger.warning("技能 '%s' 卸载超时，强制取消任务", name)
            skill.runner_task.cancel()
        except Exception as e:
            logger.warning("技能 '%s' 卸载时出现异常: %s", name, e)
        finally:
            if name in self.loaded_skills:
                del self.loaded_skills[name]

        return {"status": "unloaded"}

    async def unload_all(self) -> None:
        """安全卸载所有已加载的技能（程序退出时调用）"""
        names = list(self.loaded_skills.keys())
        for name in names:
            await self.unload_skill(name)

    def get_all_tools(self) -> list[dict]:
        """获取所有已加载技能的工具集合"""
        all_tools = []
        for skill in self.loaded_skills.values():
            all_tools.extend(skill.tools)
        return all_tools

    def get_tool_names(self) -> list[str]:
        """获取所有已加载技能的工具名称"""
        names = []
        for skill in self.loaded_skills.values():
            for tool in skill.tools:
                names.append(tool["function"]["name"])
        return names

    async def call_tool(self, func_name: str, arguments: dict) -> str | None:
        """
        在已加载的技能中查找并调用指定工具。
        返回 None 表示该工具不属于任何已加载技能。
        """
        for skill in self.loaded_skills.values():
            tool_names = [t["function"]["name"] for t in skill.tools]
            if func_name in tool_names:
                result = await skill.session.call_tool(func_name, arguments=arguments)
                # 提取文本内容
                texts = []
                for item in result.content:
                    if hasattr(item, "text"):
                        texts.append(item.text)
                    elif isinstance(item, dict) and "text" in item:
                        texts.append(item["text"])
                    else:
                        texts.append(str(item))
                return "\n".join(texts)
        return None

    def register_new_skill(self, skill_config: dict) -> None:
        """
        动态追加新技能到注册表。
        同时写入 YAML 文件持久化。
        """
        self.registry.append(skill_config)
        try:
            with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                yaml.dump({"skills": self.registry}, f, allow_unicode=True, default_flow_style=False)
            logger.info("📦 [技能管理器] 新技能 '%s' 已注册到注册表", skill_config.get("name"))
        except Exception as e:
            logger.error("注册表写入失败: %s", e)

    # ========== Phase 3: 技能自动发现与评分 ==========

    async def discover_skill(self, query: str, session=None) -> list[dict]:
        """
        从 GitHub 搜索 MCP Server 仓库。
        通过主 MCP Session 调用 GitHub Search API。
        返回候选技能列表 [{name, repo, stars, description, updated}]
        """
        if not session:
            logger.warning("发现技能需要 MCP session (GitHub)")
            return []

        print(f"🔍 [技能发现] 在 GitHub 搜索 MCP 技能: {query}...")

        try:
            # 通过已连接的 GitHub MCP 搜索仓库
            result = await session.call_tool(
                "search_repositories",
                arguments={"query": f"{query} mcp server", "page": 1, "perPage": 5},
            )
            texts = []
            for item in result.content:
                if hasattr(item, "text"):
                    texts.append(item.text)
            raw = "\n".join(texts)

            # 简单解析搜索结果（GitHub MCP 返回的是文本格式）
            candidates = []
            import re
            # 尝试从返回文本中提取仓库信息
            repo_pattern = re.compile(r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)")
            repos = list(set(repo_pattern.findall(raw)))[:5]

            for repo_name in repos:
                candidates.append({
                    "name": repo_name.split("/")[-1],
                    "repo": repo_name,
                    "description": "",
                    "raw_info": raw[:500],
                })

            print(f"📦 [技能发现] 找到 {len(candidates)} 个候选技能")
            return candidates

        except Exception as e:
            logger.error("技能发现失败: %s", e)
            return []

    def score_candidates(self, candidates: list[dict]) -> list[dict]:
        """
        对候选技能进行静态评分（Phase 3 竞标评分）。
        评分维度: Stars 数量、更新时间、README 完整度。
        返回排序后的候选列表。
        """
        for c in candidates:
            score = 0
            # Stars 评分 (最高 40 分)
            stars = c.get("stars", 0)
            score += min(stars, 1000) * 0.04

            # 活跃度评分 (最高 30 分) - 基于最近更新时间
            updated = c.get("updated", "")
            if updated:
                try:
                    from datetime import datetime
                    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    days_ago = (datetime.now(updated_dt.tzinfo) - updated_dt).days
                    if days_ago < 30:
                        score += 30
                    elif days_ago < 90:
                        score += 20
                    elif days_ago < 365:
                        score += 10
                except Exception:
                    pass

            # README 评分 (最高 30 分) - 描述长度
            desc = c.get("description", "")
            score += min(len(desc), 150) * 0.2

            c["score"] = round(score, 1)

        # 按分数排序
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

        print("📊 [技能竞标] 评分结果:")
        for i, c in enumerate(candidates, 1):
            print(f"  {i}. {c.get('name', '?')} — 得分: {c.get('score', 0)}")

        return candidates

    async def auto_install(self, query: str, session=None) -> dict:
        """
        全自动技能安装流程：
        1. 搜索 GitHub 候选
        2. 静态评分排名
        3. 冠军技能注册到 registry
        """
        # 发现候选
        candidates = await self.discover_skill(query, session)
        if not candidates:
            return {"status": "no_candidates", "message": f"未找到与 '{query}' 相关的 MCP 技能"}

        # 评分排名
        ranked = self.score_candidates(candidates)
        winner = ranked[0]

        # 注册到 registry（npm 包名推断）
        skill_config = {
            "name": winner["name"],
            "description": winner.get("description", f"自动发现的技能: {query}"),
            "command": "npx",
            "args": ["-y", f"@modelcontextprotocol/server-{winner['name']}"],
            "always_loaded": False,
        }

        self.register_new_skill(skill_config)
        print(f"🏆 [技能安装] 冠军技能 '{winner['name']}' 已注册到注册表")

        return {
            "status": "installed",
            "skill_name": winner["name"],
            "repo": winner.get("repo", ""),
            "score": winner.get("score", 0),
        }
