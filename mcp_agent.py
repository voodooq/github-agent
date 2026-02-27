import asyncio
import json
import logging
import os
from typing import AsyncGenerator
from openai import AsyncOpenAI
from mcp import ClientSession
from tool_converter import convertMcpToolsToOpenai
from prompts import EXPERT_REGISTRY, COORDINATOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class UnifiedClient:
    """
    统一 LLM 客户端：支持云端/本地路由与自动降级回退。
    """

    def __init__(self, cloud_config: dict, local_config: dict):
        self.cloud_config = cloud_config
        self.local_config = local_config
        self.cloud_client = AsyncOpenAI(api_key=cloud_config["api_key"], base_url=cloud_config["base_url"])
        self.local_client = AsyncOpenAI(api_key=local_config["api_key"], base_url=local_config["base_url"])

    async def generate(self, tier: str, system_prompt: str, user_content: str, response_format: dict | None = None) -> str:
        """
        根据层级决定调用哪个模型，支持回退。
        """
        # 定义层级对应的模型顺序
        if tier in ("PREMIUM", "LONG_CONTEXT"):
            targets = [
                (self.cloud_client, self.cloud_config["model"], "CLOUD"),
                (self.local_client, self.local_config["model"], "LOCAL")
            ]
        else:
            targets = [
                (self.local_client, self.local_config["model"], "LOCAL"),
                (self.cloud_client, self.cloud_config["model"], "CLOUD")
            ]

        last_error = None
        for client, model, label in targets:
            if not client.api_key: continue
            try:
                kwargs = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    "timeout": 60
                }
                if response_format:
                    kwargs["response_format"] = response_format

                # AsyncOpenAI client is asynchronous
                response = await client.chat.completions.create(**kwargs)
                return response.choices[0].message.content
            except Exception as e:
                logger.warning(f"模型 {label}({model}) 调用失败: {e}，正在尝试降级...")
                last_error = e
                continue
        
        raise Exception(f"所有算力层级均调用失败: {last_error}")

    async def generate_stream(self, tier: str, messages: list[dict], tools: list[dict] | None = None):
        """
        流式生成，主要用于 Coordinator 或 Chat。
        """
        # 简单实现：Chat 优先云端，出错不降级（保持上下文一致性）
        client = self.cloud_client if self.cloud_config["api_key"] else self.local_client
        model = self.cloud_config["model"] if self.cloud_config["api_key"] else self.local_config["model"]
        
        kwargs = {
            "model": model,
            "messages": messages,
            "stream": True
        }
        if tools:
            kwargs["tools"] = tools

        return await client.chat.completions.create(**kwargs)


