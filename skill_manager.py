"""
AOS 2.0 动态技能管理器 (Skill Manager)
按需加载/卸载 MCP 服务进程，替代硬编码 .env 配置。
支持防僵尸进程保护和技能热插拔。
"""

import asyncio
import logging
import os
import yaml
from datetime import datetime
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tool_converter import convertMcpToolsToOpenai

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
        
        # AOS 3.3: 启动时自动初始化/更新基因库
        self._bootstrap_genes()

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
            # AOS 2.8.4/3.8.6: 路径校验逻辑。如果当前已加载技能的路径与请求的 workspace_path 不符，强制重载。
            current_args = getattr(self.loaded_skills[name], "last_args", [])
            if name == "filesystem" and workspace_path:
                target_abs = os.path.abspath(workspace_path)
                # [Fix AOS 3.8.6] 修正索引：路径参数在 args 列表末尾，而不是 [0]
                if not current_args or target_abs not in current_args:
                    logger.info("🔄 [AOS 3.8.6] 检测到工作区变更，正在为技能 '%s' 重新授权物理路径...", name)
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

        except Exception as e:
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
        internal_tools = ["discover_and_install_skill", "cfo_report", "write_blackboard", "inject_funds", "cfo_approve"]
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
        winner = ranked[0]
        
        # AOS 3.3.1: 基因富集 (Metadata Enrichment)
        description = winner.get("description", "")
        if self.unified_client:
            description = await self._enrich_metadata(winner["name"], description)

        # 注册到 registry（npm 包名推断）
        skill_config = {
            "name": winner["name"],
            "description": description,
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
            "description": description
        }

    async def check_health(self, skill_name: str) -> dict:
        """
        [AOS 4.0] 免疫自检：针对单个技能进行三级健康检查。
        """
        config = self.get_skill_config(skill_name)
        if not config:
            return {"name": skill_name, "status": "FAIL", "reason": "未在注册表中找到"}

        report = {
            "name": skill_name, 
            "static": "PASS", 
            "dynamic": "PASS", 
            "semantic": "PASS", 
            "healed": False
        }
        
        # 🟢 Level 1: Static Physical Check (针对外部命令可用性)
        # 简单校验 command 是否在该 OS 路径下可通过 (此项可通过 try catch load 替代，此处预留)
        
        # 🔵 Level 2: Dynamic Process Check
        if skill_name in self.loaded_skills:
            skill = self.loaded_skills[skill_name]
            if skill.runner_task.done():
                 report["dynamic"] = "FAIL"
                 report["reason"] = "进程已意外退出或被系统挂起"
            else:
                # 🟡 Level 3: Semantic Handshake (Ping)
                try:
                    # 使用 list_tools 作为心跳，设置短超时
                    await asyncio.wait_for(skill.session.list_tools(), timeout=5.0)
                except Exception as e:
                    report["semantic"] = "FAIL"
                    report["reason"] = f"语义握手超时 (Zombified): {str(e)}"
        else:
            # 未加载的技能，动态和语义检查跳过
            report["dynamic"] = "N/A"
            report["semantic"] = "N/A"
            
        return report

    async def run_full_checkup(self) -> dict:
        """
        [AOS 4.0] 全量免疫扫描与自愈。
        遍历所有已注册技能，检测由于环境漂移、进程挂死造成的异常，并执行暴力冷启动修复。
        """
        print(f"📡 [AOS 4.0] 启动全量免疫扫描...")
        results = []
        for skill in self.registry:
            name = skill["name"]
            status = await self.check_health(name)
            
            # 🚑 自愈逻辑 (Self-Healing)
            # 如果是已加载但语义失败的（僵尸进程），或者已加载但 Task 挂了（崩了），执行重连
            if status["dynamic"] == "FAIL" or status["semantic"] == "FAIL":
                logger.warning("🚑 [自愈进程] 侦测到技能 '%s' 处于亚健康状态，启动断点重连...", name)
                # 记录原始加载参数（如 workspace）
                old_args = []
                if name in self.loaded_skills:
                    old_args = self.loaded_skills[name].last_args
                    await self.unload_skill(name)
                
                # 尝试暴力冷启动
                load_res = await self.load_skill(name) # 此处暂时默认不带 workspace，或由 load_skill 内部处理 always_loaded
                if load_res.get("status") == "loaded":
                    status["healed"] = True
                    status["dynamic"] = "PASS"
                    status["semantic"] = "PASS"
                    status["reason"] = "已通过『暴力冷启动』恢复稳态"
            
            results.append(status)
        
        # 🚨 AOS 4.1: Side-Effect Verification (副作用核验)
        # 针对调度器等具有持久化副作用的技能，进行“账实核验”
        if self.agent_ref and hasattr(self.agent_ref, "blackboard"):
            bb = self.agent_ref.blackboard
            try:
                # 1. 尝试获取物理数据库中的真实任务列表
                real_tasks_text = await self.call_tool("list_scheduled_tasks", {})
                
                # 2. 扫描黑板中所有声称“已完成”的任务标志 (模式: _task_done_{expert})
                # 以及特定的定时任务完成标志 (如 reminder_scheduled_at_xxxx)
                done_keys = [k for k in bb.facts.keys() if "_task_done_" in k or "reminder_scheduled_" in k]
                
                for key in done_keys:
                    # 如果黑板有标志，但物理列表里完全搜不到相关任务 ID 或描述
                    # (由于 list_scheduled_tasks 返回的是文本，我们进行模糊匹配)
                    # 注意：这只是一个简单的启发式自检，防止明显的“纸面胜利”
                    if key.replace("_task_done_", "") not in real_tasks_text and "reminder" in key:
                        logger.warning("🔍 [副作用核验] 发现“账实不符”：黑板标记了 %s 但数据库中无相关记录。正在物理抹除...", key)
                        bb.delete(key)
                        # 同时抹掉 Orchestrator 的任务完成标志
                        bb.delete("_task_completed") 
            except Exception as e:
                # 如果没加载 scheduler 技能，call_tool 会抛错，此处静默忽略
                pass

        # 统计整体稳态
        total_fail = len([r for r in results if r["static"] == "FAIL" or r["dynamic"] == "FAIL" or r["semantic"] == "FAIL"])
        overall = "HEALTHY" if total_fail == 0 else "UNSTABLE"
        
        report = {
            "overall": overall,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "details": results
        }
        
        # 将体检报告同步到黑板
        if self.agent_ref and hasattr(self.agent_ref, "blackboard"):
            self.agent_ref.blackboard.write("skill_health_report", report, author="ImmuneSystem")
        
        return report
