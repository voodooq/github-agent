import asyncio
import json
import logging
import os
import re
import time
from typing import AsyncGenerator
from openai import AsyncOpenAI
from mcp import ClientSession
from tool_converter import convertMcpToolsToOpenai
from prompts import EXPERT_REGISTRY, COORDINATOR_SYSTEM_PROMPT
from docker_sandbox import DockerSandboxAgent

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """
    从模型输出中稳健地提取 JSON 对象。支持 markdown 自动剥离。
    """
    if not text or not text.strip():
        raise ValueError("模型返回了空文本，无法提取 JSON")
    text = text.strip()
    # 尝试匹配 ```json ... ``` 或 ``` ... ```
    markdown_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if markdown_match:
        text = markdown_match.group(1)
    else:
        # 尝试匹配第一个 { 和最后一个 }
        bracket_match = re.search(r"(\{.*\})", text, re.DOTALL)
        if bracket_match:
            text = bracket_match.group(1)
            
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {str(e)}\n原文本: {text[:200]}")


class AsyncRateLimiter:
    """
    异步频率限制器，确保请求频率不超过设定的 RPM。
    """
    def __init__(self, rpm: int):
        self.interval = 60.0 / rpm
        self.last_called = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self.last_called
            if elapsed < self.interval:
                wait_time = self.interval - elapsed
                print(f"⏳ [频率控制] 保护中... 需等待 {wait_time:.2f}s 以遵循 40 RPM 限制")
                await asyncio.sleep(wait_time)
                self.last_called = asyncio.get_event_loop().time()
            else:
                self.last_called = now


