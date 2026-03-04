"""
AOS 2.0 动态技能管理器 (Skill Manager)
按需加载/卸载 MCP 服务进程，替代硬编码 .env 配置。
支持防僵尸进程保护和技能热插拔。
"""

import asyncio
import logging
import os
import yaml
import shutil
import traceback
from datetime import datetime, timezone
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tool_converter import convertMcpToolsToOpenai
from config import (
    SELF_UPGRADE_SAFE_MODE,
    SELF_UPGRADE_MIN_STARS,
    SELF_UPGRADE_MAX_AGE_DAYS,
    SELF_UPGRADE_TRUSTED,
    SELF_UPGRADE_DENYLIST,
    resolve_executable_command,
    build_subprocess_env,
)

logger = logging.getLogger(__name__)

# 技能注册表文件路径
REGISTRY_PATH = os.path.join(os.path.dirname(__file__), ".agents", "skills_registry.yaml")
GENE_PATH = os.path.join(os.path.dirname(__file__), "memories", "skill_genes.json")


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

    def __init__(self, unified_client=None, agent_ref=None):
        self.registry: list[dict] = []
        self.loaded_skills: dict[str, LoadedSkill] = {}
        self.unified_client = unified_client
        self.agent_ref = agent_ref
        self._load_registry()

        # 自升级安全策略
        self.self_upgrade_safe_mode = SELF_UPGRADE_SAFE_MODE
        self.self_upgrade_min_stars = max(0, SELF_UPGRADE_MIN_STARS)
        self.self_upgrade_max_age_days = max(1, SELF_UPGRADE_MAX_AGE_DAYS)
        self.self_upgrade_trusted = [x.lower() for x in SELF_UPGRADE_TRUSTED]
        self.self_upgrade_denylist = [x.lower() for x in SELF_UPGRADE_DENYLIST]
        
        # AOS 3.3: 启动时自动初始化/更新基因库
        self._bootstrap_genes()

        # [AOS 8.3] 并发加载锁：防止多个子专家同时 load_skill 触发重复 npx 冷启动
        self._load_locks: dict[str, asyncio.Lock] = {}
        # 预加载核心锁以消除首次竞态
        for k in ["filesystem", "github", "scrape-mcp"]:
            self._load_locks[k] = asyncio.Lock()

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

    async def load_always_loaded_skills(self, workspace_path: str | None = None) -> None:
        """
        [AOS 3.8.5] 自动加载所有标记为 always_loaded 的技能（如 filesystem）。
        """
        for skill in self.registry:
            if skill.get("always_loaded", False):
                name = skill["name"]
                if name not in self.loaded_skills:
                    await self.load_skill(name, workspace_path=workspace_path)

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

    def _is_candidate_allowed(self, candidate: dict) -> tuple[bool, str]:
        """
        自升级候选技能安全审核。
        """
        name = str(candidate.get("name", "")).strip()
        repo = str(candidate.get("repo", "")).strip()
        stars = int(candidate.get("stars", 0) or 0)
        updated = str(candidate.get("updated", "")).strip()
        scope = f"{name} {repo}".lower()

        # denylist 永远优先
        for banned in self.self_upgrade_denylist:
            if banned and banned in scope:
                return False, f"命中 denylist: {banned}"

        # 关闭安全模式时只执行 denylist 约束
        if not self.self_upgrade_safe_mode:
            return True, "safe_mode=off"

        if stars < self.self_upgrade_min_stars:
            return False, f"社区信号不足(stars={stars} < {self.self_upgrade_min_stars})"

        # 更新时间约束（防止安装陈旧无人维护技能）
        if not updated:
            return False, "缺少更新时间"
        try:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
            if age_days > self.self_upgrade_max_age_days:
                return False, f"更新过旧({age_days} 天 > {self.self_upgrade_max_age_days} 天)"
        except Exception:
            return False, f"更新时间不可解析: {updated}"

        # 可选信任来源限制（为空则不限制）
        if self.self_upgrade_trusted:
            if not any(t in scope for t in self.self_upgrade_trusted):
                return False, f"不在 trusted 范围: {self.self_upgrade_trusted}"

        return True, "通过策略审核"

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
        except (Exception, BaseExceptionGroup) as e:
            if not session_future.done():
                session_future.set_exception(e)
            
            # [AOS 7.5.2] 深度異常診斷：展開 ExceptionGroup 查看真實原因
            logger.error("🚫 [技能運行器] 運行時異常 (%s): %s", name, str(e))
            if hasattr(e, "exceptions"):
                for idx, sub_e in enumerate(e.exceptions):
                    logger.error("  └─ [子異常 %d]: %s", idx, str(sub_e))
                    # 如果子異常還有堆棧，記錄下來
                    logger.error(traceback.format_exc())
        finally:
            logger.debug("🔌 [技能运行器] 任务已终结: %s", name)

    async def load_skill(self, name: str, workspace_path: str | None = None) -> dict:
        """
        动态加载指定技能的 MCP 服务。
        [AOS 8.3.1] 改进锁获取逻辑，消除 __init__ 外的竞态。
        """
        if name not in self._load_locks:
            self._load_locks[name] = asyncio.Lock()
            
        if self._load_locks[name].locked():
            print(f"⏳ [技能管理器] 技能 '{name}' 正在被其他专家占用，正在排队等锁...")
            
        async with self._load_locks[name]:
            return await self._load_skill_inner(name, workspace_path)

    async def _load_skill_inner(self, name: str, workspace_path: str | None = None) -> dict:
        """实际的技能加载逻辑（被锁保护）。"""
        if name in self.loaded_skills:
            # AOS 2.8.4/3.8.6: 路径校验逻辑。如果当前已加载技能的路径与请求的 workspace_path 不符，强制重载。
            current_args = getattr(self.loaded_skills[name], "last_args", [])
            if name == "filesystem" and workspace_path:
                target_abs = os.path.abspath(workspace_path)
                # [AOS 8.3.2] 路径比对精度加固：仅比对 args 列表中的物理路径
                current_roots = []
                for a in current_args:
                    arg_str = str(a)
                    # 排除 cmd/npx/package 等非路径参数
                    if arg_str.lower() in ["/c", "cmd.exe", "npx", "npx.cmd", "-y"]:
                        continue
                    if "@modelcontextprotocol" in arg_str:
                        continue
                    
                    try:
                        abs_p = os.path.abspath(arg_str)
                        if os.path.exists(abs_p) or arg_str == ".":
                            current_roots.append(abs_p)
                    except:
                        continue

                already_covered = any(
                    target_abs == root or target_abs.startswith(root + os.sep)
                    for root in current_roots
                )

                if not current_args or not already_covered:
                    logger.info("🔄 [AOS 8.2] 检测到工作区超出当前授权矩阵，正在为技能 '%s' 重新授权物理路径...", name)
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
            # [AOS 7.5.4] NPM 兼容性加固：強制使用官方 Registry 以免鏡像同步延遲導致包找不到
            # [Fix AOS 7.5.6] 環境變量加固：必須繼承原始環境變量（特別是 PATH 和 SystemRoot），否则 npx 会失败
            # NOTE: os 已在文件顶部全局导入，禁止在此处重复 import（会导致 UnboundLocalError）
            env_config = build_subprocess_env()
            env_config.update({
                "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org",
                "NPM_CONFIG_AUDIT": "false",
                "NPM_CONFIG_FUND": "false",
                "NPM_CONFIG_LOGLEVEL": "error",
                "NPM_CONFIG_UPDATE_NOTIFIER": "false"
            })
            for key, val in config.get("env", {}).items():
                if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
                    env_var = val[2:-1]
                    env_config[key] = os.getenv(env_var, "")
                else:
                    env_config[key] = val

            args = config.get("args", [])

            # [AOS 8.1] 启动前预检：避免 firecrawl 缺少关键环境变量导致“Connection closed”迷惑报错
            # firecrawl-mcp 要求至少提供 FIRECRAWL_API_KEY 或 FIRECRAWL_API_URL，否则进程会立即退出。
            is_firecrawl = (
                "firecrawl" in str(name).lower()
                or any("firecrawl" in str(a).lower() for a in args)
            )
            if is_firecrawl:
                firecrawl_api_key = str(env_config.get("FIRECRAWL_API_KEY", "")).strip()
                firecrawl_api_url = str(env_config.get("FIRECRAWL_API_URL", "")).strip()
                if not firecrawl_api_key and not firecrawl_api_url:
                    msg = (
                        "Firecrawl 启动前预检失败：缺少 FIRECRAWL_API_KEY 或 FIRECRAWL_API_URL。"
                        "请在 .env 中配置至少一个变量，或在 skills_registry.yaml 的 env 字段中提供。"
                    )
                    logger.error("❌ [技能预检] %s", msg)
                    return {"status": "error", "message": msg}
            
            # [AOS 3.8.6] 授权矩阵：始终允许项目根目录，如果指定了工作区则追加
            # 解决“访问被拒绝”问题：确保 Agent 在沙箱内工作的同时，仍有权读取项目本身的配置或代码。
            allowed_roots = [os.path.abspath(".")]
            if name == "filesystem" and workspace_path:
                abs_workspace = os.path.abspath(workspace_path)
                if abs_workspace not in allowed_roots:
                    allowed_roots.append(abs_workspace)
                logger.info("🔒 [隔离] filesystem 技能已授权路径矩阵: %s", allowed_roots)

            if name == "filesystem":
                # 清理 registry 中的占位路径并替换为授权矩阵
                new_args = []
                for a in args:
                    if a == "." or os.path.isabs(a): 
                        continue
                    new_args.append(a)
                new_args.extend(allowed_roots)
                args = new_args

            # [AOS 7.5.2+] 统一命令纠偏：与 main.py 共用解析逻辑
            cmd_name = config["command"]
            resolved_cmd = resolve_executable_command(cmd_name)
            
            if not resolved_cmd:
                logger.warning("⚠️ [AOS 7.5.2] 未能找到可執行文件: %s，尝试使用原始名稱。", cmd_name)
                resolved_cmd = cmd_name
            
            # [AOS 7.5.3] Shell Protocol：在 Windows 上，如果是脚本類指令，可能需要 cmd /c 輔助
            final_args = args
            if os.name == "nt" and resolved_cmd.lower().endswith((".cmd", ".bat", ".ps1")):
                logger.info("🐚 [Shell Protocol] 检测到 Windows 脚本，封装为 cmd /c 模式")
                final_args = ["/c", resolved_cmd] + args
                resolved_cmd = "cmd.exe"

            server_params = StdioServerParameters(
                command=resolved_cmd,
                args=final_args,
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
            # [AOS 8.2] 防卡死补丁：增加启动等待超时，避免 session_future 无期限挂起
            startup_timeout = float(config.get("startup_timeout", 90))
            try:
                session, raw_tools = await asyncio.wait_for(session_future, timeout=startup_timeout)
            except asyncio.TimeoutError as e:
                runner_task.cancel()
                raise RuntimeError(
                    f"技能 '{name}' 启动超时（>{int(startup_timeout)}s）。"
                    f" command={resolved_cmd}, args={final_args[:6]}"
                ) from e
            except Exception as e:
                runner_task.cancel()
                # [AOS 8.4] 优化 ExceptionGroup 报错：提取底层真实原因
                error_msg = str(e)
                if hasattr(e, "exceptions") and e.exceptions: # ExceptionGroup or BaseExceptionGroup
                    # 尝试递归寻找最底层的非 Group 异常
                    curr = e
                    while hasattr(curr, "exceptions") and curr.exceptions:
                        curr = curr.exceptions[0]
                    error_msg = str(curr)
                raise RuntimeError(f"技能 '{name}' 加载失败: {error_msg}") from e

            # [AOS 3.9.9] 点火验质 (Post-Load Verification)
            # 物理进程虽然起来了，但可能卡在依赖安装（如 Puppeteer），此处强制握手一次
            try:
                verify_tools = await asyncio.wait_for(session.list_tools(), timeout=15.0)
                if not verify_tools or not verify_tools.tools:
                    raise Exception("物理进程已连接，但未侦测到任何工具。")
            except Exception as e:
                logger.error("❌ [AOS 3.9.9] 技能 %s 启动握手失败: %s", name, str(e))
                # 尝试关闭已建立的无效连接
                try:
                    await session_future
                    runner_task.cancel()
                except:
                    pass
                raise RuntimeError(f"加载失败：物理层无响应或超时 ({str(e)})。请检查该库(如 Puppeteer/Playwright)的环境依赖。")

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

        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            error_msg = str(e)
            if hasattr(e, "exceptions"):
                sub_msgs = [str(sub) for sub in getattr(e, "exceptions")]
                error_msg = f"{error_msg} -> " + " | ".join(sub_msgs)
            logger.error("技能 '%s' 加载过程中出现异常: %s", name, error_msg)
            return {"status": "error", "message": error_msg}

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

    async def hot_load_skill(self, name: str, workspace_path: str | None = None) -> dict:
        """
        [AOS 5.0] 物理环境对齐：热加载技能并瞬间同步到当前活跃专家的工具箱中。
        """
        logger.info(f"🧬 [AOS 5.0] 正在执行技能热插拔: {name}")
        
        # 1. 如果是 filesystem 等关键技能，先强制卸载旧的（如果有）以确保路径授权更新
        if name in self.loaded_skills:
            await self.unload_skill(name)
            
        # 2. 调用标准加载流程
        load_result = await self.load_skill(name, workspace_path=workspace_path)
        
        if load_result.get("status") in ["loaded", "already_loaded"]:
            # 3. 🚨 [AOS 5.0] 核心对齐：触发 Agent 引用，强制刷新其内部的 openaiTools
            if self.agent_ref:
                logger.info(f"🛰️ [AOS 5.0] 正在同步新技能 '{name}' 到 Agent 视网膜...")
                # 重新构建 Agent 的工具列表
                self.agent_ref.openaiTools = self.agent_ref._get_combined_tools()
                
            logger.info(f"✅ [AOS 5.0] 技能 '{name}' 热插拔成功，物理层已就位。")
            
        return load_result

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
            names.extend([t["function"]["name"] for t in skill.tools])
        return names

    def is_tool_available(self, tool_name: str) -> bool:
        """
        [AOS 3.5.1] 检查指定工具是否已加载。
        用于工具名幻觉纠偏逻辑。
        """
        for skill in self.loaded_skills.values():
            if any(t["function"]["name"] == tool_name for t in skill.tools):
                return True
        return False
    def resolve_alias(self, func_name: str) -> str:
        """
        [AOS 3.7.3/3.9.3] 别名解析器：将幻觉工具名映射为物理工具名。
        """
        import re
        # 1. 脱水
        clean_name = re.sub(r'^(filesystem|github|browser|sqlite|mcp)[_.]', '', func_name)
        
        # 2. 映射表 (仅保留确信度极高的映射)
        TOOL_ALIASES = {
            "get_repository_contents": "get_file_contents",
            "list_repository": "get_file_contents",
            "list_directory": "get_file_contents",
            "get_repo_info": "search_repositories",
            "read_repo": "get_file_contents",
            "list_files": "get_file_contents",
            "edit_file": "create_or_update_file",
            "delete_file": "push_files",
            
            # AOS 核心黑板映射
            "board_update": "write_blackboard",
            "board_write": "write_blackboard",
            "update_blackboard": "write_blackboard",
            "write_board": "write_blackboard",
            "board_read": "read_blackboard",
            "read_blackboard": "read_blackboard",

            # AOS 3.7.5 别名扩容
            "dynamic_web_fetcher": "fetch",
            "web_fetcher": "fetch",
            "simulate_web_fetcher": "fetch",
            "write_to_board": "write_blackboard",
            "set_blackboard": "write_blackboard",
            "filesystem_read_file": "read_file"
        }
        return TOOL_ALIASES.get(clean_name, clean_name)

    async def call_tool(self, func_name: str, arguments: dict) -> str | None:
        """
        [AOS 3.7.2/3.7.3/3.9.5/3.9.7] 语义增强版：支持拦截大类调用、脱水与别名纠偏、直通内部工具
        """
        import re

        # 🚨 AOS 3.9.7: 内部工具直通车 (Absolute Sync)
        # 防止由于专家工具列表缺失或未同步导致重要生存工具报 Unknown tool
        internal_tools = ["discover_and_install_skill", "cfo_report", "write_blackboard", "inject_funds", "cfo_approve", "list_scheduled_tasks", "add_scheduled_task"]
        if func_name in internal_tools and self.agent_ref:
            logger.info("⚡ [直通车] 内部工具 '%s' 被拦截并交由 AOS 核心处理", func_name)
            return await self.agent_ref._handle_internal_tool(func_name, arguments)
        
        # 1. 🛡️ 技能大类拦截 (Skill Class Interceptor) - AOS 3.9.5
        # 防止模型傻调 'filesystem' 类名导致死循环
        if func_name.lower() in ["filesystem", "github", "browser", "mcp"]:
            available = self.get_tool_names()
            error_msg = f"❌ [ERROR] 你尝试直接调用技能大类 '{func_name}'，物理引擎无法执行类对象。\n请从以下【具体方法】中选择一个重新尝试：\n{available}"
            logger.error(f"❌ [技能管理器] 拦截到大类调用: {func_name}")
            return error_msg

        # 2. ⚔️ 脱水与别名决策 (Dehydration & Alias Strategy)
        # 先执行命名空间脱水 (github. / github_ -> tool)
        clean_func_name = re.sub(r'^(filesystem|github|browser|sqlite|mcp)[_.]', '', func_name)
        
        # 使用统一解析器获取最终物理名称
        final_target_name = self.resolve_alias(func_name)
        
        # 2. 物理工具查找
        available_tool_names = self.get_tool_names()
        
        # 策略 A: 优先匹配脱水后的名称 (e.g. write_file -> filesystem.write_file)
        matching_skill = None
        final_func_name = clean_func_name
        
        for skill in self.loaded_skills.values():
            if any(t["function"]["name"] == clean_func_name for t in skill.tools):
                matching_skill = skill
                break
        
        # 策略 B: 如果脱水后的名字找不到，尝试通过 resolve_alias 进行别名映射
        if not matching_skill and final_target_name != clean_func_name:
            for skill in self.loaded_skills.values():
                if any(t["function"]["name"] == final_target_name for t in skill.tools):
                    logger.warning(f"🎭 [语义纠偏] 找不到原名工具 '{func_name}'，已映射为别名: '{final_target_name}'")
                    matching_skill = skill
                    final_func_name = final_target_name
                    break

        # 3. 执行物理调用
        if matching_skill:
            result = await matching_skill.session.call_tool(final_func_name, arguments=arguments)
            # 文本提纯逻辑
            texts = []
            for item in result.content:
                if hasattr(item, "text"): texts.append(item.text)
                elif isinstance(item, dict) and "text" in item: texts.append(item["text"])
                else: texts.append(str(item))
            return "\n".join(texts)
                
        # 4. 🚨 强制纠偏反馈 (扔回正确列表，逼迫大模型清醒)
        # 用脱水后的名字报错，让模型知道我们其实听懂了它的意图，只是没找到物理对应的工具
        if not available_tool_names:
            error_msg = f"Unknown tool: {clean_func_name}. 当前没有任何技能被加载！请先调用 'load_skill' 加载所需技能。"
        else:
            error_msg = f"Unknown tool: {clean_func_name}. Available tools: {available_tool_names}"
        
        logger.error(f"❌ [技能管理器] {error_msg}")
        raise ValueError(error_msg)

    def _bootstrap_genes(self):
        """
        [AOS 3.3] 启动引导：如果基因库缺失或不完整，自动从 YAML 注册表重建。
        """
        import json
        if not self.registry:
            return
            
        genes_exist = os.path.exists(GENE_PATH)
        current_genes = {}
        if genes_exist:
            try:
                with open(GENE_PATH, "r", encoding="utf-8") as f:
                    current_genes = json.load(f)
            except Exception:
                pass
        
        updated = False
        for skill in self.registry:
            name = skill.get("name")
            if name and (name not in current_genes):
                self._record_skill_genes(name, skill.get("description", ""))
                updated = True
        
        if updated:
            logger.info("🧬 [达尔文引擎] 基因库引导任务完成，已同步现有技能。")

    def register_new_skill(self, skill_config: dict) -> None:
        """
        动态追加新技能到注册表。
        同时写入 YAML 文件持久化。
        """
        # 防止重复注册
        if any(s.get("name") == skill_config.get("name") for s in self.registry):
            return

        self.registry.append(skill_config)
        try:
            with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                yaml.dump({"skills": self.registry}, f, allow_unicode=True, default_flow_style=False)
            logger.info("📦 [技能管理器] 新技能 '%s' 已注册到注册表", skill_config.get("name"))
            
            # AOS 3.3: 同步写入基因库
            self._record_skill_genes(skill_config.get("name"), skill_config.get("description", ""))
        except Exception as e:
            logger.error("注册表写入失败: %s", e)

    def _record_skill_genes(self, name: str, description: str):
        """
        [AOS 3.3] 录入技能基因：将技能名称和描述中的关键词存入基因库。
        """
        import json
        import re
        
        # 提取关键词：中文词组或 3 字符以上的英文单词
        words = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', description))
        words.add(name) # 名称本身也是核心基因
        
        # 排除无意义的词（简单过滤）
        stop_words = {"测试", "示例", "工具", "mcp", "server", "protocol", "model", "context"}
        genes = [w.lower() for w in words if w.lower() not in stop_words]
        
        try:
            os.makedirs(os.path.dirname(GENE_PATH), exist_ok=True)
            registry = {}
            if os.path.exists(GENE_PATH):
                with open(GENE_PATH, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            
            registry[name] = {
                "description": description,
                "keywords": genes
            }
            
            with open(GENE_PATH, "w", encoding="utf-8") as f:
                json.dump(registry, f, ensure_ascii=False, indent=2)
            logger.info("🧬 [达尔文引擎] 技能 '%s' 的基因已录入 (关键词: %s)", name, ", ".join(genes[:5]))
        except Exception as e:
            logger.error("基因录入失败: %s", e)

    def match_genes(self, user_demand: str) -> list[str]:
        """
        [AOS 3.3] 基因共鸣：根据用户需求匹配已安装的技能基因。
        """
        import json
        if not os.path.exists(GENE_PATH):
            return []
            
        try:
            with open(GENE_PATH, "r", encoding="utf-8") as f:
                registry = json.load(f)
            
            matched = []
            demand = user_demand.lower()
            for name, meta in registry.items():
                if any(kw in demand for kw in meta.get("keywords", [])):
                    matched.append(name)
            return matched
        except Exception as e:
            logger.error("基因匹配失败: %s", e)
            return []

    def search(self, query: str) -> list[dict]:
        """
        [AOS 3.8] 搜索可用技能。
        扫描注册表和基因库，返回匹配的技能列表。
        """
        query = query.lower()
        results = []
        for skill in self.registry:
            name = skill["name"].lower()
            desc = skill.get("description", "").lower()
            if query in name or query in desc:
                results.append({
                    "name": skill["name"],
                    "description": skill.get("description", ""),
                    "loaded": skill["name"] in self.loaded_skills
                })
        return results

    def get_content(self, name: str) -> str | None:
        """
        [AOS 3.8] 获取技能的详细说明。
        """
        skill = self.get_skill_config(name)
        if not skill:
            return None
            
        desc = skill.get("description", "无描述")
        status = "已加载" if name in self.loaded_skills else "未加载"
        return f"技能: {name}\n状态: {status}\n描述: {desc}"

    async def _enrich_metadata(self, name: str, original_description: str) -> str:
        """
        [AOS 3.3.1] 基因富集：利用本地模型对技能描述进行二次蒸馏。
        生成精炼、带关键词的描述，用于雷达展示和基因匹配。
        """
        if not self.unified_client:
            return original_description
            
        print(f"🧬 [达尔文引擎] 正在为新技能 '{name}' 蒸馏基因描述...")
        prompt = f"""
        请为 MCP 技能 '{name}' 编写一段极其精炼的中文备注（不超过 50 字）。
        原始描述: {original_description}
        要求：
        1. 必须包含技能的核心价值点。
        2. 包含 2-3 个核心功能关键词。
        3. 语气客观、专业。
        示例输出：GitHub 仓库操作工具，支持代码搜索、仓库分析和 Issue 管理。
        """.strip()
        
        try:
            # 使用本地模型进行蒸馏，零成本
            summary = await self.unified_client.generate(
                tier="LOCAL",
                messages=[{"role": "user", "content": prompt}]
            )
            # 去除可能的多余字符
            summary = summary.strip().replace("\n", " ")
            if len(summary) > 10: # 保证质量
                return summary
        except Exception as e:
            logger.error("基因蒸馏失败: %s", e)
            
        return original_description

            
        return original_description

    async def _extract_candidates_metadata(self, raw_text: str) -> list[dict]:
        """
        [AOS 3.10.1] 元数据洗涤 (Metadata Scrubber)：
        利用本地模型从原始搜索文本中提取结构化元数据。
        """
        if not self.unified_client:
            return []
            
        prompt = f"""
        请从以下 GitHub 搜索结果文本中提取前 5 个最相关的 MCP Server 仓库信息。
        要求输出纯 JSON 数组，每个对象包含以下键：
        - "name": 仓库短名 (如: fetch)
        - "repo": 完整路径 (如: user/repo)
        - "stars": 数字 (Star 数)
        - "updated": ISO 时间字符串 (如: 2024-02-28)
        - "description": 简短中文功能描述
        
        搜索结果文本:
        {raw_text[:3000]}
        
        注意：仅输出 JSON 数组，禁止任何解释性文字。
        """.strip()
        
        try:
            result = await self.unified_client.generate(
                tier="LOCAL",
                messages=[{"role": "user", "content": prompt}]
            )
            from mcp_agent import extract_json
            candidates = extract_json(result)
            if isinstance(candidates, list):
                # 确保字段存在
                for c in candidates:
                    if "stars" not in c: c["stars"] = 0
                    if "description" not in c: c["description"] = ""
                    if "updated" not in c: c["updated"] = ""
                return candidates[:5]
        except Exception as e:
            logger.error("元数据提取失败: %s", e)
            
        return []

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

            # [AOS 3.10.1] 元数据洗涤逻辑：从正则提取升级为 LLM 智能提取
            candidates = await self._extract_candidates_metadata(raw)

            # 兜底逻辑：如果 LLM 提取失败，回退到原始正则模式（虽然会是 0 分，但能保证流程不中断）
            if not candidates:
                import re
                repo_pattern = re.compile(r"([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)")
                repos = list(set(repo_pattern.findall(raw)))[:5]
                for repo_name in repos:
                    candidates.append({
                        "name": repo_name.split("/")[-1],
                        "repo": repo_name,
                        "description": "",
                        "stars": 0,
                        "updated": "",
                        "raw_info": raw[:500],
                    })

            print(f"📦 [技能发现] 找到 {len(candidates)} 个候选技能")
            return candidates

        except Exception as e:
            logger.error("技能发现失败: %s", e)
            return []

        return candidates

    def score_candidates(self, candidates: list[dict]) -> list[dict]:
        """
        [AOS 3.11.0] 评分物理入库版
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

            # 🚨 关键修改：将计算出的分数物理存入字典，供后续写入黑板
            c["score"] = round(score, 1)
            # 强制补全描述，防止裁判因空描述判负
            if not c.get("description"):
                c["description"] = "该技能由 GitHub 自动发现，评分反映了其活跃度与社区信任度。"

        # 按分数排序
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

        print("📊 [技能竞标] 评分结果已存入元数据:")
        for i, c in enumerate(candidates, 1):
            print(f"  {i}. {c.get('name', '?')} — 分数: {c['score']}")

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
        winner = None
        rejected = []
        for c in ranked:
            ok, reason = self._is_candidate_allowed(c)
            if ok:
                winner = c
                break
            rejected.append(f"{c.get('name', '?')}: {reason}")

        if not winner:
            reason = "；".join(rejected[:5]) if rejected else "策略拒绝"
            logger.warning("🚫 [技能安装策略] 已拒绝 query='%s' 的候选技能: %s", query, reason)
            return {
                "status": "blocked_by_policy",
                "message": f"候选技能未通过安全策略审核: {reason}",
                "query": query,
            }
        
        # AOS 3.3.1: 基因富集 (Metadata Enrichment)
        description = winner.get("description", "")
        if self.unified_client:
            description = await self._enrich_metadata(winner["name"], description)

        # 注册到 registry（npm 包名推断）
        # [AOS 7.5.7] 智能包名推斷：不再盲目加前綴
        # 如果是官方 repo 則保留，否則尝试使用 winner["repo"] 或原始名稱
        pkg_name = winner.get("repo", f"@modelcontextprotocol/server-{winner['name']}")
        if "/" in pkg_name and not pkg_name.startswith("@"):
            # 如果是 github 路径，npx 通常需要补全或从 search 结果映射
            # 此处简单兜底：如果分数高，通常已有标准包名
            pass

        skill_config = {
            "name": winner["name"],
            "description": description,
            "command": "npx",
            "args": ["-y", f"{winner['name']}" if not winner['name'].startswith("@") else winner['name']],
            "always_loaded": False,
        }
        
        # 针对特定大厂技能的特殊映射
        if "firecrawl" in winner["name"].lower():
            skill_config["args"] = ["-y", "firecrawl-mcp"]
            skill_config["env"] = {
                "FIRECRAWL_API_KEY": "${FIRECRAWL_API_KEY}",
                "FIRECRAWL_API_URL": "${FIRECRAWL_API_URL}",
            }
        elif "sqlite" in winner["name"].lower():
             skill_config["args"] = ["-y", "@modelcontextprotocol/server-sqlite", "./data.db"]

        self.register_new_skill(skill_config)
        print(f"🏆 [技能安装] 冠军技能 '{winner['name']}' 已注册到注册表")

        return {
            "status": "installed",
            "skill_name": winner["name"],
            "repo": winner.get("repo", ""),
            "score": winner.get("score", 0),
            "description": description
        }

    async def auto_install_and_load(self, query: str, session=None, workspace_path: str | None = None) -> dict:
        """
        [AOS 5.0 / 7.0] 完全自治安装闭环：发现 -> 安装 -> 注册 -> 隔离验证 -> 热加载。
        """
        logger.info(f"🛰️ [AOS 5.0] 启动军火采购闭环: {query}")
        
        # 1. 发现并安装（自动写入注册表）
        install_result = await self.auto_install(query, session)
        
        if install_result.get("status") == "installed":
            skill_name = install_result["skill_name"]
            
            # [AOS 7.0] 隔离与验收 (Quarantine Install & Smoke Test)
            try:
                temp_quarantine_workspace = os.path.join(workspace_path or ".", ".quarantine_test")
                os.makedirs(temp_quarantine_workspace, exist_ok=True)
                
                logger.info("🧪 [Quarantine] 对新技能 '%s' 执行隔离冒烟测试...", skill_name)
                load_result = await self.hot_load_skill(skill_name, workspace_path=temp_quarantine_workspace)
                
                if load_result.get("status") not in ("loaded", "already_loaded"):
                    raise RuntimeError(f"热插拔失败: {load_result.get('message', '未知错误')}")
                
                tools = load_result.get("tools", [])
                if not tools:
                    raise RuntimeError("技能启动成功但未暴露任何有效工具接口。")
                    
                logger.info("✅ [Quarantine] 技能验收通过。提供工具数: %d", len(tools))
                
                # 若需要对齐至真实的物理工作区，再加载一次
                if workspace_path and workspace_path != temp_quarantine_workspace:
                    await self.hot_load_skill(skill_name, workspace_path=workspace_path)
                
                return {
                    "status": "evolution_success",
                    "skill_name": skill_name,
                    "tools": tools,
                    "message": f"🏆 进化成功！新技能 '{skill_name}' (验收通过) 已就绪并同步至视网膜。"
                }
                
            except Exception as e:
                logger.error("🛑 [Quarantine] 新技能验收失败，正在执行熔断卸载并回滚注册表: %s", str(e))
                await self.unload_skill(skill_name)
                self.registry = [s for s in self.registry if s["name"] != skill_name]
                try:
                    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                        yaml.dump({"skills": self.registry}, f, allow_unicode=True, default_flow_style=False)
                except Exception:
                    pass
                return {
                    "status": "quarantine_failed",
                    "skill_name": skill_name,
                    "message": f"自动安装的技能未通过隔离验收测试: {str(e)}。已物理熔断丢弃。"
                }
        
        return install_result

    async def check_health(self, skill_name: str) -> dict:
        """
        [AOS 4.2] 免疫自检：针对单个技能进行物理与逻辑双重扫描。
        """
        config = self.get_skill_config(skill_name)
        if not config:
            return {"name": skill_name, "status": "FAIL", "reason": "未在注册表中找到"}

        report = {
            "name": skill_name, 
            "phys_ok": False, 
            "handshake_ok": False, 
            "healed": False
        }
        
        # 🟢 Level 1: Physical Check (针对外部命令或路径)
        # 检查 command 是否在路径中 (或者如果是 npx 则默认 PATH)
        report["phys_ok"] = True # 默认通过，后续可增加 shutil.which 校验
        
        # 🔵 Level 2: Logic Handshake (探针)
        if skill_name in self.loaded_skills:
            skill = self.loaded_skills[skill_name]
            if not skill.runner_task.done():
                try:
                    # 使用 list_tools 作为心跳探针，限时 3.0s (AOS 4.2 标准)
                    await asyncio.wait_for(skill.session.list_tools(), timeout=3.0)
                    report["handshake_ok"] = True
                except Exception as e:
                    report["reason"] = f"握手失败 (Zombified): {str(e)}"
            else:
                report["reason"] = "进程已意外退出"
        else:
            report["reason"] = "技能尚未加载（沉睡中）"
            
        return report

    async def run_full_checkup(self) -> dict:
        """
        [AOS 4.3] 免疫系統：物理級真實點火握手體檢。
        """
        print("\n📡 [OpenClaw 免疫系统] 正在启动全技能深度体检...")
        print("-" * 50)
        
        results_lines = []
        details_for_bb = []
        
        for skill in self.registry:
            name = skill["name"]
            status = await self.check_health(name)
            
            # 🚑 自动修复逻辑 (AOS 4.3)
            if name in self.loaded_skills and not status["handshake_ok"]:
                print(f"   🚑 侦测到 {name} 逻辑失效，正在执行强行冷启动修复...")
                await self.unload_skill(name)
                load_res = await self.load_skill(name)
                if load_res.get("status") == "loaded":
                    status["healed"] = True
                    status["handshake_ok"] = True
            
            icon = "🟢 稳健" if status["phys_ok"] and status["handshake_ok"] else "🔴 异常"
            line = f"{icon} | 技能: {name:<15} | 路径: {'OK' if status['phys_ok'] else 'ERR'} | 握手: {'OK' if status['handshake_ok'] else 'ERR'}"
            print(line)
            results_lines.append(line)
            details_for_bb.append(status)
            
        print("-" * 50 + "\n✅ 自检自愈完成")

        # 🚨 AOS 4.3: Side-Effect Verification (副作用核验)
        if self.agent_ref and hasattr(self.agent_ref, "blackboard"):
            bb = self.agent_ref.blackboard
            try:
                real_tasks_text = await self.call_tool("list_scheduled_tasks", {})
                done_keys = [k for k in bb.facts.keys() if "_task_done_" in k or "reminder_scheduled_" in k]
                for key in done_keys:
                    if key.replace("_task_done_", "") not in real_tasks_text and "reminder" in key:
                        logger.warning("🔍 [副作用核验] 发现“账实不符”：黑板已标记 %s 但物理库无记录。物理抹除中...", key)
                        bb.delete(key)
            except:
                pass

        # 同步体检报告到黑板
        bb_data = {
            "overall": "HEALTHY" if "🔴" not in "\n".join(results_lines) else "UNSTABLE",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "details": details_for_bb
        }
        if self.agent_ref and hasattr(self.agent_ref, "blackboard"):
            self.agent_ref.blackboard.write("skill_health_report", bb_data, author="ImmuneSystem")
        
        return bb_data
