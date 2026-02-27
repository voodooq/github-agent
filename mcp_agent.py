"""
MCP Agent 核心模块
实现 ReAct 循环 + 多轮对话记忆 + MCP 工具调用。
"""

import asyncio
import json
import logging
from typing import AsyncGenerator
from openai import OpenAI
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
        self.cloud_client = OpenAI(api_key=cloud_config["api_key"], base_url=cloud_config["base_url"])
        self.local_client = OpenAI(api_key=local_config["api_key"], base_url=local_config["base_url"])

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

                # OpenAI client is synchronous, but we can wrap it or use it as is if not hitting event loop issues.
                # In this simple agent, we use it directly.
                response = client.chat.completions.create(**kwargs)
                return response.choices[0].message.content
            except Exception as e:
                logger.warning(f"模型 {label}({model}) 调用失败: {e}，正在尝试降级...")
                last_error = e
                continue
        
        raise Exception(f"所有算力层级均调用失败: {last_error}")

    def generate_stream(self, tier: str, messages: list[dict]):
        """
        流式生成，主要用于 Coordinator 或 Chat。
        """
        # 简单实现：Chat 优先云端，出错不降级（保持上下文一致性）
        client = self.cloud_client if self.cloud_config["api_key"] else self.local_client
        model = self.cloud_config["model"] if self.cloud_config["api_key"] else self.local_config["model"]
        
        return client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True
        )


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
        self.messages: list[dict] = [{"role": "system", "content": systemPrompt}]
        self.session: ClientSession | None = None
        self.openaiTools: list[dict] = []
        self.mode = mode

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
        self.messages.append({"role": "user", "content": userInput})
        MAX_ITERATIONS = 10
        for _ in range(MAX_ITERATIONS):
            kwargs: dict = {
                "messages": self.messages,
            }
            # Chat 模式下模型选择
            model = self.cloud_model if self.unified_client.cloud_config["api_key"] else self.local_model
            client = self.unified_client.cloud_client if self.unified_client.cloud_config["api_key"] else self.unified_client.local_client

            if self.openaiTools:
                kwargs["tools"] = self.openaiTools

            fullContent = ""
            toolCallsDict = {}  # 用于按索引累计 tool_calls 数据

            response = client.chat.completions.create(model=model, stream=True, **kwargs)

            for chunk in response:
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
            
            self.messages.append(assistantMsg)

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

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": resultText,
                    }
                )

        yield "已达到最大工具调用次数，请简化您的请求。"

    def clearMemory(self, systemPrompt: str | None = None):
        """
        清除对话历史，保留 system prompt
        @param systemPrompt 可选，替换新的 system prompt
        """
        prompt = systemPrompt or self.messages[0]["content"]
        self.messages = [{"role": "system", "content": prompt}]
        logger.info("对话记忆已清除")

    def saveMemory(self, filePath: str):
        """
        将对话历史持久化到 JSON 文件
        @param filePath 保存路径
        """
        with open(filePath, "w", encoding="utf-8") as f:
            json.dump(self.messages, f, ensure_ascii=False, indent=2)
        logger.info("记忆已保存到 %s", filePath)

    def loadMemory(self, filePath: str):
        """
        从 JSON 文件恢复对话历史
        @param filePath 文件路径
        """
        try:
            with open(filePath, "r", encoding="utf-8") as f:
                self.messages = json.load(f)
            logger.info("已从 %s 恢复 %d 条记忆", filePath, len(self.messages))
        except FileNotFoundError:
            logger.info("未找到记忆文件 %s，使用空白记忆", filePath)

    async def adaptive_load_data(self, repo_name: str, expert_config: dict | None = None) -> str:
        """
        自适应数据加载：根据专家算力层级决定分析深度。
        """
        tier = expert_config.get("tier", "LOCAL") if expert_config else "LOCAL"
        print(f"📂 [{self.mode} | {tier}] 正在扫描仓库结构...")
        
        try:
            # 1. 扫描文件树
            tree = await self.session.list_directory(arguments={"path": "./", "repository": repo_name})
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
            read_tasks = [self.session.call_tool("get_file_contents", arguments={"path": f, "repository": repo_name}) for f in files_to_read]
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
        单个专家的异步评审逻辑，自带路由与回退。
        """
        logger.info("专家评审启动: %s (%s)", expertName, config["tier"])
        try:
            resultText = await self.unified_client.generate(
                tier=config["tier"],
                system_prompt=config["prompt"],
                user_content=f"请评价这个项目：\n{projectData}",
                response_format={"type": "json_object"}
            )
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
            # 自适应加载每个专家所需的数据
            repo_name = repo_url.split("/")[-1]
            context = await self.adaptive_load_data(repo_name, cfg)
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

        response = self.unified_client.generate_stream("PREMIUM", [
            {"role": "system", "content": full_system_prompt},
            {"role": "user", "content": "请基于以上各领域专家的深度审计，给出你作为产品经理的最终投资/选型建议。"}
        ])
        
        for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                yield content