class TokenBudget:
    """
    Token 预算管理器：跟踪单任务累计 Token 消耗。
    当消耗超过阈值时挂起操作，防止无限循环烧光 API 额度。
    """

    def __init__(self, max_tokens: int = 20000):
        self.max_tokens = max_tokens
        self.consumed = 0
        self.exceeded = False

    def consume(self, tokens: int) -> bool:
        """
        消耗 Token 并检查是否超限。
        @returns True 如果仍在预算内，False 如果已超限
        """
        self.consumed += tokens
        if self.consumed >= self.max_tokens:
            self.exceeded = True
            print(f"🚨 [预算墙] Token 消耗已达 {self.consumed}/{self.max_tokens}，任务已挂起！")
            return False
        return True

    def estimate_tokens(self, text: str) -> int:
        """粗略估算 Token 数（中文约 2 字符/Token，英文约 4 字符/Token）"""
        return max(len(text) // 3, 1)

    def reset(self):
        """重置预算计数器（新任务开始时调用）"""
        self.consumed = 0
        self.exceeded = False

    @property
    def remaining(self) -> int:
        return max(self.max_tokens - self.consumed, 0)

    def __repr__(self) -> str:
        return f"TokenBudget({self.consumed}/{self.max_tokens}, exceeded={self.exceeded})"


class UnifiedClient:
    """
    统一 LLM 客户端：支持云端/本地路由与自动降级回退。
    云端使用 AsyncOpenAI SDK，本地使用 Ollama 原生 HTTP API（绕过兼容层 bug）。
    """

    def __init__(self, cloud_config: dict, local_config: dict, agent_mode: str = "AUTO", economy = None):
        self.cloud_config = cloud_config
        self.local_config = local_config
        self.agent_mode = agent_mode.upper()
        self.economy = economy
        
        # 检测可用性
        self.cloud_available = bool(cloud_config.get("api_key") and cloud_config.get("model"))
        self.local_available = bool(local_config.get("model") and local_config.get("base_url"))
        
        # 内部诊断：打印加载情况（非敏感信息）
        import time
        print(f"DEBUG: Cloud Available={self.cloud_available}, Local Available={self.local_available}")
        print(f"DEBUG: Local Model='{local_config.get('model')}', BaseURL='{local_config.get('base_url')}'")

        
        # 初始化云端客户端
        base_url = cloud_config.get("base_url", "https://api.deepseek.com/v1")
        # [DEFENSIVE] 很多用户会误贴包含 /chat/completions 的完整 URL
        if base_url.endswith("/chat/completions"):
            base_url = base_url.replace("/chat/completions", "")
            logger.info("📡 自动修正 CLOUD_LLM_BASE_URL: 移除了末尾多余的 /chat/completions")

        self.cloud_client = AsyncOpenAI(
            api_key=cloud_config.get("api_key", "none"),
            base_url=base_url,
            timeout=15,  # [AOS 2.1] 降低超时到 15s，减少重试等待感
            max_retries=1,
        )
        
        # [AOS 2.1] 熔断机制：防止线上 API 抽风导致系统假死
        self._cloud_circuit_broken_until = 0.0
        self._consecutive_cloud_failures = 0
        
        # 初始化本地端参数
        local_base = local_config.get("base_url", "http://localhost:11434/v1").replace("/v1", "").rstrip("/")
        self._local_api_url = f"{local_base}/api/chat"
        self._local_model = local_config.get("model", "")
        
        # Ollama 单 GPU 串行推理，信号量防止并发排队
        self._local_semaphore = asyncio.Semaphore(1)
        
        # 频率限制：NVIDIA 限制 40 RPM，我们设为 35 以保证绝对安全
        self.rate_limiter = AsyncRateLimiter(rpm=35)

    @staticmethod
    def _sanitize_messages_for_ollama(messages: list[dict]) -> list[dict]:
        """
        将包含 tool 角色和 tool_calls 的消息序列转换为 Ollama 可识别的格式。
        Ollama 原生 API 不支持 tool 角色，须将其融入 user/assistant 消息。
        """
        cleaned = []
        for msg in messages:
            role = msg.get("role", "")
            
            if role == "tool":
                # 将 tool 响应转为 user 角色的信息
                content = msg.get("content", "")
                cleaned.append({"role": "user", "content": f"[工具返回结果]: {content[:3000]}"})
            elif role == "assistant":
                new_msg = {"role": "assistant", "content": msg.get("content", "") or ""}
                # 如果有 tool_calls，把工具调用意图追加到内容中
                if msg.get("tool_calls"):
                    tool_desc = "; ".join(
                        f"调用{tc.get('function', {}).get('name', '?')}" 
                        for tc in msg["tool_calls"]
                    )
                    new_msg["content"] = (new_msg["content"] or "") + f" [{tool_desc}]"
                if not new_msg["content"]:
                    new_msg["content"] = "(正在思考...)"
                cleaned.append(new_msg)
            else:
                # system / user 消息直接保留
                cleaned.append({"role": role, "content": msg.get("content", "") or ""})
        
        return cleaned


    async def _call_ollama(self, messages: list[dict], format: str | None = None) -> str:
        """
        直接调用 Ollama 原生 /api/chat 端点，绕过 OpenAI 兼容层。
        """
        import httpx
        safe_messages = self._sanitize_messages_for_ollama(messages)
        payload = {
            "model": self._local_model,
            "messages": safe_messages,
            "stream": False,
        }
        if format == "json":
            payload["format"] = "json"
        # 显式禁用代理（使用 proxies={} 和 trust_env=False）
        async with httpx.AsyncClient(timeout=180, proxy=None, trust_env=False) as client:
            resp = await client.post(self._local_api_url, json=payload)
            print(f"📡 本地模型响应状态: {resp.status_code}")
            resp.raise_for_status()

            data = resp.json()
            content = data.get("message", {}).get("content", "")
            if not content.strip():
                logger.warning("⚠️  本地模型返回了空内容，这可能导致后续解析失败")
            return content

    async def _call_ollama_stream(self, messages: list[dict]):
        """
        流式调用 Ollama 原生 /api/chat 端点。
        """
        import httpx
        import json
        safe_messages = self._sanitize_messages_for_ollama(messages)
        payload = {
            "model": self._local_model,
            "messages": safe_messages,
            "stream": True,
        }
        # 使用 yield 来模拟 OpenAI 的流式输出结构
        # 显式禁用代理（使用 proxies={} 和 trust_env=False）
        async with httpx.AsyncClient(timeout=180, proxy=None, trust_env=False) as client:
            async with client.stream("POST", self._local_api_url, json=payload) as resp:

                if resp.status_code != 200:
                    body = await resp.aread()
                    print(f"📡 本地流式模型连接异常 (状态码: {resp.status_code}), 响应: {body.decode('utf-8', errors='replace')[:300]}")
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line: continue
                    data = json.loads(line)
                    if "message" in data:
                        content = data["message"].get("content", "")
                        # 构造一个 1:1 兼容 OpenAI chunk 的抽象对象（字典模拟）
                        # 这样 McpAgent 逻辑就不需要改。
                        yield type('obj', (object,), {
                            'choices': [type('obj', (object,), {
                                'delta': type('obj', (object,), {
                                    'content': content,
                                    'tool_calls': None,
                                    'role': 'assistant'
                                })
                            })]
                        })


    async def generate(self, tier: str, system_prompt: str, user_content: str, response_format: dict | None = None) -> str:
        """
        根据层级决定调用哪个模型，支持回退。
        """
        import time
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        # 动态决定优先级
        if not self.cloud_available and not self.local_available:
            raise Exception("未配置任何可用模型（云端或本地）")

        # 模式路由逻辑
        if not self.cloud_available:
            order = ["LOCAL"]
        elif not self.local_available:
            order = ["CLOUD"]
        elif self.agent_mode == "TURBO":
            order = ["CLOUD", "LOCAL"]  # TURBO 模式全线优先线上
        elif self.agent_mode == "SEQUENTIAL":
            if tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]  # SEQUENTIAL 模式下特定任务仍优先线上
            else:
                order = ["LOCAL", "CLOUD"]  # 其他任务优先本地
        else:  # AUTO 模式
            if tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]
            else:
                order = ["LOCAL", "CLOUD"]



        last_error = None
        print(f"DEBUG: Mode={self.agent_mode}, Tier={tier}, Order={order}")
        
        # [AOS 2.1] 进度心跳：每 10 秒提示一次，防止用户认为假死
        async def heartbeat():
            count = 1
            while True:
                await asyncio.sleep(10)
                print(f"⏳ ... 正在生成中 (已运行 {count*10}s) ...")
                count += 1
        for label in order:
            try:
                if label == "LOCAL":
                    if not self.local_available:
                        print("DEBUG: Local skipped (not available)")
                        continue
                    print(f"🏠 正在调用本地模型 ({self._local_model})...")
                    async with self._local_semaphore:
                        fmt = "json" if response_format and response_format.get("type") == "json_object" else None
                        
                        # 启动心跳
                        hb_task = asyncio.create_task(heartbeat())
                        try:
                            result = await self._call_ollama(messages, format=fmt)
                        finally:
                            hb_task.cancel()
                            
                        return result
                else:
                    if not self.cloud_available:
                        print("DEBUG: Cloud skipped (not available)")
                        continue
                    if self._cloud_circuit_broken_until > time.time():
                        print(f"❄️  [熔断] 云端模型目前不可用，跳过...")
                        continue

                    print(f"☁️ 正在调用云端模型 ({self.cloud_config['model']})...")
                    kwargs = {"model": self.cloud_config["model"], "messages": messages}
                    if response_format:
                        kwargs["response_format"] = response_format
                    
                    # 频率控制
                    await self.rate_limiter.wait()
                    
                    print(f"📡 正在向 API 发送请求 ({self.cloud_config['model']})...")
                    
                    # 启动心跳
                    hb_task = asyncio.create_task(heartbeat())
                    try:
                        response = await self.cloud_client.chat.completions.create(**kwargs)
                    finally:
                        hb_task.cancel()
                        
                    print(f"✅ API 响应已接收 ({self.cloud_config['model']})")
                    
                    # 成功则重置失败计数
                    self._consecutive_cloud_failures = 0
                    
                    # [AOS 2.5] 财务扣费拦截器
                    if self.economy:
                        usage = response.usage
                        self.economy.track_api_call(
                            usage.prompt_tokens, 
                            usage.completion_tokens,
                            is_local=False
                        )
                        
                    return response.choices[0].message.content
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                logger.warning(f"模型 {label}({self._local_model if label == 'LOCAL' else self.cloud_config['model']}) 调用失败: {error_msg}，正在尝试降级...")
                
                # [AOS 2.1] 更新熔断逻辑
                if label == "CLOUD":
                    self._consecutive_cloud_failures += 1
                    if self._consecutive_cloud_failures >= 2:
                        self._cloud_circuit_broken_until = time.time() + 180  # 熔断 3 分钟
                        logger.error("🚫 [熔断] 云端 API 连续失败 %d 次，已进入 3 分钟熔断期", self._consecutive_cloud_failures)
                
                last_error = error_msg
                continue
        
        raise Exception(f"所有算力层级均调用失败。最后一次错误: {last_error}")



    async def generate_stream(self, tier: str, messages: list[dict], tools: list[dict] | None = None):
        """
        流式生成，支持云端/本地降级（主要用于 Coordinator 或 Chat）。
        tier 决定优先级：LOCAL 优先本地，PREMIUM/LONG_CONTEXT 优先云端。
        """
        # 动态决定优先级
        if not self.cloud_available and not self.local_available:
            raise Exception("未配置任何可用流式模型")

        # 模式路由逻辑
        if not self.cloud_available:
            order = ["LOCAL"]
        elif not self.local_available:
            order = ["CLOUD"]
        elif self.agent_mode == "TURBO":
            order = ["CLOUD", "LOCAL"]
        elif self.agent_mode == "SEQUENTIAL":
            if tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]
            else:
                order = ["LOCAL", "CLOUD"]
        else:  # AUTO 模式
            if tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]
            else:
                order = ["LOCAL", "CLOUD"]




        last_error = None
        print(f"DEBUG: [Stream] Mode={self.agent_mode}, Tier={tier}, Order={order}")
        for label in order:
            try:
                if label == "LOCAL":
                    if not self.local_available:
                        print("DEBUG: [Stream] Local skipped")
                        continue
                    print(f"🏠 正在并行启动本地流式模型 ({self._local_model})...")
                    async with self._local_semaphore:
                        async for chunk in self._call_ollama_stream(messages):
                            yield chunk
                        return # 成功完成则退出
                else:
                    if not self.cloud_available:
                        print("DEBUG: [Stream] Cloud skipped")
                        continue
                    print(f"☁️ 正在并行启动云端流式模型 ({self.cloud_config['model']})...")
                    kwargs = {
                        "model": self.cloud_config["model"],
                        "messages": messages,
                        "stream": True,
                        "stream_options": {"include_usage": True}
                    }
                    if tools:
                        kwargs["tools"] = tools
                    
                    # 频率控制
                    await self.rate_limiter.wait()
                    
                    print(f"📡 正在建立云端流式连接 ({self.cloud_config['model']})...")
                    response = await self.cloud_client.chat.completions.create(**kwargs)
                    print(f"✨ 流式连接已建立，开始接收数据...")
                    full_content = ""
                    usage_found = False
                    async for chunk in response:
                        if chunk.choices and len(chunk.choices) > 0:
                            delta = chunk.choices[0].delta.content or ""
                            full_content += delta
                        yield chunk
                        
                        # [AOS 2.5] 捕获流式输出最后一个 chunk 里的 usage 数据
                        if hasattr(chunk, "usage") and chunk.usage:
                            usage_found = True
                            if self.economy:
                                self.economy.track_api_call(
                                    chunk.usage.prompt_tokens, 
                                    chunk.usage.completion_tokens,
                                    is_local=False
                                )
                    
                    # [AOS 2.5] 仅在未获得官方 usage 时进行保守估算
                    if not usage_found and self.economy:
                        try:
                            # 估算输入：messages 长度
                            input_text = json.dumps(messages, ensure_ascii=False)
                            in_tokens = len(input_text) // 2
                            out_tokens = len(full_content) // 2
                            self.economy.track_api_call(in_tokens, out_tokens, is_local=False)
                        except:
                            pass
                    return
            except (Exception, asyncio.CancelledError) as e:
                import traceback
                error_msg = f"{type(e).__name__}: {str(e)}"
                
                # [FEATURE] Friendly error for RateLimit (HTTP 429)
                if "429" in error_msg or "rate_limit" in error_msg.lower():
                    friendly_msg = "⚠️ [API 限流] 线上模型当前请求过多（429），正在尝试降级到本地或等待重试..."
                    logger.warning(friendly_msg)
                    # 如果是流式，我们可以直接尝试把这个友好的信息 yield 出去让用户看到
                    # 但在这里 yield 会破坏 current order 循环逻辑，我们还是通过 logger 记录
                
                logger.warning(f"流式模型 {label} 启动失败: {error_msg}，尝试降级...")
                last_error = error_msg
                continue
        
        raise Exception(f"所有流式算力层级均彻底不可用。最后一次错误: {last_error}")




