"""
MCP 工具 → OpenAI Tools 格式转换器
将 MCP list_tools() 返回的 JSON Schema 转为 OpenAI function calling 所需格式。
"""

from mcp.types import Tool


def convertMcpToolsToOpenai(mcpTools: list[Tool]) -> list[dict]:
    """
    将 MCP 工具描述转换为 OpenAI tools 参数格式
    @param mcpTools MCP list_tools() 返回的工具列表
    @returns OpenAI chat.completions.create() 所需的 tools 参数
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema,
            },
        }
        for tool in mcpTools
    ]
