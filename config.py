"""
配置管理模块
从 .env 文件加载所有配置项，统一管理 LLM 和 MCP 服务端参数。
"""

import os
from dotenv import load_dotenv

load_dotenv()

# LLM 配置
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# MCP 服务端配置
MCP_COMMAND = os.getenv("MCP_COMMAND", "npx")
MCP_ARGS = os.getenv("MCP_ARGS", "-y,@modelcontextprotocol/server-github").split(",")

# NOTE: MCP 服务端可能需要专属环境变量（如 GITHUB_TOKEN），在此解析
_mcp_env_raw = os.getenv("MCP_ENV", "")
MCP_ENV: dict[str, str] = {}
if _mcp_env_raw:
    for pair in _mcp_env_raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            MCP_ENV[k.strip()] = v.strip()

# Agent 配置
MEMORY_FILE = os.getenv("MEMORY_FILE", "")

# 评审模式配置: AUTO, TURBO, SEQUENTIAL
AGENT_MODE_ENV = os.getenv("AGENT_MODE", "AUTO").upper()

def resolve_agent_mode(mode_env: str, base_url: str) -> str:
    if mode_env in ("TURBO", "SEQUENTIAL"):
        return mode_env
    # 自动识别: 本地模型(localhost/127.0.0.1)默认使用 SEQUENTIAL
    if "localhost" in base_url or "127.0.0.1" in base_url:
        return "SEQUENTIAL"
    return "TURBO"

AGENT_MODE = resolve_agent_mode(AGENT_MODE_ENV, LLM_BASE_URL)
