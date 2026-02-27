"""
MCP Agent 核心模块
实现 ReAct 循环 + 多轮对话记忆 + MCP 工具调用。
"""

import json
import logging
from openai import OpenAI
from mcp import ClientSession
from tool_converter import convertMcpToolsToOpenai

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
    ):
        self.client = OpenAI(api_key=apiKey, base_url=baseUrl)
        self.model = model
        self.messages: list[dict] = [{"role": "system", "content": systemPrompt}]
        self.session: ClientSession | None = None
        self.openaiTools: list[dict] = []

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

    async def chat(self, userInput: str) -> str:
        """
        核心 ReAct 循环：接收用户输入，返回最终回复。
        若 LLM 请求工具调用，自动通过 MCP 执行并将结果反馈给 LLM，
        循环直到获得纯文本回复。
        @param userInput 用户输入文本
        @returns Agent 的最终回复文本
        """
        self.messages.append({"role": "user", "content": userInput})

        # NOTE: 设置最大循环次数，防止工具调用死循环
        MAX_ITERATIONS = 10

        for _ in range(MAX_ITERATIONS):
            # 构建请求参数：有工具时附带 tools，无工具时不附带
            kwargs: dict = {"model": self.model, "messages": self.messages}
            if self.openaiTools:
                kwargs["tools"] = self.openaiTools

            response = self.client.chat.completions.create(**kwargs)
            msg = response.choices[0].message

            # 没有工具调用 → 返回最终回复
            if not msg.tool_calls:
                content = msg.content or ""
                self.messages.append({"role": "assistant", "content": content})
                return content

            # 有工具调用 → 逐一执行并将结果追加到记忆
            # NOTE: 需要将完整的 assistant message（含 tool_calls）追加到 messages
            self.messages.append(msg.model_dump())

            for toolCall in msg.tool_calls:
                funcName = toolCall.function.name
                # NOTE: 使用 json.loads 替代 eval，更安全
                arguments = json.loads(toolCall.function.arguments)
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
                        "tool_call_id": toolCall.id,
                        "content": resultText,
                    }
                )

        # 超过最大循环次数，返回兜底回复
        return "已达到最大工具调用次数，请简化您的请求。"

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
