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
from prompts import EXPERT_PROMPTS, COORDINATOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class McpAgent:
    """
    极简 MCP Agent
    通过 OpenAI 兼容接口调用 LLM，通过 MCP Session 调用工具，
    使用 messages 列表维护多轮对话记忆。
    """

    def __init__(
        self,
        apiKey: str,
        baseUrl: str,
        model: str,
        systemPrompt: str = "你是一个智能助手。",
        mode: str = "TURBO",
    ):
        self.client = OpenAI(api_key=apiKey, base_url=baseUrl)
        self.model = model
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
                "model": self.model,
                "messages": self.messages,
                "stream": True,
            }
            if self.openaiTools:
                kwargs["tools"] = self.openaiTools

            fullContent = ""
            toolCallsDict = {}  # 用于按索引累计 tool_calls 数据

            response = self.client.chat.completions.create(**kwargs)

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

    async def adaptive_load_data(self, repo_name: str) -> str:
        """
        自适应数据加载：根据模式决定分析深度。
        """
        print(f"📂 [{self.mode} Mode] 正在获取仓库结构...")
        try:
            # 1. 扫描文件树
            tree = await self.session.call_tool("list_directory", arguments={"path": "./", "repository": repo_name})
            tree_content = str(tree.content)
            
            # 2. 确定待加载文件
            files_to_read = ["README.md"]
            if self.mode == "TURBO":
                # Turbo 模式搜寻核心代码
                candidates = ["requirements.txt", "package.json", "main.py", "app.py", "index.ts", "src/index.ts", "setup.py"]
                # 简单过滤 tree 中存在的文件
                found_files = [f for f in candidates if f in tree_content]
                files_to_read.extend(found_files[:3]) # 最多额外读 3 个核心文件

            # 3. 批量读取源码
            print(f"🚀 正在加载核心上下文: {files_to_read}...")
            read_tasks = [self.session.call_tool("get_file_contents", arguments={"path": f, "repository": repo_name}) for f in files_to_read]
            file_results = await asyncio.gather(*read_tasks)
            source_context = f"--- 文件树结构 ---\n{tree_content}\n\n"
            for i, r in enumerate(file_results):
                source_context += f"--- 文件内容: {files_to_read[i]} ---\n{str(r.content)}\n\n"
            
            return source_context
        except Exception as e:
            logger.error("数据加载失败: %s", e)
            return f"数据加载部分失败: {e}"

    async def expertReview(self, expertName: str, systemPrompt: str, projectData: str) -> dict:
        """
        单个专家的异步评审逻辑
        """
        logger.info("专家评审启动: %s", expertName)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": systemPrompt},
                    {"role": "user", "content": f"请评价这个项目：\n{projectData}"}
                ],
                response_format={"type": "json_object"}
            )
            resultText = response.choices[0].message.content
            return {expertName: json.loads(resultText)}
        except Exception as e:
            logger.error("专家 %s 评审失败: %s", expertName, e)
            return {expertName: {"error": str(e)}}

    async def multiAgentReview(self, projectData: str) -> AsyncGenerator[str, None]:
        """
        多 Agent 评审流：支持 TURBO (并行) 和 SEQUENTIAL (串行) 模式。
        """
        expert_results = []
        expert_tasks_info = list(EXPERT_PROMPTS.items())

        if self.mode == "TURBO":
            print(f"\n🚀 [Turbo Mode] 正在并行调用 {len(expert_tasks_info)} 个专家进行深度审计...")
            tasks = [
                self.expertReview(name, prompt, projectData) 
                for name, prompt in expert_tasks_info
            ]
            expert_results = await asyncio.gather(*tasks)
        else:
            print(f"\n🐢 [Sequential Mode] 正在逐一请教专家 (本地显存优化)...")
            for name, prompt in expert_tasks_info:
                print(f" -> {name} 正在思考...", end="", flush=True)
                res = await self.expertReview(name, prompt, projectData)
                expert_results.append(res)
                print(" ✅")
        
        # 2. 汇总专家意见
        expertReviewsJson = json.dumps(expert_results, ensure_ascii=False, indent=2)
        
        # 3. 协调员生成最终报告
        print("✍️  正在由协调员汇总专家意见并生成最终报告...")
        
        # 可以在此处根据 mode 调整 coordinator prompt 的前缀
        mode_hint = "【注意：当前为 Turbo 深度模式，已阅读源码数据。】" if self.mode == "TURBO" else "【注意：当前为 Sequential 轻量模式，仅阅读了 README 数据。】"
        full_system_prompt = mode_hint + "\n" + COORDINATOR_SYSTEM_PROMPT.format(expert_reviews=expertReviewsJson)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": full_system_prompt},
                {"role": "user", "content": "请基于以上评审意见，给出你的最终咨询建议。"}
            ],
            stream=True
        )
        
        for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                yield content