import glob
from config import TOKEN_BUDGET as TOKEN_BUDGET_LIMIT
from skill_manager import SkillManager
from blackboard import Blackboard
from orchestrator import Orchestrator
from scheduler import Scheduler
from economy import EconomyEngine
from experience_engine import ExperienceEngine


class McpAgent:
    """
    AOS 2.0 自主操作系统 Agent
    支持动态技能挂载、黑板共享记忆、子专家召唤与 Token 预算控制。
    """

    def __init__(
        self,
        cloud_config: dict,
        local_config: dict,
        systemPrompt: str = "你是一个智能助手。",
        mode: str = "AUTO",
    ):
        # AOS 2.0: 黑板共享上下文
        self.blackboard = Blackboard()
        # AOS AEA: 经济与生存引擎 (CFO Agent)
        self.economy = EconomyEngine(blackboard=self.blackboard)
        # AOS 2.4: 进阶经验引擎
        self.exp_engine = ExperienceEngine()
        # AOS Phase 3: Cron 守护进程调度器
        self.scheduler = Scheduler()
        
        self.unified_client = UnifiedClient(cloud_config, local_config, agent_mode=mode, economy=self.economy)
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
        # Docker 沙盒代理
        self.docker_sandbox = DockerSandboxAgent()
        # AOS: 技能目录（Markdown 手册）
        self.skills_dir = os.path.join(os.path.dirname(__file__), ".agents", "skills")
        # AOS: Token 预算管理器
        self.token_budget = TokenBudget(max_tokens=TOKEN_BUDGET_LIMIT)
        # AOS 2.0: 动态技能管理器
        self.skill_manager = SkillManager()

        
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

        # AOS: 注册内部工具（技能系统、子专家）到 openai tool schema
        aos_tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_skills",
                    "description": "搜索本地技能库（.agents/skills/）以获取排错指南或方法论。当遇到 Docker 错误、代码分析等问题时使用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索关键词，如 'docker', '代码分析'"}
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_skill",
                    "description": "读取指定技能文件的完整内容。先用 search_skills 找到文件名，再用此工具读取。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "技能文件名，如 'docker_troubleshooting.md'"}
                        },
                        "required": ["name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "spawn_expert",
                    "description": "召唤无状态子专家来解决特定子任务。子专家独立工作，完成后立即销毁，不继承对话历史。适用于需要深度分析、安全审计等场景。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "description": "专家角色，如 '架构师', '安全审计员', 'Docker调试专家'"},
                            "task": {"type": "string", "description": "具体任务描述"},
                            "context_summary": {"type": "string", "description": "≤500字的背景信息摘要，禁止传递完整对话"}
                        },
                        "required": ["role", "task"]
                    }
                }
            },
        ]
        self.openaiTools.extend(aos_tools)
        toolNames.extend(["search_skills", "read_skill", "spawn_expert"])

        # AOS 2.0: 动态技能管理 + 黑板共享
        aos2_tools = [
            {
                "type": "function",
                "function": {
                    "name": "load_skill",
                    "description": "动态加载一个 MCP 技能服务。加载后该技能的所有工具将可用。使用前先用 list_skills 查看可用技能。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "技能名称，如 'sqlite_analyzer', 'browser'"}
                        },
                        "required": ["name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "unload_skill",
                    "description": "卸载已加载的 MCP 技能服务，释放系统资源。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "要卸载的技能名称"}
                        },
                        "required": ["name"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_skills",
                    "description": "列出所有可用的 MCP 技能及其加载状态。当你发现现有工具无法解决问题时使用。",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_blackboard",
                    "description": "向全局黑板写入客观事实，供其他专家和后续任务参考。写入重要发现（如端口号、技术栈、部署状态）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "事实标识，如 'service_port', 'tech_stack'"},
                            "value": {"type": "string", "description": "事实内容"},
                            "author": {"type": "string", "description": "写入者名称"}
                        },
                        "required": ["key", "value"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_blackboard",
                    "description": "读取全局黑板上所有共享事实。查看其他专家写入的发现和项目状态。",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
        ]
        self.openaiTools.extend(aos2_tools)
        toolNames.extend(["load_skill", "unload_skill", "list_skills", "write_blackboard", "read_blackboard"])

        # AOS Phase 3: 调度器 + 技能发现
        phase3_tools = [
            {
                "type": "function",
                "function": {
                    "name": "add_scheduled_task",
                    "description": "添加定时任务。支持每天定时(08:30)、周期执行(*/5=每5分钟)、cron表达式(0 8 * * *)。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "description": "任务唯一标识，如 'med_reminder'"},
                            "description": {"type": "string", "description": "任务描述"},
                            "cron_expr": {"type": "string", "description": "触发时间，如 '08:30' 或 '*/5' 或 '0 8 * * *'"},
                            "action": {"type": "string", "description": "动作类型: print/webhook/wechat"},
                            "payload": {"type": "string", "description": "消息内容或 URL"}
                        },
                        "required": ["task_id", "description", "cron_expr"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled_tasks",
                    "description": "列出所有定时任务及其下次触发时间。",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled_task",
                    "description": "取消指定的定时任务。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "description": "要取消的任务 ID"}
                        },
                        "required": ["task_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "discover_and_install_skill",
                    "description": "当现有工具无法解决问题时，自动从 GitHub 搜索、评分并安装最佳 MCP 技能。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索关键词，如 'sqlite database' 或 'browser automation'"}
                        },
                        "required": ["query"]
                    }
                }
            },
        ]
        self.openaiTools.extend(phase3_tools)
        toolNames.extend(["add_scheduled_task", "list_scheduled_tasks", "cancel_scheduled_task", "discover_and_install_skill"])

        # 启动后台调度器心跳
        self.scheduler.start()

        # AOS AEA: CFO 经济工具
        aea_tools = [
            {
                "type": "function",
                "function": {
                    "name": "cfo_report",
                    "description": "获取 CFO 财务简报：余额、燃烧率、剩余跑道天数、生存模式。",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "inject_funds",
                    "description": "向 Agent 钱包注资（充值），模拟收入或老板打款。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "amount": {"type": "number", "description": "注资金额 ($)"},
                            "description": {"type": "string", "description": "注资说明"}
                        },
                        "required": ["amount"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "cfo_approve",
                    "description": "让 CFO 评估一次云端 API 调用的 ROI，决定是否批准执行。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "estimated_cost": {"type": "number", "description": "预估消耗 ($)"},
                            "expected_value": {"type": "number", "description": "预期收益 ($)，无直接收益填 0"}
                        },
                        "required": ["estimated_cost"]
                    }
                }
            },
        ]
        self.openaiTools.extend(aea_tools)
        toolNames.extend(["cfo_report", "inject_funds", "cfo_approve"])

        # 同步经济指标到黑板
        for key, val in self.economy.get_blackboard_facts().items():
            self.blackboard.write(key, val, author="CFO")

        total_aos = len(aos_tools) + len(aos2_tools) + len(phase3_tools) + len(aea_tools)
        logger.info("已加载 %d 个工具 (MCP %d + AOS %d): %s", len(toolNames), len(mcpTools.tools), total_aos, toolNames)
        return toolNames

    # ========== AOS: 技能系统 ==========

    def search_skills(self, query: str) -> list[dict]:
        """
        搜索 .agents/skills/ 目录下的技能文件。
        基于文件名和内容关键词匹配（轻量级，无需向量化）。
        """
        results = []
        if not os.path.exists(self.skills_dir):
            return results
        for filepath in glob.glob(os.path.join(self.skills_dir, "*.md")):
            filename = os.path.basename(filepath)
            # 读取前 500 字符进行关键词匹配
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    preview = f.read(500)
            except Exception:
                preview = ""
            # 简单关键词匹配
            if query.lower() in filename.lower() or query.lower() in preview.lower():
                results.append({"name": filename, "preview": preview[:200]})
        return results

    def read_skill(self, name: str) -> str:
        """读取指定技能文件的完整内容"""
        filepath = os.path.join(self.skills_dir, name)
        if not os.path.exists(filepath):
            # 尝试补全 .md 后缀
            filepath = os.path.join(self.skills_dir, f"{name}.md")
        if not os.path.exists(filepath):
            return f"技能文件未找到: {name}"
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    # ========== AOS: 子专家工厂 ==========

    async def spawn_expert(self, role: str, task_description: str, context_summary: str = "") -> str:
        """
        召唤无状态子专家：独立完成一个细分任务后立即销毁。
        上下文修剪：context_summary 必须 ≤500 字，禁止传递完整对话栈。
        """
        # 强制上下文修剪
        if len(context_summary) > 500:
            context_summary = context_summary[:500] + "...(已截断)"
            logger.warning("⚠️ 子专家上下文已被强制修剪至 500 字符")

        expert_prompt = f"""你是一个 {role} 专家。你的任务是独立完成以下工作，直接输出结果，不需要额外对话。

【背景信息】
{context_summary}

【任务】
{task_description}"""

        print(f"🧬 [AOS] 正在召唤子专家: {role}...")
        try:
            result = await self.unified_client.generate(
                tier="PREMIUM",
                system_prompt=f"你是一个专注的 {role} 专家。简洁、精准地完成指定任务。",
                user_content=expert_prompt,
            )
            # 跟踪 Token 消耗
            self.token_budget.consume(self.token_budget.estimate_tokens(result))
            print(f"✅ [AOS] 子专家 [{role}] 已完成任务并销毁")
            return result
        except Exception as e:
            logger.error("子专家 [%s] 执行失败: %s", role, e)
            return f"子专家执行失败: {e}"

    # ========== AOS: 内部工具调度器 ==========

    async def _handle_internal_tool(self, func_name: str, arguments: dict) -> str | None:
        """
        处理 AOS 内部工具调用（非 MCP 工具）。
        返回 None 表示不是内部工具，应交由 MCP 或 SkillManager 处理。
        """
        # AOS 1.0: 技能手册
        if func_name == "search_skills":
            query = arguments.get("query", "")
            results = self.search_skills(query)
            if results:
                return "\n".join([f"📚 {r['name']}: {r['preview']}" for r in results])
            return "未找到匹配的技能文件"

        elif func_name == "read_skill":
            name = arguments.get("name", "")
            return self.read_skill(name)

        elif func_name == "spawn_expert":
            role = arguments.get("role", "通用")
            task = arguments.get("task", "")
            context = arguments.get("context_summary", "")
            # AOS 2.0: 自动注入黑板快照作为额外上下文
            bb_context = self.blackboard.read_all()
            if bb_context and "黑板为空" not in bb_context:
                context = f"{context}\n\n{bb_context}"
            return await self.spawn_expert(role, task, context)

        # AOS 2.0: 动态技能管理
        elif func_name == "load_skill":
            name = arguments.get("name", "")
            result = await self.skill_manager.load_skill(name)
            return json.dumps(result, ensure_ascii=False)

        elif func_name == "unload_skill":
            name = arguments.get("name", "")
            result = await self.skill_manager.unload_skill(name)
            return json.dumps(result, ensure_ascii=False)

        elif func_name == "list_skills":
            skills = self.skill_manager.list_available()
            return json.dumps(skills, ensure_ascii=False, indent=2)

        # AOS 2.0: 黑板读写
        elif func_name == "write_blackboard":
            key = arguments.get("key", "")
            value = arguments.get("value", "")
            author = arguments.get("author", "Agent")
            self.blackboard.write(key, value, author)
            return f"已写入黑板: {key} = {value}"

        elif func_name == "read_blackboard":
            return self.blackboard.read_all()

        # AOS Phase 3: 定时任务调度
        elif func_name == "add_scheduled_task":
            result = self.scheduler.add_task(
                task_id=arguments.get("task_id", ""),
                description=arguments.get("description", ""),
                cron_expr=arguments.get("cron_expr", ""),
                action=arguments.get("action", "print"),
                payload=arguments.get("payload", ""),
            )
            return json.dumps(result, ensure_ascii=False)

        elif func_name == "list_scheduled_tasks":
            tasks = self.scheduler.list_tasks()
            return json.dumps(tasks, ensure_ascii=False, indent=2)

        elif func_name == "cancel_scheduled_task":
            result = self.scheduler.cancel_task(arguments.get("task_id", ""))
            return json.dumps(result, ensure_ascii=False)

        # AOS Phase 3: 技能自动发现与安装
        elif func_name == "discover_and_install_skill":
            query = arguments.get("query", "")
            result = await self.skill_manager.auto_install(query, session=self.session)
            return json.dumps(result, ensure_ascii=False)

        # AOS AEA: CFO 经济工具
        elif func_name == "cfo_report":
            return self.economy.get_financial_report()

        elif func_name == "inject_funds":
            amount = arguments.get("amount", 0)
            desc = arguments.get("description", "收入")
            self.economy.earn(amount, desc)
            # 同步到黑板
            for key, val in self.economy.get_blackboard_facts().items():
                self.blackboard.write(key, val, author="CFO")
            return self.economy.get_financial_report()

        elif func_name == "cfo_approve":
            cost = arguments.get("estimated_cost", 0)
            value = arguments.get("expected_value", 0)
            result = self.economy.should_approve_cloud_call(cost, value)
            return json.dumps(result, ensure_ascii=False)

        return None  # 非内部工具

    def _get_combined_tools(self) -> list[dict]:
        """获取静态工具与动态技能工具的合集 (AOS 2.3)"""
        all_tools = list(self.openaiTools) if self.openaiTools else []
        skill_tools = self.skill_manager.get_all_tools()
        if skill_tools:
            # 过滤重复项（按名称）
            existing_names = set(t["function"]["name"] for t in all_tools)
            for st in skill_tools:
                if st["function"]["name"] not in existing_names:
                    all_tools.append(st)
        return all_tools if all_tools else None

    async def chat(self, userInput: str, tier: str = "LOCAL") -> AsyncGenerator[str, None]:
        """
        AOS 核心 ReAct 循环（流式版）。
        支持目标驱动、自动反思、技能加载、子专家召唤与 Token 预算控制。
        """
        if "main" not in self.memories:
            self.memories["main"] = [{"role": "system", "content": self.systemPrompt}]

        self.memories["main"].append({"role": "user", "content": userInput})
        # AOS: 每次新的用户交互重置 Token 预算
        self.token_budget.reset()

        MAX_ITERATIONS = 50
        call_history = []  # 死循环检测
        for iteration in range(MAX_ITERATIONS):
            # Token 预算墙检查
            if self.token_budget.exceeded:
                yield f"\n🚨 [预算墙] 本次任务已消耗约 {self.token_budget.consumed} Tokens，超过预算上限 {self.token_budget.max_tokens}。任务已暂停，请确认是否继续。"
                return

            # 动态截断上下文，防止超长报错 (如 DeepSeek 128k 限制)
            self._truncate_memory("main")
            
            kwargs: dict = {
                "messages": self.memories["main"],
            }
            # tier 由外部传入，决定本地/云端优先级

            
            fullContent = ""
            toolCallsDict = {}  # 用于按索引累计 tool_calls 数据

            # AOS 2.3: 动态合并当前已加载的所有工具（含动态技能）
            current_tools = self._get_combined_tools()
            
            response = self.unified_client.generate_stream(
                tier=tier,
                messages=self.memories["main"],
                tools=current_tools
            )


            async for chunk in response:
                # [DEFENSIVE] NVIDIA or other APIs might return empty chunks or chunks without choices
                if not chunk or not hasattr(chunk, "choices") or not chunk.choices:
                    continue
                
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
                # 跟踪 Token 消耗
                self.token_budget.consume(self.token_budget.estimate_tokens(fullContent))
                return

            # 执行工具调用
            for tc in assistantMsg["tool_calls"]:
                funcName = tc["function"]["name"]
                arguments = tc["function"]["arguments"]  # 已经是字符串

                # 死循环检测：如果完全一致的 (函数名, 参数) 连续出现 3 次，判定为死循环
                call_sig = f"{funcName}:{arguments}"
                call_history.append(call_sig)
                if len(call_history) >= 2 and call_history.count(call_sig) >= 2:
                    yield "⚠️ [系统提示] 检测到模型正在反复执行相同的工具调用，已强制中断。请尝试换个方式描述您的需求，或者检查搜索关键词是否过于冷门。"
                    return

                logger.info("调用工具: %s(%s)", funcName, arguments[:200])

                try:
                    parsed_args = json.loads(arguments)

                    # AOS: 优先尝试内部工具（技能、子专家、黑板）
                    internal_result = await self._handle_internal_tool(funcName, parsed_args)
                    if internal_result is not None:
                        resultText = internal_result
                    else:
                        # AOS 2.0: 尝试从动态加载的技能中调用
                        skill_result = await self.skill_manager.call_tool(funcName, parsed_args)
                        if skill_result is not None:
                            resultText = skill_result
                        else:
                            # 最终回退：主 MCP 会话
                            result = await self.session.call_tool(funcName, arguments=parsed_args)
                            texts = []
                            for item in result.content:
                                if hasattr(item, "text"):
                                    texts.append(item.text)
                                elif isinstance(item, dict) and "text" in item:
                                    texts.append(item["text"])
                                else:
                                    texts.append(str(item))
                            resultText = "\n".join(texts)

                    # 对工具输出进行长度保护
                    MAX_TOOL_OUTPUT = 30000
                    if len(resultText) > MAX_TOOL_OUTPUT:
                        resultText = resultText[:MAX_TOOL_OUTPUT] + f"\n\n...(输出过长，已截断前 {MAX_TOOL_OUTPUT} 字符数据)"

                    # 跟踪 Token 消耗
                    self.token_budget.consume(self.token_budget.estimate_tokens(resultText))

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

            # AOS: 每 10 轮强制自我反思，防止无效循环
            if iteration > 0 and iteration % 10 == 0:
                reflection_msg = f"[系统提示] 你已经执行了 {iteration} 轮工具调用。请反思：当前目标是否接近达成？是否需要改变策略？如果陷入困境，考虑使用 search_skills 查找相关技能或 spawn_expert 召唤子专家。Token 预算剩余: {self.token_budget.remaining}"
                self.memories["main"].append({"role": "user", "content": reflection_msg})
                yield f"\n💭 [自我反思 - 第 {iteration} 轮] 检查进度与策略...\n"

        yield f"已达到最大工具调用次数 ({MAX_ITERATIONS})。Token 消耗: {self.token_budget.consumed}。请简化您的请求。"


    def _truncate_memory(self, context_id: str, max_msgs: int = 30, max_chars: int = 100000):
        """
        截断记忆历史，并确保消息序列完整性（不产生孤儿 tool 消息）。
        采用“块”概念：assistant 及其随后的 tool 消息必须作为一个整体保留或丢弃。
        """
        if context_id not in self.memories or not self.memories[context_id]:
            return
            
        history = self.memories[context_id]
        if len(history) <= 1: 
            return
            
        system_msg = history[0] if history[0]["role"] == "system" else None
        msgs_to_check = history[1:] if system_msg else history
        
        keep = []
        current_len = 0
        
        # 从新到旧处理
        temp_msgs = list(reversed(msgs_to_check))
        i = 0
        while i < len(temp_msgs):
            msg = temp_msgs[i]
            group = []
            
            # 如果是 tool 消息，尝试寻找它的源头 assistant 消息并捆绑
            if msg.get("role") == "tool":
                j = i
                while j < len(temp_msgs) and temp_msgs[j].get("role") == "tool":
                    j += 1
                if j < len(temp_msgs) and temp_msgs[j].get("role") == "assistant":
                    group = temp_msgs[i : j + 1] # [tool, tool, ..., assistant]
                    i = j + 1
                else:
                    # 孤儿 tool 消息，丢弃
                    i += 1
                    continue
            else:
                group = [msg]
                i += 1
                
            # 计算该组长度
            group_text = "".join([str(m.get("content", "") or "") for m in group])
            group_len = len(group_text)
            
            if current_len + group_len > max_chars or len(keep) + len(group) > max_msgs:
                break
                
            keep.extend(group)
            current_len += group_len
            
        # 恢复顺序
        truncated = list(reversed(keep))
        
        # 如果什么都没剩下（比如单条消息就爆了），保底留最后一条并强行截断部分文字
        if not truncated and msgs_to_check:
             last_msg = msgs_to_check[-1]
             content = str(last_msg.get("content", "") or "")
             truncated = [{**last_msg, "content": content[:max_chars] + "\n...(内容过长强制截断)"}]

        self.memories[context_id] = ([system_msg] + truncated) if system_msg else truncated

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

    async def saveAllMemories(self):
        """保存所有记忆、清理沙盒、安全卸载技能、停止调度器"""
        for context_id in list(self.memories.keys()):
            self.saveMemory(context_id)
        # AOS Phase 3: 停止后台调度器
        if hasattr(self, "scheduler"):
            await self.scheduler.stop()
        # AOS 2.0: 安全卸载所有动态技能（防僵尸进程）
        if hasattr(self, "skill_manager"):
            await self.skill_manager.unload_all()
        # 退出清理 Docker 沙盒
        if hasattr(self, "docker_sandbox"):
            self.docker_sandbox.cleanup_all()
        logger.info("💾 所有 Agent 记忆已持久化到 %s/", self.memory_dir)

    # ========== AOS 2.0: 自治编排引擎 ==========

    async def autonomous_execute(self, user_demand: str) -> AsyncGenerator[str, None]:
        """
        /auto 命令入口：启动全自治任务循环。
        动态招聘子 Agent -> 黑板协作 -> AI 裁判验收 -> 多轮自愈。
        """
        orchestrator = Orchestrator(
            unified_client=self.unified_client,
            skill_manager=self.skill_manager,
            blackboard=self.blackboard,
            agent=self, # AOS 2.1: 传递当前 Agent 实例以便执行工具调用
            exp_engine=self.exp_engine, # AOS 2.4+: 共享经验引擎实例
        )
        async for chunk in orchestrator.run_mission(
            user_demand=user_demand,
            primary_session=self.session,
            max_rounds=3,
        ):
            yield chunk

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_content: str,
        tier: str = "PREMIUM",
        context_id: str = "internal_task"
    ) -> str:
        """
        AOS 核心 ReAct 循环（非流式交互版）。
        适用于子专家、Orchestrator 子任务等需要自主执行工具的场景。
        @returns 最终的任务输出摘要
        """
        # 初始化或恢复针对该任务的记忆
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        self.memories[context_id] = messages
        
        # 针对该任务重置预算
        self.token_budget.reset()
        
        MAX_ITERATIONS = 15 # 子任务通常较短
        call_history = []
        
        for iteration in range(MAX_ITERATIONS):
            if self.token_budget.exceeded:
                return f"🚨 [预算限制] 任务已停止 (消耗 {self.token_budget.consumed} tokens)"

            self._truncate_memory(context_id)
            
            fullContent = ""
            toolCallsDict = {}
            
            # AOS 2.3: 动态合并当前已加载的所有工具（保证子 Agent 能看见 filesystem 等工具）
            current_tools = self._get_combined_tools()
            
            # 使用流式生成并累积结果（复用流量控制与模型降级逻辑）
            response_stream = self.unified_client.generate_stream(
                tier=tier,
                messages=self.memories[context_id],
                tools=current_tools
            )
            
            async for chunk in response_stream:
                if not chunk or not hasattr(chunk, "choices") or not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    fullContent += delta.content
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in toolCallsDict:
                            toolCallsDict[idx] = {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id: toolCallsDict[idx]["id"] = tc.id
                        if tc.function.name: toolCallsDict[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments: toolCallsDict[idx]["function"]["arguments"] += tc.function.arguments

            # 保存对话记录
            assistantMsg = {"role": "assistant", "content": fullContent or None}
            if toolCallsDict:
                assistantMsg["tool_calls"] = list(toolCallsDict.values())
            self.memories[context_id].append(assistantMsg)
            
            # 如果没有工具调用，说明任务执行到阶段性终点
            if not toolCallsDict:
                self.token_budget.consume(self.token_budget.estimate_tokens(fullContent))
                return fullContent
                
            # 执行工具调用 (ReAct Action)
            for tc in assistantMsg["tool_calls"]:
                funcName = tc["function"]["name"]
                arguments = tc["function"]["arguments"]
                
                # 死循环防护
                call_sig = f"{funcName}:{arguments}"
                call_history.append(call_sig)
                if call_history.count(call_sig) >= 3:
                    return f"⚠️ [死循环中断] 任务已停止以防无限循环调用 {funcName}"

                logger.info("[%s] 正在执行子任务工具: %s", context_id, funcName)
                
                try:
                    parsed_args = json.loads(arguments)
                    # 按照优先级处理工具：内部 -> 技能 -> MCP
                    resultText = await self._handle_internal_tool(funcName, parsed_args)
                    if resultText is None:
                        resultText = await self.skill_manager.call_tool(funcName, parsed_args)
                    if resultText is None:
                        result = await self.session.call_tool(funcName, arguments=parsed_args)
                        resultText = "\n".join([
                            (item.text if hasattr(item, "text") else str(item))
                            for item in result.content
                        ])
                    
                    # 保护处理
                    if len(resultText) > 20000:
                        resultText = resultText[:20000] + "...(数据截断)"
                    self.token_budget.consume(self.token_budget.estimate_tokens(resultText))
                    
                except Exception as e:
                    logger.error("子任务工具调用失败: %s - %s", funcName, e)
                    resultText = f"工具执行错误: {str(e)}"

                self.memories[context_id].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": resultText,
                })
        
        return fullContent # 到达最大迭代次数时的返回结果

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
                        # 主动执行一次截断与校验，确保历史记忆序列合法
                        self._truncate_memory(context_id)
                        logger.info("🧠 已从文件恢复 [%s] 的历史记忆并完成序列校验", context_id)
                        return self.memories[context_id]
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
            # 1. 扫描文件 tree (尝试递归获取更多目录信息，帮助模型判断代码分布)
            tree = await self.session.call_tool("search_repositories", arguments={
                "query": f"repo:{owner}/{repo} path:/",
            })
            # 如果 search 不太好用，由于 MCP 工具限制，我们至少在 adaptive_load_data 中明确告知 LLM 根目录结构
            tree_content = str(tree.content)
            
            # 如果 search_repositories 返回不理想，回退到原来的 list
            if "total_count" not in tree_content or "items" not in tree_content:
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
                content = str(r.content)
                if len(content) > 15000: # 限制单文件加载大小
                    content = content[:15000] + "\n...(文件内容过大，已截断前 15000 字符)"
                source_context += f"--- 文件内容: {files_to_read[i]} ---\n{content}\n\n"
            
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
            
            return {expertName: extract_json(resultText)}
        except Exception as e:
            logger.error("专家 %s 评审失败: %s", expertName, e)
            return {expertName: {"error": str(e)}}

    async def multiAgentReview(self, repo_url: str) -> AsyncGenerator[str, None]:
        """
        混合多 Agent 评审流：并发调度、算力路由、自适应加载。
        """
        print(f"\n🚀 [Hybrid Mode: {self.mode}] 启动 OpenClaw 多专家联合评审团...")
        
        # 1. 自适应加载数据 (基础扫描)
        project_data = await self.adaptive_load_data(repo_url)
        
        # 2. 调度专家评审 (AUTO/TURBO 模式并发)
        experts = list(EXPERT_REGISTRY.keys())
        if "Deployment_Executor" in experts:
            # 评审时不包含部署专家，它只在 /deploy 时显式调用
            experts.remove("Deployment_Executor")
            
        tasks = []
        for name in experts:
            tasks.append(self.expertReview(name, EXPERT_REGISTRY[name], project_data))
            
        print(f"🕵️  正在调度 {len(tasks)} 位专家进行综合评审...")
        results = await asyncio.gather(*tasks)
        
        # 3. 汇总报告 (Coordinator)
        reviews_summary = "\n\n".join([
            f"--- {r.get('dimension', '专家')} (得分: {r.get('score', '?')}) ---\n"
            f"观察: {', '.join(r.get('key_observations', []))}\n"
            f"总结: {r.get('summary', '')}" 
            for r in results if r
        ])
        
        system_prompt = COORDINATOR_SYSTEM_PROMPT.format(expert_reviews=reviews_summary)
        user_input = f"请为项目 {repo_url} 生成最终的专家团综合评审报告。"
        
        async for chunk in self.unified_client.generate_stream("PREMIUM", [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ]):
            # [DEFENSIVE] Fix for list index out of range in streaming responses
            if not chunk or not hasattr(chunk, "choices") or not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = delta.content if hasattr(delta, "content") else ""
            if content:
                yield content

    async def deploy_project(self, repo_url: str) -> AsyncGenerator[str, None]:
        """
        AOS 增强型一键部署：支持 5 次自愈、状态回滚、健康探针。
        """
        owner, repo = self._parse_github_url(repo_url)
        if not owner:
            yield f"❌ 无效的 GitHub URL: {repo_url}"
            return

        yield f"🔍 正在初始化 [{repo}] 的部署流程...\n"

        # 1. 加载部署上下文
        yield "🏁 [里程碑] 正在扫描项目文件以提取技术栈...\n"
        context_data = await self.adaptive_load_data(repo_url, expert_config=EXPERT_REGISTRY["Deployment_Executor"])

        # 2. 迭代尝试部署（含自愈与状态回滚）
        MAX_RETRIES = 5
        last_error_log = ""
        dockerfile_content = ""
        # AOS: 状态回滚 - 保存每次 Dockerfile 快照
        dockerfile_history: list[str] = []

        for attempt in range(1, MAX_RETRIES + 1):
            retry_prefix = f"【第 {attempt}/{MAX_RETRIES} 次尝试】" if attempt > 1 else ""

            # 生成/修正 Dockerfile
            if attempt == 1:
                yield f"🤖 {retry_prefix}部署专家正在编写专属 Dockerfile...\n"
                prompt_input = f"请为以下项目数据生成最优 Dockerfile：\n\n{context_data}"
            elif attempt <= 3:
                yield f"🔧 {retry_prefix}正在自愈：根据错误日志分析并修正 Dockerfile...\n"
                prompt_input = f"上一次构建失败了。\n\n【错误日志】:\n{last_error_log}\n\n【之前生成的 Dockerfile】:\n{dockerfile_content}\n\n请针对上述错误进行分析并输出修复后的 Dockerfile。"
            else:
                # AOS: 第 4-5 次尝试使用"部署调试器"子专家 + 尝试回滚到历史最优版本
                yield f"🧬 {retry_prefix}启用部署调试专家进行深度诊断...\n"
                # 尝试回滚到第一个版本（往往是最干净的）
                rollback_df = dockerfile_history[0] if dockerfile_history else dockerfile_content
                debug_context = f"Dockerfile 版本历史数量: {len(dockerfile_history)}。最近错误: {last_error_log[:300]}"
                expert_analysis = await self.spawn_expert(
                    "Docker 部署调试专家",
                    f"分析以下构建错误并修复 Dockerfile:\n\n【错误日志】:\n{last_error_log}\n\n【原始 Dockerfile】:\n{rollback_df}\n\n请直接输出完整的修复后 Dockerfile，不要包含 Markdown。",
                    debug_context
                )
                prompt_input = None
                dockerfile_content = expert_analysis

            if prompt_input:
                dockerfile_content = await self.unified_client.generate(
                    "PREMIUM",
                    EXPERT_REGISTRY["Deployment_Executor"]["prompt"],
                    prompt_input
                )

            # 剥离可能的 markdown 标记
            if "```" in dockerfile_content:
                match = re.search(r"```(?:dockerfile)?\s*(.*?)\s*```", dockerfile_content, re.DOTALL)
                if match:
                    dockerfile_content = match.group(1)

            # AOS: 保存 Dockerfile 快照用于回滚
            dockerfile_history.append(dockerfile_content)

            # 执行沙盒部署
            yield f"🐳 {retry_prefix}正在启动本地 Docker 沙盒进行构建与部署...\n"

            current_attempt_failed = False
            try:
                for event in self.docker_sandbox.deploy_in_sandbox(repo, dockerfile_content, repo_url):
                    evt_type = event.get("type")
                    msg = event.get("message", "")

                    if evt_type == "progress":
                        yield f"  {msg}\n"
                    elif evt_type == "log":
                        if msg:
                            yield f"    ┃ {msg}\n"
                    elif evt_type == "success":
                        ports_str = " | ".join(event.get("ports", []))
                        container_id = event['container_id']

                        yield f"\n🏁 [里程碑] 容器启动成功，正在执行健康检查...\n"

                        # AOS: 健康探针
                        import time
                        time.sleep(3)  # 等待服务启动
                        health = self.docker_sandbox.check_health(container_id)

                        self.memories["main"].append({
                            "role": "assistant",
                            "content": f"【系统通知】项目已成功部署。映射端口：{ports_str} (容器ID: {container_id})"
                        })
                        yield f"\n✅ **部署成功！**\n"
                        yield f"- **尝试次数**: {attempt}\n"
                        yield f"- **端口映射**: `{ports_str}`\n"
                        yield f"- **容器 ID**: `{container_id}`\n"

                        # 健康检查结果
                        if health.get("healthy"):
                            yield f"- **健康状态**: 🟢 正常\n"
                        else:
                            yield f"- **健康状态**: 🟡 服务可能未完全就绪\n"
                        for probe in health.get("probes", []):
                            status = probe.get("status_code", "N/A")
                            yield f"  - 端口 {probe.get('port')}: HTTP {status}\n"
                            if probe.get("diagnosis"):
                                yield f"    ⚠️ {probe['diagnosis']}\n"

                        if event.get("logs"):
                            yield f"\n📋 **启动日志摘要 (前10行)**:\n```\n{event['logs']}\n```\n"

                        yield "💡 温馨提示：容器仅在 Agent 运行时存活，关闭程序或使用 /clear 将自动销毁。\n\n"
                        return
                    elif evt_type == "error":
                        yield f"  ❌ 构建/部署失败: {msg}\n"
                        last_error_log = event.get("details", msg)
                        current_attempt_failed = True
                        break

                if current_attempt_failed:
                    continue
                else:
                    return

            except Exception as e:
                yield f"  ⚠️ 部署执行异常: {str(e)}\n"
                last_error_log = str(e)
                if attempt == MAX_RETRIES:
                    raise e

        # AOS: 熔断 - 所有重试均失败
        yield f"\n🚨 [熔断] 自愈失败。已尝试 {MAX_RETRIES} 种方案，请人类接管。\n"
        yield f"最后一次错误详情:\n```\n{last_error_log}\n```\n"