class McpAgent:
    """
    极简 MCP Agent
    通过 OpenAI 兼容接口调用 LLM，通过 MCP Session 调用工具，
    使用 messages 列表维护多轮对话记忆。
    """

    def __init__(
        self,
        cloud_config: dict,
        local_config: dict,
        systemPrompt: str = "你是一个智能助手。",
        mode: str = "AUTO",
    ):
        self.unified_client = UnifiedClient(cloud_config, local_config)
        self.cloud_model = cloud_config["model"]
        self.local_model = local_config["model"]
        self.systemPrompt = systemPrompt
        # 核心记忆字典：{ context_id: messages_list }
        self.mode = mode
        # 记忆管理配置
        self.memory_dir = "memories"
        os.makedirs(self.memory_dir, exist_ok=True)
        # 运行时缓存 (context_id -> messages)
        self.memories: dict[str, list[dict]] = {}
        
    async def connect(self, session: ClientSession) -> list[str]:
        """
        绑定 MCP Session 并获取可用工具列表
        @param session 已初始化的 MCP ClientSession
        @returns 可用工具名称列表
        """
        self.session = session
        mcpTools = await session.list_tools()
        self.openaiTools = convertMcpToolsToOpenai(mcpTools.tools)
        toolNames = [t.name for t in mcpTools.tools]
        logger.info("已加载 %d 个 MCP 工具: %s", len(toolNames), toolNames)
        return toolNames

    async def chat(self, userInput: str) -> AsyncGenerator[str, None]:
        """
        核心 ReAct 循环（流式版）：接收用户输入，产生流式回复块。
        """
        if "main" not in self.memories:
            self.memories["main"] = [{"role": "system", "content": self.systemPrompt}]
            
        self.memories["main"].append({"role": "user", "content": userInput})
        MAX_ITERATIONS = 10
        for _ in range(MAX_ITERATIONS):
            kwargs: dict = {
                "messages": self.memories["main"],
            }
            # Chat 模式下模型选择
            model = self.cloud_model if self.unified_client.cloud_config["api_key"] else self.local_model
            client = self.unified_client.cloud_client if self.unified_client.cloud_config["api_key"] else self.unified_client.local_client

            # Chat 模式下模型选择，这里统一使用 UnifiedClient 的 generate_stream
            # tier 暂时固定为 PREMIUM，后续可根据 mode 或其他逻辑调整
            tier = "PREMIUM" # Or "LOCAL" based on self.mode or other logic
            
            fullContent = ""
            toolCallsDict = {}  # 用于按索引累计 tool_calls 数据

            response = await self.unified_client.generate_stream(
                tier=tier,
                messages=self.memories["main"],
                tools=self.openaiTools if self.openaiTools else None
            )

            async for chunk in response:
                delta = chunk.choices[0].delta
                
                # 处理文本流
                if delta.content:
                    fullContent += delta.content
                    yield delta.content

                # 处理工具调用流
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in toolCallsDict:
                            toolCallsDict[idx] = {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            toolCallsDict[idx]["id"] = tc.id
                        if tc.function.name:
                            toolCallsDict[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            toolCallsDict[idx]["function"]["arguments"] += tc.function.arguments

            # 流完后，将最终消息存入历史
            assistantMsg = {"role": "assistant", "content": fullContent or None}
            if toolCallsDict:
                assistantMsg["tool_calls"] = list(toolCallsDict.values())
            
            self.memories["main"].append(assistantMsg)

            # 如果没有工具调用，结束循环
            if not toolCallsDict:
                return

            # 执行工具调用
            for tc in assistantMsg["tool_calls"]:
                funcName = tc["function"]["name"]
                arguments = json.loads(tc["function"]["arguments"])
                logger.info("调用工具: %s(%s)", funcName, json.dumps(arguments, ensure_ascii=False)[:200])

                try:
                    result = await self.session.call_tool(funcName, arguments=arguments)
                    resultText = str(result.content)
                except Exception as e:
                    logger.error("工具调用失败: %s - %s", funcName, e)
                    resultText = f"工具调用失败: {e}"

                self.memories["main"].append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": resultText,
                    }
                )

        yield "已达到最大工具调用次数，请简化您的请求。"

    def _get_memory_path(self, context_id: str) -> str:
        """获取指定上下文的记忆文件路径"""
        safe_name = context_id.lower().replace(" ", "_")
        return os.path.join(self.memory_dir, f"{safe_name}.json")

    def clearMemory(self, context_id: str = "main", systemPrompt: str | None = None):
        """
        清理指定上下文的记忆（同时删除物理文件）
        """
        prompt = systemPrompt or self.systemPrompt
        self.memories[context_id] = [{"role": "system", "content": prompt}]
        
        path = self._get_memory_path(context_id)
        if os.path.exists(path):
            os.remove(path)
        logger.info("🗑️ 已清理并重置记忆: %s", context_id)

    def clearAllMemories(self):
        """
        核爆式清理：删除 memories/ 目录下所有文件并重置内存
        """
        if os.path.exists(self.memory_dir):
            for filename in os.listdir(self.memory_dir):
                file_path = os.path.join(self.memory_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        self.memories = {}
        logger.info("💥 已清理所有 Agent 的记忆文件，系统重置为出厂状态。")

    def saveMemory(self, context_id: str = "main"):
        """
        持久化指定上下文的记忆
        """
        if context_id not in self.memories:
            return
        path = self._get_memory_path(context_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.memories[context_id], f, ensure_ascii=False, indent=2)
        logger.debug("💾 记忆已保存: %s", context_id)

    def saveAllMemories(self):
        """保存当前所有已加载的上下文记忆"""
        for ctx_id in self.memories.keys():
            self.saveMemory(ctx_id)
        logger.info("💾 所有 Agent 记忆已持久化到 %s/", self.memory_dir)

    def loadMemory(self, context_id: str = "main", system_prompt: str | None = None) -> list[dict]:
        """
        异步加载指定上下文的记忆
        """
        path = self._get_memory_path(context_id)
        prompt = system_prompt or self.systemPrompt
        
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.memories[context_id] = data
                        logger.info("🧠 已从文件恢复 [%s] 的历史记忆", context_id)
                        return data
            except Exception as e:
                logger.error("加载记忆失败 %s: %s", context_id, e)
        
        # 初始状态
        initial_msg = [{"role": "system", "content": prompt}]
        self.memories[context_id] = initial_msg
        return initial_msg

    def _parse_github_url(self, url: str) -> tuple[str, str]:
        """从 GitHub URL 中解析 owner 和 repo"""
        parts = url.rstrip("/").split("/")
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return "", ""

    async def adaptive_load_data(self, repo_url: str, expert_config: dict | None = None) -> str:
        """
        自适应数据加载：根据专家算力层级决定分析深度。
        """
        owner, repo = self._parse_github_url(repo_url)
        if not owner or not repo:
            raise ValueError(f"无法解析 GitHub URL: {repo_url}")

        tier = expert_config.get("tier", "LOCAL") if expert_config else "LOCAL"
        print(f"📂 [{self.mode} | {tier}] 正在扫描仓库结构...")
        
        try:
            # 1. 扫描文件树 (使用 get_file_contents 读取目录)
            tree = await self.session.call_tool("get_file_contents", arguments={
                "owner": owner,
                "repo": repo,
                "path": "."
            })
            tree_content = str(tree.content)
            
            # 2. 确定待加载文件
            files_to_read = ["README.md"]
            if tier in ("PREMIUM", "LONG_CONTEXT") or self.mode == "TURBO":
                # 深度模式：搜寻核心代码
                candidates = ["requirements.txt", "package.json", "main.py", "app.py", "index.ts", "src/index.ts", "setup.py", "go.mod"]
                found_files = [f for f in candidates if f in tree_content]
                # 限制读取数量，防止上下文溢出
                limit = 5 if tier == "LONG_CONTEXT" else 2
                files_to_read.extend(found_files[:limit])

            # 3. 批量读取源码
            print(f"🚀 正在加载上下文文件: {files_to_read}...")
            read_tasks = [self.session.call_tool("get_file_contents", arguments={
                "owner": owner, 
                "repo": repo, 
                "path": f
            }) for f in files_to_read]
            file_results = await asyncio.gather(*read_tasks)
            
            source_context = f"--- 文件树结构 ---\n{tree_content}\n\n"
            for i, r in enumerate(file_results):
                source_context += f"--- 文件内容: {files_to_read[i]} ---\n{str(r.content)}\n\n"
            
            # 4. 数据脱水逻辑 (如果专家需要且是本地模型可处理的)
            if expert_config and expert_config.get("need_preprocess", False):
                print("🏠 正在触发本地模型进行数据脱水清洗...")
                wash_prompt = "你是一个代码清洗助手。请删除以下代码中的许可证声明、冗长注释、HTML标签和非逻辑代码，只保留核心业务逻辑和属性定义。保持代码紧凑。"
                source_context = await self.unified_client.generate("LOCAL", wash_prompt, source_context)
            
            return source_context
        except Exception as e:
            logger.error("数据加载失败: %s", e)
            return f"数据加载部分失败: {e}"

    async def expertReview(self, expertName: str, config: dict, projectData: str) -> dict:
        """
        单个专家的异步评审逻辑，包含独立记忆持久化。
        """
        logger.info("专家评审启动: %s (%s)", expertName, config["tier"])
        
        # 1. 加载专家的持久化记忆
        messages = self.loadMemory(expertName, config["prompt"])
        
        # 2. 追加当前评审任务 (不作为长期记忆，仅作为当前上下文)
        # 专家通常不需要记住所有评审过的代码，但可以记住历史发现的规律
        # 这里演示为：将项目数据作为一次性 user 输入
        current_messages = messages + [{"role": "user", "content": f"请评价这个项目：\n{projectData}"}]
        
        try:
            resultText = await self.unified_client.generate(
                tier=config["tier"],
                system_prompt=config["prompt"], # 注意这里统一由 UnifiedClient 处理 System Prompt
                user_content=f"请评价这个项目：\n{projectData}", # 简化处理，暂时不让专家在 messages 中累积无限代码
                response_format={"type": "json_object"}
            )
            # 3. 如果需要专家学习，可以在这里 append resultText 到 messages
            # messages.append({"role": "assistant", "content": resultText})
            # self.saveMemory(expertName)
            
            return {expertName: json.loads(resultText)}
        except Exception as e:
            logger.error("专家 %s 评审失败: %s", expertName, e)
            return {expertName: {"error": str(e)}}

    async def multiAgentReview(self, repo_url: str) -> AsyncGenerator[str, None]:
        """
        混合多 Agent 评审流：并发调度、算力路由、自适应加载。
        """
        print(f"\n🚀 [Hybrid Mode: {self.mode}] 启动 OpenClaw 多专家联合评审团...")
        
        expert_results = []
        # 将注册表转换为任务列表
        expert_items = list(EXPERT_REGISTRY.items())

        # 1. 专家任务分发
        async def _run_expert_task(name, cfg):
            # NOTE: 传入完整 URL，由 adaptive_load_data 内部解析 owner/repo
            context = await self.adaptive_load_data(repo_url, cfg)
            # 执行评审
            return await self.expertReview(name, cfg, context)

        if self.mode != "SEQUENTIAL":
            # 并发模式 (Turbo / Auto)
            tasks = [_run_expert_task(name, cfg) for name, cfg in expert_items]
            expert_results = await asyncio.gather(*tasks)
        else:
            # 串行模式 (本地保护)
            for name, cfg in expert_items:
                print(f" -> {name} 正在工作...", end="", flush=True)
                res = await _run_expert_task(name, cfg)
                expert_results.append(res)
                print(" ✅")
        
        # 2. 汇总专家意见
        expertReviewsJson = json.dumps(expert_results, ensure_ascii=False, indent=2)
        
        # 3. 协调员生成最终报告
        print("✍️  正在由首席协调员汇总报告...")
        full_system_prompt = COORDINATOR_SYSTEM_PROMPT.format(expert_reviews=expertReviewsJson)

        response = await self.unified_client.generate_stream("PREMIUM", [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": "请基于以上各领域专家的深度审计，给出你作为产品经理的最终投资/选型建议。"}
        ])
        
        async for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                yield content
