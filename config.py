"""
配置管理模块
从 .env 文件加载所有配置项，统一管理 LLM 和 MCP 服务端参数。
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# 模型配置体系
# 1. 线上模型 (Cloud)
CLOUD_API_KEY = os.getenv("CLOUD_LLM_API_KEY", os.getenv("LLM_API_KEY", ""))
CLOUD_BASE_URL = os.getenv("CLOUD_LLM_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"))
CLOUD_MODEL = os.getenv("CLOUD_LLM_MODEL", os.getenv("LLM_MODEL", "deepseek-chat"))

# 2. 本地模型 (Local)
LOCAL_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "ollama")
LOCAL_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LOCAL_MODEL = os.getenv("LOCAL_LLM_MODEL", "GLM-PureGPU")

# 综合模式配置
AGENT_MODE = os.getenv("AGENT_MODE", "AUTO").upper()

# MCP 服务端配置
MCP_COMMAND = os.getenv("MCP_COMMAND", os.getenv("MCP_GITHUB_COMMAND", "npx"))
MCP_ARGS = os.getenv("MCP_ARGS", os.getenv("MCP_GITHUB_ARGS", "-y,@modelcontextprotocol/server-github")).split(",")

# NOTE: MCP 服务端所需要环境变量
_mcp_env_raw = os.getenv("MCP_ENV", "")
MCP_ENV: dict[str, str] = {}
if _mcp_env_raw:
    for pair in _mcp_env_raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            MCP_ENV[k.strip()] = v.strip()

# 记忆持久化
MEMORY_FILE = os.getenv("MEMORY_FILE", "memory.json")

# AOS: Token 预算上限（单次任务最大 Token 消耗，超过则挂起等待用户确认）
TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", "50000"))
