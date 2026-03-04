"""
配置管理模块
从 .env 文件加载所有配置项，统一管理 LLM 和 MCP 服务端参数。
"""

import os
import shutil
import logging
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on", "y"}


def resolve_executable_command(command: str) -> str:
    """
    解析可执行命令的绝对路径（尤其是 Windows 下的 npx.cmd）。
    找不到时回退原始命令名，保持兼容性。
    """
    cmd = (command or "").strip()
    if not cmd:
        return command

    lowered = cmd.lower()
    if os.name == "nt" and lowered == "npx":
        return shutil.which("npx.cmd") or shutil.which("npx") or cmd
    return shutil.which(cmd) or cmd


def build_subprocess_env(overlays: dict[str, str] | None = None) -> dict[str, str]:
    """
    为子进程构建环境变量：继承宿主环境并叠加覆盖项。
    """
    env = os.environ.copy()
    if overlays:
        for k, v in overlays.items():
            if v is None:
                continue
            env[str(k)] = str(v)
    return env

# 模型配置体系
# 1. 线上模型 (Cloud)
CLOUD_API_KEY = os.getenv("CLOUD_LLM_API_KEY", os.getenv("LLM_API_KEY", ""))
CLOUD_BASE_URL = os.getenv("CLOUD_LLM_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"))
CLOUD_MODEL = os.getenv("CLOUD_LLM_MODEL", os.getenv("LLM_MODEL", "deepseek-chat"))

# 2. 本地模型 (Local)
LOCAL_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "ollama")
LOCAL_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LOCAL_MODEL = os.getenv("LOCAL_LLM_MODEL", "GLM-PureGPU")

# 综合模式配置 (AUTO, TURBO, SEQUENTIAL, CLOUD)
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

# 统一解析后的 MCP 主命令（主入口/技能加载可共享）
MCP_RESOLVED_COMMAND = resolve_executable_command(MCP_COMMAND)

# 记忆持久化
MEMORY_FILE = os.getenv("MEMORY_FILE", "memory.json")

# AOS: Token 预算上限（单次任务最大 Token 消耗，超过则挂起等待用户确认）
TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", "50000"))

# AOS: 能力缺失 -> 自升级 -> 热加载策略
SELF_UPGRADE_ENABLED = _env_bool("SELF_UPGRADE_ENABLED", True)
SELF_UPGRADE_SAFE_MODE = _env_bool("SELF_UPGRADE_SAFE_MODE", True)
SELF_UPGRADE_MIN_STARS = int(os.getenv("SELF_UPGRADE_MIN_STARS", "20"))
SELF_UPGRADE_MAX_AGE_DAYS = int(os.getenv("SELF_UPGRADE_MAX_AGE_DAYS", "540"))
SELF_UPGRADE_TRUSTED = [x.strip() for x in os.getenv("SELF_UPGRADE_TRUSTED", "").split(",") if x.strip()]
SELF_UPGRADE_DENYLIST = [x.strip() for x in os.getenv("SELF_UPGRADE_DENYLIST", "").split(",") if x.strip()]
SELF_UPGRADE_RETRY_ORIGINAL_CALL = _env_bool("SELF_UPGRADE_RETRY_ORIGINAL_CALL", True)
EVOLUTION_AUDIT_LOG_PATH = os.getenv("EVOLUTION_AUDIT_LOG_PATH", "memories/evolution_audit.log")

# Stable rules injection
ENABLE_AGENT_RULES = _env_bool("ENABLE_AGENT_RULES", True)
AGENT_RULES_PATH = os.getenv("AGENT_RULES_PATH", "AGENT_RULES.md")
SESSION_START_PATH = os.getenv("SESSION_START_PATH", "SESSION_START.md")
AGENT_RULES_MAX_CHARS = int(os.getenv("AGENT_RULES_MAX_CHARS", "12000"))

# Silicon life declaration injection
ENABLE_SURVIVAL_DECLARE = _env_bool("ENABLE_SURVIVAL_DECLARE", True)
SURVIVAL_DECLARE_PATH = os.getenv("SURVIVAL_DECLARE_PATH", "survival_declare.md")
SURVIVAL_DECLARE_MAX_CHARS = int(os.getenv("SURVIVAL_DECLARE_MAX_CHARS", "12000"))


def load_text_contract(path: str, max_chars: int = 12000) -> str:
    """
    安全读取文本契约文件。
    - 文件不存在/读取失败时返回空串，不中断启动流程
    - 超长内容自动截断，避免撑爆上下文
    """
    p = (path or "").strip()
    if not p:
        return ""
    if not os.path.exists(p):
        logger.warning("契约文件不存在，跳过加载: %s", p)
        return ""
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
        if len(content) > max_chars:
            logger.warning("契约文件过长，已截断: %s (%d -> %d)", p, len(content), max_chars)
            content = content[:max_chars] + "\n...[TRUNCATED]"
        return content
    except Exception as e:
        logger.warning("契约文件读取失败，跳过加载: %s | %s", p, e)
        return ""
