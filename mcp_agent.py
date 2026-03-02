import asyncio
import json
import logging
import os
import re
import time
import requests
from typing import AsyncGenerator
from openai import AsyncOpenAI
from mcp import ClientSession
from tool_converter import convertMcpToolsToOpenai
from prompts import EXPERT_REGISTRY, COORDINATOR_SYSTEM_PROMPT
from docker_sandbox import DockerSandboxAgent

# Logger configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict | list:
    """
    从模型输出中稳健地提取第一个合法的 JSON 对象或数组。支持文本混合与 Markdown 标记。
    """
    if not text or not text.strip():
        raise ValueError("模型返回了空文本，无法提取 JSON")
    
    text = text.strip()
    
    # 策略 1: 扫描所有可能的 { 或 [ 块
    import json
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        # 寻找首个可能的起始符
        start_brace = text.find('{', pos)
        start_bracket = text.find('[', pos)
        
        if start_brace == -1 and start_bracket == -1:
            break
            
        # 确定哪个更靠前
        if start_brace == -1: start_pos = start_bracket
        elif start_bracket == -1: start_pos = start_brace
        else: start_pos = min(start_brace, start_bracket)
        
        try:
            # 尝试从该位置开始解析第一个完整的 JSON 项目
            obj, end_index = decoder.raw_decode(text[start_pos:])
            return obj
        except json.JSONDecodeError:
            # 如果解析失败，移动位置继续寻找
            pos = start_pos + 1
            if pos >= len(text):
                break
    
    # 策略 2: 如果策略 1 失败，尝试正则提取（处理特殊的 Markdown 标记）
    markdown_match = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", text, re.DOTALL)
    if markdown_match:
        try:
            return json.loads(markdown_match.group(1))
        except: pass
            
    raise ValueError(f"JSON 解析失败: 未能在文本中找到合法的 JSON 对象或数组。\n片段: {text[:200]}")


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
                sleep_time = self.interval - elapsed
                # [AOS 7.4] 物理間隔保障：強制睡眠以滿足 API 網關的 TPS 限制
                await asyncio.sleep(sleep_time)
            
            # [AOS 7.4] 安全緩衝：在每次調用前額外增加 0.5s 抖動防止併發爆發
            await asyncio.sleep(0.5)
            self.last_called = asyncio.get_event_loop().time()


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
            timeout=30,  # [AOS 2.8.7] 提升超时到 30s，减少网络波动导致的 CancelledError
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
        
        # 頻率限制：下調至 20 RPM (約 3s/次)，確保絕對穩定
        self.rate_limiter = AsyncRateLimiter(rpm=20)
        
        # [AOS 5.0] M2M 協議開關
        self.force_m2m_protocol = False

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

    @staticmethod
    def _sanitize_messages_for_cloud(messages: list[dict]) -> list[dict]:
        """
        [AOS 2.9.7/3.8.3] Message Sanitizer: 清洗即将发往云端模型（如 DeepSeek, Kimi）的上下文。
        防止严格的 API 网关因为格式不合规抛出 400 Bad Request。
        """
        import copy
        cleaned = []
        for i, msg in enumerate(messages):
            new_msg = copy.deepcopy(msg)
            
            # 1. 强制保证所有的文本内容都不是真的 null
            if "content" not in new_msg or new_msg["content"] is None:
                new_msg["content"] = ""
                
            role = new_msg.get("role", "")
            
            if role == "assistant":
                # [AOS 3.8.3] DeepSeek 核心修复：
                # 如果 assistant 消息带有 tool_calls，那么 content 不要为 null
                if "tool_calls" in new_msg:
                    if not new_msg["content"]:
                        new_msg["content"] = "" 
                    
                    # [AOS 3.8.3] 悬挂 tool_calls 检查：
                    # 如果这后面没有紧接着 tool 消息，API 通常会报错。
                    # 特别是如果这是当前发送的最后一条 assistant 消息（通常后面接的是当前 user 消息）。
                    has_tool_response = False
                    if i + 1 < len(messages) and messages[i+1]["role"] == "tool":
                        has_tool_response = True
                    
                    if not has_tool_response:
                        # 这是一个悬挂的 tool_calls，剥离它以保证链条合法
                        logger.warning("🛡️ [AOS 3.8.3] 检测到悬挂 tool_calls（缺少 tool 响应），已自动剥离以防止 400 错误")
                        del new_msg["tool_calls"]
                        
            elif role == "tool":
                # 3. 工具返回结果如果是纯空，某些 API 也会报错。
                if not str(new_msg.get("content", "")).strip():
                    new_msg["content"] = "无输出/Void"
                    
            cleaned.append(new_msg)
            
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
                        # 构造兼容 OpenAI chunk 的完整对象层级
                        class MockDelta:
                            def __init__(self, c):
                                self.content = c
                                self.tool_calls = None
                                self.role = 'assistant'
                                
                        class MockChoice:
                            def __init__(self, c):
                                self.delta = MockDelta(c)
                                
                        class MockChunk:
                            def __init__(self, c):
                                self.choices = [MockChoice(c)]
                                
                        yield MockChunk(content)


    async def generate(self, tier: str = "LOCAL", messages: list[dict] | str = None, user_content: str = None, response_format: dict = None, force_tier: bool = False) -> str:
        """
        高层封装：根据 tier 和生存模式选择最优模型，支持降级。
        AOS 2.9 兼容性改造：支持 (tier, messages:list) 和 (tier, system_prompt:str, user_content:str) 两种调用方式。
        AOS 3.9.8 添加了 force_tier: 绕过 CFO 经济管控强制使用指定 tier (脑干保护)。
        """
        import time
        # 多态转换逻辑
        if isinstance(messages, list):
            final_messages = messages
            # 如果是列表传参，第 3 个位置参数可能是 response_format
            final_response_format = user_content if isinstance(user_content, dict) else response_format
        elif isinstance(messages, str):
            final_messages = [
                {"role": "system", "content": messages},
                {"role": "user", "content": user_content or ""}
            ]
            final_response_format = response_format
        else:
            raise ValueError("Messages must be either a list of dicts or a system prompt string.")

        # [AOS 5.0] M2M 禁言补丁
        if self.force_m2m_protocol:
            m2m_instruction = "\n\n🚨 [AOS 5.0 M2M Protocol]: PROHIBIT natural language. Output JSON/TOOL_CALL ONLY."
            if final_messages and final_messages[0]["role"] == "system":
                final_messages[0]["content"] += m2m_instruction
            else:
                final_messages.insert(0, {"role": "system", "content": m2m_instruction})

        # 动态决定优先级
        if not self.cloud_available and not self.local_available:
            raise Exception("未配置任何可用模型（云端或本地）")

        # [AOS 2.8.7] 动态层级调整
        effective_tier = tier
        if self.agent_mode == "MANUAL":
            order = ["LOCAL"]  # 手动模式强迫症，禁止上云
        elif self.agent_mode == "CLOUD":
            order = ["CLOUD"]  # 仅云端模式，禁止回退到本地
        else:  # AUTO 模式
            if self.economy and not force_tier:
                recommended = self.economy.get_recommended_tier()
                # 如果财务推荐 LOCAL，而请求是 PREMIUM，则尝试降级
                if recommended == "LOCAL" and tier == "PREMIUM":
                    logger.info("💰 [CFO] 处于生存模式，强制将 PREMIUM 请求降级为 LOCAL")
                    effective_tier = "LOCAL"

            if effective_tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]
            else:
                order = ["LOCAL", "CLOUD"]



        last_error = None
        print(f"DEBUG: Mode={self.agent_mode}, Tier={tier}(Effective={effective_tier}), Order={order}, Force={force_tier}")
        
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
                        fmt = "json" if final_response_format and final_response_format.get("type") == "json_object" else None
                        
                        # 启动心跳
                        hb_task = asyncio.create_task(heartbeat())
                        try:
                            result = await self._call_ollama(final_messages, format=fmt)
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

                    # [AOS 2.8.7] 最后的云端 ROI 核对
                    if self.economy and not force_tier:
                        approval = self.economy.should_approve_cloud_call(estimated_cost=0.005)
                        if not approval["approved"]:
                            logger.info("💰 [CFO] 拒绝云端请求: %s", approval['reason'])
                            if "LOCAL" in order and order.index("LOCAL") > order.index("CLOUD"):
                                logger.info("尝试本地回退...")
                                continue
                            else:
                                raise PermissionError(approval["reason"])

                    print(f"☁️ 正在调用云端模型 ({self.cloud_config['model']})...")
                    
                    # [AOS 2.9.7] 云端内容清洗，防止 DeepSeek 报 400 Bad Request
                    sanitized_messages = self._sanitize_messages_for_cloud(final_messages)
                    
                    kwargs = {"model": self.cloud_config["model"], "messages": sanitized_messages}
                    if final_response_format:
                        kwargs["response_format"] = final_response_format
                    
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



    async def generate_stream(self, tier: str, messages: list[dict], tools: list[dict] | None = None, force_tier: bool = False):
        """
        流式生成，支持云端/本地降级（主要用于 Coordinator 或 Chat）。
        tier 决定优先级：LOCAL 优先本地，PREMIUM/LONG_CONTEXT 优先云端。
        AOS 3.9.8 添加了 force_tier: 绕过 CFO 经济管控。
        """
        # 动态决定优先级
        if not self.cloud_available and not self.local_available:
            raise Exception("未配置任何可用流式模型")

        # 模式路由逻辑
        # [AOS 3.9.8/2.8.7] 动态层级调整
        effective_tier = tier
        if self.economy and not force_tier:
            recommended = self.economy.get_recommended_tier()
            if recommended == "LOCAL" and tier == "PREMIUM":
                logger.info("💰 [CFO] 处于生存模式，主循环被强制降级为 LOCAL")
                effective_tier = "LOCAL"

        if not self.cloud_available:
            order = ["LOCAL"]
        elif not self.local_available:
            order = ["CLOUD"]
        elif self.agent_mode == "CLOUD":
            order = ["CLOUD"]
        elif self.agent_mode == "TURBO":
            order = ["CLOUD", "LOCAL"]
        elif self.agent_mode == "SEQUENTIAL":
            if effective_tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]
            else:
                order = ["LOCAL", "CLOUD"]
        else:  # AUTO 模式
            if effective_tier in ("PREMIUM", "LONG_CONTEXT"):
                order = ["CLOUD", "LOCAL"]
            else:
                order = ["LOCAL", "CLOUD"]



        last_error = None
        print(f"DEBUG: [Stream] Mode={self.agent_mode}, Tier={tier}(Effective={effective_tier}), Order={order}, Force={force_tier}")
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
                    
                    # [AOS 2.9.7] 云端内容清洗，防止 DeepSeek 报 400 Bad Request
                    sanitized_messages = self._sanitize_messages_for_cloud(messages)
                    
                    kwargs = {
                        "model": self.cloud_config["model"],
                        "messages": sanitized_messages,
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
            except (asyncio.CancelledError, KeyboardInterrupt):
                # 严禁吞噬核心控制信号
                raise
            except Exception as e:
                import traceback
                error_msg = f"{type(e).__name__}: {str(e)}"
                
                # [FEATURE] Friendly error for RateLimit (HTTP 429)
                if "429" in error_msg or "rate_limit" in error_msg.lower():
                    friendly_msg = "⚠️ [API 限流] 线上模型当前请求过多（429），正在尝试降级到本地或等待重试..."
                    logger.warning(friendly_msg)
                
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
        self.skill_manager = SkillManager(unified_client=self.unified_client, agent_ref=self)
        # AOS 2.7+: 任务隔离区路径
        self.workspace_path = None
        # [AOS 2.9] 初始化工具列表，防止 connect 前调用引发 AttributeError
        self.openaiTools = []

        
    async def prepare_for_retry(self, blackboard: Blackboard):
        """
        [AOS 4.3] 强制洗脑（逻辑消磁）：重试前抹除所有完成标志。
        确保每一轮重跑都是真实的物理重跑，拒绝 Skip Trap。
        """
        logger.info("♻️ [AOS 4.3] 正在执行重试消磁程序...")
        keys_to_clear = [k for k in blackboard.facts.keys() if k.startswith("_task_done_")]
        for k in keys_to_clear:
            blackboard.delete(k)
            logger.info("   - 已物理抹除专家状态: %s (强制重跑)", k)
        
        # 抹除全局完成标志
        blackboard.delete("_task_completed")
        logger.info("✅ 逻辑消磁完成：已物理强制重置所有专家状态")

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
                    "description": "召唤一个专注特定领域的子专家（无状态）。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "description": "专家角色定义"},
                            "task": {"type": "string", "description": "具体子任务描述"},
                            "context_summary": {"type": "string", "description": "必要的上下文摘要"}
                        },
                        "required": ["role", "task"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "write_blackboard",
                    "description": "向任务黑板写入一个持久化事实，供所有子专家共享。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "事实键名"},
                            "value": {"type": "string", "description": "具体内容"},
                            "author": {"type": "string", "description": "记录者身份"}
                        },
                        "required": ["key", "value"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_blackboard",
                    "description": "读取黑板上的所有共享事实。",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
        ]
        self.openaiTools.extend(aos_tools)
        
        # [AOS 3.8.5] 自动加载标注为 always_loaded 的技能 (如 filesystem)
        # 这确保了 read_file/write_file 等基础生存工具在启动时就可用
        await self.skill_manager.load_always_loaded_skills(workspace_path=self.workspace_path)
        
        toolNames.extend(["search_skills", "load_skill", "run_autonomous_task", "spawn_expert", "write_blackboard", "read_blackboard"])

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
            {
                "type": "function",
                "function": {
                    "name": "http_fetch",
                    "description": "[AOS 4.9 刺客武装] 物理级 HTTP 下载工具。直接抓取资源并存入本地文件，绕过浏览器。专用于抓取静态 JS/JSON/OSS 直链。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "目标资源的完整 URL"},
                            "save_path": {"type": "string", "description": "本地保存相对路径，如 'sportList.js'"}
                        },
                        "required": ["url"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "clear_all_scheduled_tasks",
                    "description": "清空所有已注册的定时任务。慎用！",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
        ]
        self.openaiTools.extend(phase3_tools)
        toolNames.extend(["add_scheduled_task", "list_scheduled_tasks", "cancel_scheduled_task", "clear_all_scheduled_tasks", "discover_and_install_skill", "http_fetch"])

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

        total_aos = len(aos_tools) + len(phase3_tools) + len(aea_tools)
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

    async def spawn_expert(self, role: str, task_description: str, context_summary: str = "", default_tier: str = "LOCAL") -> str:
        """
        召唤无状态子专家：独立完成一个细分任务后立即销毁。
        [AOS 3.4/3.5.2] 柔性路由：根据任务难度自动升档。
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

        # [AOS 3.4/3.5.2] 柔性路由与智商拦截器
        from prompts import AOS_GOD_MODE_PROMPT, EXPERT_REGISTRY
        
        # 1. 基础档位：优先读取注册表，否则使用传入的默认档位
        expert_config = EXPERT_REGISTRY.get(role, {})
        target_tier = expert_config.get("tier", default_tier)
        
        # 2. [AOS 3.5.2] 智商强制拦截器：对于关键的物理操作任务，强制升档到 PREMIUM
        high_iq_keywords = ["discover", "installer", "scout", "loader", "deploy", "install", "config"]
        combined_text = (role + " " + task_description).lower()
        if target_tier == "LOCAL":
            for kw in high_iq_keywords:
                if kw in combined_text:
                    target_tier = "PREMIUM"
                    print(f"☁️ [柔性路由] 检测到高难度任务 '{role}'，自动为子专家申请 PREMIUM 云端加速...")
                    break
        
        # 3. 上帝模式注入 (针对 LOCAL 智商不足的情况)
        final_system_prompt = f"你是一个专注的 {role} 专家。简洁、精准地完成指定任务。"
        if target_tier == "LOCAL":
            final_system_prompt = AOS_GOD_MODE_PROMPT + "\n\n" + final_system_prompt

        print(f"🧬 [AOS] 正在召唤子专家: {role} (算力层级: {target_tier})...")
        try:
            # [AOS 3.7.4/3.9.5] 核心技能强制注入 (笔与笔记本)
            # 确保子专家无论如何都有权读写文件系统和黑板
            await self.skill_manager.load_skill("filesystem", workspace_path=self.workspace_path)
            
            expert_available_tools = self.skill_manager.get_all_tools()
            essential_tools = ["write_blackboard", "read_blackboard", "cfo_report", "discover_and_install_skill"]
            existing_names = set(t["function"]["name"] for t in expert_available_tools)
            for t_name in essential_tools:
                if t_name not in existing_names:
                    for sys_t in self.openaiTools:
                        if sys_t["function"]["name"] == t_name:
                            expert_available_tools.append(sys_t)
                            break

            # [AOS 7.4] 物理主權：使用 UUID 徹底隔絕併發記憶污染
            import uuid
            safe_id = f"expert_{role}_{uuid.uuid4().hex[:8]}"
            
            # [AOS 3.7.4/3.9.5] 升级为「递归自治」：使用 execute_with_tools 替代简单的 generate
            # 这赋予了子专家思考并调用工具的能力，真正解决幻觉并提交物理成果
            result = await self.execute_with_tools(
                system_prompt=final_system_prompt,
                user_demand=expert_prompt,
                tier=target_tier,
                context_id=safe_id,
                workspace_path=self.workspace_path,
                tools=expert_available_tools
            )
            
            print(f"✅ [AOS] 子专家 [{role}] 已完成任务并销毁")
            return result
        except Exception as e:
            logger.error("子专家 [%s] 执行失败: %s", role, e)
            return f"子专家执行失败: {e}"

    # ========== AOS: 内部工具调度器 ==========

    async def _handle_internal_tool(self, func_name: str, arguments: dict) -> str | None:
        """
        处理 AOS 内部工具调用（非 MCP 工具）。
        [AOS 3.8/3.7.3] 别名解析加持：支持 board_update 等多种幻觉前缀。
        """
        # [AOS 3.7.3] 利用统一解析器进行别名与脱水处理
        target_func = self.skill_manager.resolve_alias(func_name)
        
        # 内部元工具直接匹配
        if target_func == "search_skills":
            query = arguments.get("query", "")
            skills = self.skill_manager.search(query)
            return json.dumps(skills, ensure_ascii=False)
            
        elif target_func == "read_skill":
            name = arguments.get("name", "")
            content = self.skill_manager.get_content(name)
            return content or "技能未找到"
            
        elif target_func == "load_skill" or target_func == "hot_load_skill":
            name = arguments.get("name", "")
            # [AOS 5.0] 自动识别热加载需求
            result = await self.skill_manager.hot_load_skill(name, workspace_path=self.workspace_path)
            return json.dumps(result, ensure_ascii=False)

        elif target_func == "unload_skill":
            name = arguments.get("name", "")
            result = await self.skill_manager.unload_skill(name)
            return json.dumps(result, ensure_ascii=False)

        elif target_func == "list_skills":
            skills = self.skill_manager.list_available()
            return json.dumps(skills, ensure_ascii=False)

        elif target_func == "write_blackboard":
            key = arguments.get("key")
            value = arguments.get("value")
            author = arguments.get("author", "Unknown")
            self.blackboard.write(key, value, author=author)
            return f"成功写入黑板: {key}"

        elif target_func == "read_blackboard":
            return self.blackboard.read_all()

        elif target_func == "spawn_expert":
            role = arguments.get("role", "Expert")
            task = arguments.get("task", "")
            # [AOS 2.9] 强制脱水传递上下文
            context = arguments.get("context_summary", "")
            if len(context) > 500: context = context[:500] + "..."
            
            # [AOS 3.4] 调用柔性路由专家召唤接口
            return await self.spawn_expert(role, task, context)

        elif func_name == "cfo_approve":
            cost = arguments.get("estimated_cost", 0)
            value = arguments.get("expected_value", 0)
            result = self.economy.should_approve_cloud_call(cost, value)
            return json.dumps(result, ensure_ascii=False)

        elif func_name == "cfo_report":
            return self.economy.get_financial_report()
            
        elif func_name == "inject_funds":
            amount = arguments.get("amount", 0)
            desc = arguments.get("description", "注资")
            self.economy.inject_funds(amount=amount)
            return f"成功注入资金 ${amount}: {desc}"
            
        elif func_name == "discover_and_install_skill" or func_name == "auto_install_and_load":
            query = arguments.get("query", "")
            # [AOS 5.0] 升级为全自动闭环安装并加载
            result = await self.skill_manager.auto_install_and_load(query, self.session, self.workspace_path)
            return json.dumps(result, ensure_ascii=False)

        elif func_name == "run_checkup":
            # [AOS 4.0] 免疫自检直通车
            result = await self.skill_manager.run_full_checkup()
            return json.dumps(result, ensure_ascii=False)

        # 调度器工具
        elif func_name == "add_scheduled_task":
            result = self.scheduler.add_task(
                task_id=arguments.get("task_id", ""),
                description=arguments.get("description", ""),
                cron_expr=arguments.get("cron_expr", ""),
                action=arguments.get("action", "print"),
                payload=arguments.get("payload", "")
            )
            return f"⏰ [调度器] 任务已添加: {json.dumps(result, ensure_ascii=False)}"

        elif func_name == "list_scheduled_tasks":
            tasks = self.scheduler.list_tasks()
            return json.dumps(tasks, ensure_ascii=False)

        elif func_name == "cancel_scheduled_task":
            id = arguments.get("task_id", "")
            result = self.scheduler.cancel_task(id)
            return f"⏰ [调度器] 任务已取消: {json.dumps(result, ensure_ascii=False)}"

        elif func_name == "clear_all_scheduled_tasks":
            result = self.scheduler.clear_all_tasks()
            return f"💥 [调度器] 所有任务已清理: {json.dumps(result, ensure_ascii=False)}"

        # [AOS 4.9] Assassin Armament: 刺客级物理下载工具
        elif func_name == "http_fetch":
            url = arguments.get("url", "")
            save_path = arguments.get("save_path", "downloaded_file.js")
            # 路径锚定
            if not os.path.isabs(save_path):
                if self.workspace_path:
                    save_path = os.path.join(self.workspace_path, save_path)
                else:
                    save_path = os.path.abspath(save_path)
            
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }
                r = requests.get(url, timeout=30, headers=headers)
                r.raise_for_status()
                with open(save_path, 'wb') as f:
                    f.write(r.content)
                size = len(r.content)
                logger.info(f"🎯 [AOS 4.9] http_fetch 成功: {url} -> {save_path} ({size} 字节)")
                return f"SUCCESS: {size} bytes saved to {os.path.basename(save_path)}. 物理文件已落地。"
            except Exception as e:
                err_msg = f"FAILED: http_fetch 无法获取 {url}. 错误: {str(e)}"
                logger.error(err_msg)
                return err_msg

        return None

    def _normalize_tool_name(self, func_name: str) -> str:
        """
        [AOS 5.2] 工具名归一化、去重与无缝纠偏。
        处理幻觉前缀以及字符粘连（tool_tool / tooltool）。
        """
        # 1. 处理无缝粘连 (Seamless Adhesion): tooltool -> tool
        if len(func_name) % 2 == 0:
            half = len(func_name) // 2
            if func_name[:half] == func_name[half:]:
                logger.info("🧠 [AOS 5.2] 检测到无缝指令粘连并纠正: %s -> %s", func_name, func_name[:half])
                func_name = func_name[:half]

        # 2. 处理有分隔符的粘连: tool_tool -> tool
        for sep in ["_", "."]:
            if sep in func_name:
                parts = func_name.split(sep)
                if len(parts) == 2 and parts[0] == parts[1]:
                    logger.info("🧠 [AOS 5.2] 检测到有分隔符指令粘连并纠正: %s -> %s", func_name, parts[0])
                    func_name = parts[0]
                    break

        # 2. 剥离命名空间前缀
        prefixes = ["filesystem", "github", "browser", "sqlite", "mcp"]
        separators = ["_", "."]
        
        for p in prefixes:
            for sep in separators:
                full_prefix = p + sep
                if func_name.startswith(full_prefix):
                    stripped = func_name[len(full_prefix):]
                    if self.skill_manager.is_tool_available(stripped):
                        logger.info("🧪 [AOS 5.0] 工具名纠偏: %s -> %s", func_name, stripped)
                        return stripped
        return func_name

    def _anchor_tool_paths(self, tool_name: str, arguments: dict, workspace_override: str | None = None) -> None:
        """
        [AOS 3.6] 绝对路径锚点拦截器。
        强制将文件操作工具的相对路径（./xxx）锚定到当前任务的工作区。
        """
        effective_wsp = workspace_override if workspace_override else self.workspace_path
        if not effective_wsp:
            return

        # 涉及路径操作的工具列表
        path_sensitive_tools = [
            "write_file", "read_file", "create_or_update_file",
            "get_file_contents", "filesystem_write_file", "filesystem_read_file",
            "list_dir", "create_directory", "filesystem_list_directory"
        ]

        if tool_name in path_sensitive_tools or any(kw in tool_name for kw in ["file", "dir"]):
            raw_path = arguments.get("path")
            if raw_path and isinstance(raw_path, str):
                # 如果是相对路径
                if not os.path.isabs(raw_path):
                    # 清洗前缀 ./
                    clean_path = raw_path[2:] if raw_path.startswith("./") else raw_path
                    # 锚定到任务工作区
                    anchored_path = os.path.abspath(os.path.join(effective_wsp, clean_path))
                    arguments["path"] = anchored_path
                    logger.info("⚓ [AOS 3.6] 空间锚点拦截: %s -> %s", raw_path, anchored_path)

    def _prune_history(self, context_id: str, keep_last_n: int = 3) -> list[dict]:
        """
        [AOS 2.9] 历史脱水：只保留 System Prompt + 最近 N 轮交互。
        [AOS 2.9.8] 修复：必须保留最新一条用户消息，否则模型看不到当前问题。
        """
        if context_id not in self.memories or not self.memories[context_id]:
            return []
            
        msgs = self.memories[context_id]
        if len(msgs) <= 2: return list(msgs)
        
        # 1. 提取并保留 system prompt
        system_msg = msgs[0] if msgs[0]["role"] == "system" else None
        
        # 2. 确保最新一条用户消息一定被保留（这是当前正在回答的问题）
        latest_user_msg = None
        latest_user_idx = -1
        for idx in range(len(msgs) - 1, -1, -1):
            if msgs[idx]["role"] == "user":
                latest_user_msg = msgs[idx]
                latest_user_idx = idx
                break
        
        # 3. 从最后一条消息往前扫描，保留最近 N 轮有意义的交互
        useful_rounds = []
        i = len(msgs) - 1
        found = 0
        while i >= 1 and found < keep_last_n:
            msg = msgs[i]
            if msg["role"] == "tool":
                # [AOS 5.5] 修复多工具调用截断 Bug：原子化采集整个 tool 序列
                tool_group = []
                j = i
                # 往前收集所有连续的 tool 消息
                while j >= 1 and msgs[j]["role"] == "tool":
                    tool_group.insert(0, msgs[j])
                    j -= 1
                # 必须找到触发这些 tool_calls 的那个 assistant 消息
                if j >= 1 and msgs[j]["role"] == "assistant" and msgs[j].get("tool_calls"):
                    useful_rounds.insert(0, tool_group) # 先临时存为 list 以便整体 insert
                    useful_rounds.insert(0, msgs[j])
                    found += 1
                    i = j - 1
                    continue
                else:
                    # 孤儿 tool 消息，跳过
                    i = j
                    continue
            elif msg["role"] == "assistant" and not msg.get("tool_calls"):
                useful_rounds.insert(0, msg)
                found += 1
            elif msg["role"] == "user":
                # 普通用户消息也保留（闲聊场景下的上下文）
                useful_rounds.insert(0, msg)
                found += 1
            i -= 1
            
        # 4. 组装最终消息序列
        final = []
        if system_msg: 
            final.append(system_msg)
            
        for round_item in useful_rounds:
            if isinstance(round_item, list):
                final.extend(round_item)
            else:
                final.append(round_item)
        
        # 5. 确保最新用户消息在末尾（如果它不在 useful_rounds 里）
        if latest_user_msg and (not final or final[-1] is not latest_user_msg):
            final.append(latest_user_msg)
            
        return final

    def _get_combined_tools(self, slim: bool = False) -> list[dict]:
        """
        获取静态工具与动态技能工具的合集 (AOS 2.3)
        @param slim: 如果为 True, 则只返回核心元工具（用于极致省钱模式）
        """
        # [AOS 2.9/4.9/6.4] 动态工具带：极大减少 Context 消耗。write_file 现已设为常驻元工具。
        meta_tool_names = {"search_skills", "read_skill", "load_skill", "unload_skill", "list_skills", "http_fetch", "write_file", "edit_file", "read_file", "list_dir"}
        
        all_tools = list(self.openaiTools) if self.openaiTools else []
        skill_tools = self.skill_manager.get_all_tools()
        
        if skill_tools:
            existing_names = set(t["function"]["name"] for t in all_tools)
            for st in skill_tools:
                if st["function"]["name"] not in existing_names:
                    all_tools.append(st)
        
        if slim:
            # 仅保留元工具
            tools = [t for t in all_tools if t["function"]["name"] in meta_tool_names]
        else:
            tools = all_tools if all_tools else []
        
        # [AOS 7.5.8] 核心增強：動態注入分片讀取參數 (offset)
        # 防止模型因不知道有 offset 而陷入死循環
        read_target_tools = ["read_file", "read_text_file", "filesystem_read_file", "get_file_contents"]
        for t in tools:
            if t["function"]["name"] in read_target_tools:
                params = t["function"].get("parameters", {}).get("properties", {})
                if "offset" not in params:
                    params["offset"] = {
                        "type": "integer",
                        "description": "[AOS 7.5.8] 大文件读取偏移量 (bytes)。当文件超过 30KB 时，请根据反馈中的建议传入此参数以读取下一分片。"
                    }
                    # 确保 parameters 结构完整
                    if "parameters" not in t["function"]:
                        t["function"]["parameters"] = {"type": "object", "properties": params}
                    else:
                        t["function"]["parameters"]["properties"] = params
                        
        return tools if tools else None

    async def chat(self, userInput: str, tier: str = "LOCAL", no_tools: bool = False) -> AsyncGenerator[str, None]:
        """
        AOS 核心 ReAct 循环（流式版）。
        支持目标驱动、自动反思、技能加载、子专家召唤与 Token 预算控制。
        [AOS 7.0] no_tools: 物理级避险开关。如果为 True，则剥离所有工具，强制进入纯社交模式。
        """
        if "main" not in self.memories:
            from prompts import AOS_GOD_MODE_PROMPT
            final_prompt = self.systemPrompt
            if tier == "LOCAL":
                final_prompt = AOS_GOD_MODE_PROMPT + "\n\n" + self.systemPrompt
            self.memories["main"] = [{"role": "system", "content": final_prompt}]

        # AOS 3.2: 动态注入技能雷达 (Skill Radar)
        # 获取最新的技能菜单并更新系统提示词（非持久化到 memory，仅限该轮调用）
        radar_menu = self._get_skill_radar_menu()
        
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

            # [AOS 2.9] 动态截断上下文，结合脱水策略极大减少 Token 消耗
            self._truncate_memory("main")
            pruned_main = self._prune_history("main", keep_last_n=3)
            
            # AOS 3.2: 在裁剪后的上下文中注入技能雷达菜单
            if pruned_main and pruned_main[0]["role"] == "system":
                original_prompt = pruned_main[0]["content"]
                pruned_main[0]["content"] = original_prompt + f"\n\n📡 [技能雷达] 当前可用技能 (请使用 load_skill 加载): {radar_menu}"
            
            kwargs: dict = {
                "messages": pruned_main,
            }
            # tier 由外部传入，决定本地/云端优先级

            fullContent = ""
            toolCallsDict = {}  # 用于按索引累计 tool_calls 数据

            # [AOS 2.9] 动态工具带：主对话初始只带元工具，极大减少首轮 Token 与推理压力
            # [AOS 7.0] no_tools 强制隔离：剥离所有实弹工具
            current_tools = self._get_combined_tools(slim=True) if not no_tools else None
            
            response = self.unified_client.generate_stream(
                tier=tier,
                messages=pruned_main,
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

            # AOS 3.2: 还原系统提示词（防止重复叠加）
            if pruned_main and pruned_main[0]["role"] == "system":
                pruned_main[0]["content"] = original_prompt

            # 流完后，将最终消息存入历史
            assistantMsg = {"role": "assistant", "content": fullContent or None}
            if toolCallsDict:
                assistantMsg["tool_calls"] = list(toolCallsDict.values())
            
            # [AOS 3.7] 本地语者 (Local Whisperer)：尝试从 Markdown JSON 中抢救
            if not toolCallsDict and "```json" in fullContent:
                try:
                    rescued = extract_json(fullContent)
                    if isinstance(rescued, dict):
                        t_name = rescued.get("command") or rescued.get("action") or rescued.get("tool")
                        t_args = rescued.get("parameters") or rescued.get("arguments") or rescued
                        if t_name:
                            toolCallsDict[0] = {
                                "id": f"rescued_{int(time.time())}",
                                "type": "function",
                                "function": {"name": t_name, "arguments": json.dumps(t_args, ensure_ascii=False)}
                            }
                            logger.info("🔧 [AOS 3.7] 本地语者 (Chat) 成功从 Markdown JSON 中抢救出工具调用: %s", t_name)
                except:
                    pass

            self.memories["main"].append(assistantMsg)

            # 如果没有工具调用，结束循环
            if not toolCallsDict:
                # 跟踪 Token 消耗
                self.token_budget.consume(self.token_budget.estimate_tokens(fullContent))
                return

            # [AOS 3.10.0/3.10.1] Protocol Sandbox & Serial Lock: 协议隔离带与串行闭锁
            # 为流式响应缓存多重工具调用结果，【强制】按序串行执行以便满足 Kimi 等模型的原子性 ID 要求
            current_tool_messages = []
            logger.info("🔒 [AOS 3.10.1] Serial Protocol Lock: 正在按照严格顺序串行执行 %d 个工具调用...", len(assistantMsg["tool_calls"]))

            # 执行工具调用
            for tc in assistantMsg["tool_calls"]:
                funcName = tc["function"]["name"]
                arguments = tc["function"]["arguments"]  # 已经是字符串

                # 死循环检测：如果是侦察类请求，给予更大宽容度
                call_sig = f"{funcName}:{arguments}"
                call_history.append(call_sig)
                
                # [AOS 3.9.7] 优雅的“侦察容错” (Graceful Polling)
                max_tolerance = 6 if any(kw in funcName for kw in ["list_directory", "read_blackboard", "list_files", "get_file_contents"]) else 3
                
                if call_history.count(call_sig) >= max_tolerance:
                    yield "⚠️ [系统提示] 检测到模型正在反复执行相同的工具调用，已强制中断。请尝试换个方式描述您的需求，或者检查搜索关键词是否过于冷门。"
                    return

                logger.info("调用工具: %s(%s)", funcName, arguments[:200])

                try:
                    parsed_args = json.loads(arguments)
                    # [AOS 3.6] 绝对路径锚定拦截器
                    self._anchor_tool_paths(funcName, parsed_args)

                    # AOS 3.5.1: 幻觉前缀自动剥离纠偏
                    actual_func = self._normalize_tool_name(funcName)
                    if False:
                        pass
                    prefixes = ["filesystem_", "github_", "browser_", "sqlite_", "mcp_"]
                    for p in prefixes:
                        if funcName.startswith(p):
                            stripped = funcName[len(p):]
                            # 验证剥离后是否有效
                            if self.skill_manager.is_tool_available(stripped):
                                logger.info("🧪 [AOS 3.5.1] 检测到工具名幻觉: %s -> %s (已纠偏)", funcName, stripped)
                                actual_func = stripped
                                break

                    # AOS: 优先尝试内部工具（技能、子专家、黑板）
                    internal_result = await self._handle_internal_tool(actual_func, parsed_args)
                    if internal_result is not None:
                        resultText = internal_result
                    else:
                        # AOS 2.0: 尝试从动态加载的技能中调用
                        skill_result = await self.skill_manager.call_tool(actual_func, parsed_args)
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

                current_tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": resultText,
                    }
                )

            # [AOS 3.10.0] 确保全量 tool_calls 回传，满足原子性要求
            self.memories["main"].extend(current_tool_messages)

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
        
        # [AOS 4.7.1] 同时清理黑板（保留固定的粘性变量）
        if hasattr(self, "blackboard"):
            self.blackboard.clear(include_sticky=False)
            
        logger.info("🗑️ 已清理并重置记忆与黑板: %s", context_id)

    def clearAllMemories(self):
        """
        核爆式清理：删除 memories/ 目录下所有文件并重置内存
        [AOS 2.9.5] 内存保护补丁：排除关键基础设施文件 (.db, blackboard.json)
        """
        protected_files = ["economy.db", "blackboard.json"]
        if os.path.exists(self.memory_dir):
            for filename in os.listdir(self.memory_dir):
                file_path = os.path.join(self.memory_dir, filename)
                # 保护机制：跳过数据库和黑板、以及任何扩展名为 .db 的文件
                if filename in protected_files or filename.endswith(".db"):
                    logger.debug("🛡️ [AOS 2.9.5] 免清理保护触发: %s", filename)
                    continue
                
                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        logger.warning("清理文件 %s 失败: %s", filename, e)
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
        print("\n💾 [AOS] 正在启动安全关闭序列...")
        
        # 1. 保存对话记忆
        try:
            for context_id in list(self.memories.keys()):
                self.saveMemory(context_id)
            logger.info("💾 所有对话记忆已持久化")
        except Exception as e:
            logger.error("🚫 记忆保存失败: %s", e)

        # 2. 停止定时任务调度器
        if hasattr(self, "scheduler"):
            try:
                await self.scheduler.stop()
            except Exception as e:
                logger.error("🚫 调度器停止失败: %s", e)

        # 3. 卸载所有动态技能 (AOS 2.7 隔离卸载)
        if hasattr(self, "skill_manager"):
            try:
                await self.skill_manager.unload_all()
            except Exception as e:
                logger.error("🚫 技能卸载失败: %s", e)

        # 4. 清理 Docker 沙盒
        if hasattr(self, "docker_sandbox"):
            try:
                self.docker_sandbox.cleanup_all()
                logger.info("🐳 Docker 沙盒已清理")
            except Exception as e:
                logger.error("🚫 Docker 清理失败: %s", e)

        print("✨ [AOS] 关闭序列完成，资源已释放。\n")
        logger.info("💾 所有 Agent 记忆已持久化到 %s/", self.memory_dir)

    def _setup_action_workspace(self, action_name: str) -> str:
        """
        [AOS 7.1] 为特定动作设置物理隔离的工作区。
        使用结构: Workspace/ActionName/YYYYMMDD_HHMMSS
        """
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 允许的动作目录映射，确保命名规范
        action_dir_map = {
            "search": "Search",
            "analyze": "Analyze",
            "review": "Review",
            "deploy": "Deploy",
            "auto": "Auto",
            "blitz": "Blitz"
        }
        # [AOS 7.4] 物理隔離：增加納秒與隨機後綴，防止併發啟動導致的路徑衝突
        import time, random
        sub_ms = int((time.time() % 1) * 1000)
        rand_id = random.randint(100, 999)
        dir_id = f"{timestamp}_{sub_ms:03d}_{rand_id}"
        
        dir_name = action_dir_map.get(action_name.lower(), action_name.capitalize())
        
        rel_path = os.path.join("Workspace", dir_name, f"{action_name}_{dir_id}")
        abs_path = os.path.abspath(rel_path)
        
        os.makedirs(abs_path, exist_ok=True)
        self.workspace_path = abs_path
        
        # 同步更新核心子组件
        if hasattr(self, "skill_manager"):
            self.skill_manager.workspace_path = abs_path
        
        logger.info("📁 [AOS 7.1] 已激活物理隔离区: %s", rel_path)
        return abs_path

    # ========== AOS 2.0: 自治编排引擎 ==========

    async def autonomous_execute(self, user_demand: str) -> AsyncGenerator[str, None]:
        """
        /auto 命令入口：启动全自治任务循环。
        动态招聘子 Agent -> 黑板协作 -> AI 裁判验收 -> 多轮自愈。
        """
        # AOS 3.3: 基因共鸣 (Gene Resonance) 预先加载
        matched = self.skill_manager.match_genes(user_demand)
        for sname in matched:
            if sname not in self.skill_manager.loaded_skills:
                yield f"🧬 [基因共鸣] 识别到任务相关的沉睡技能: 🟢 {sname}\n"
                await self.skill_manager.load_skill(sname, workspace_path=self.workspace_path)
        orchestrator = Orchestrator(
            unified_client=self.unified_client,
            skill_manager=self.skill_manager,
            blackboard=self.blackboard,
            agent=self, # AOS 2.1: 传递当前 Agent 实例以便执行工具调用
            exp_engine=self.exp_engine, # AOS 2.4+: 共享经验引擎实例
        )
        # run_mission 内部已集成 _setup_action_workspace
        async for chunk in orchestrator.run_mission(
            user_demand=user_demand,
            primary_session=self.session,
            max_rounds=3,
        ):
            yield chunk

    async def run_checkup(self) -> dict:
        """
        [AOS 4.0] 免疫系统入口：手动触发全量技能健康扫描与物理自愈。
        """
        return await self.skill_manager.run_full_checkup()

    def _extract_dsml_tool_calls(self, text: str) -> list[dict]:
        """
        [AOS 2.9.3] 紧急解析补丁：提取 DeepSeek 私有的 DSML 格式工具调用。
        兼容格式: <｜DSML｜invoke name="tool">...</｜DSML｜invoke>
        """
        import re
        import time
        calls = []
        # 正则匹配 invoke 块
        invoke_pattern = r'<｜DSML｜invoke name="(?P<name>.*?)">(?P<body>.*?)</｜DSML｜invoke>'
        # 正则匹配 parameter 块
        param_pattern = r'<｜DSML｜parameter name="(?P<pname>.*?)"(?:.*?)>(?P<pval>.*?)</｜DSML｜parameter>'
        
        for match in re.finditer(invoke_pattern, text, re.DOTALL):
            name = match.group('name')
            body = match.group('body')
            args = {}
            for pmatch in re.finditer(param_pattern, body, re.DOTALL):
                args[pmatch.group('pname')] = pmatch.group('pval')
            
            calls.append({
                "id": f"dsml_{int(time.time())}_{len(calls)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
            })
        return calls

    async def execute_with_tools(
        self,
        system_prompt: str,
        user_demand: str,
        context_id: str = "main",
        tier: str = "LOCAL",
        workspace_path: str | None = None,
        tools: list | None = None,
        max_iterations: int = 10 # [AOS 5.4] 支持外部指定循环上限
    ) -> str:
        """
        核心 ReAct 循环：
        1. 赋予 System Prompt 意识
        2. 自动根据需求和黑板上下文调用工具
        [AOS 2.8.5] 支持同步外部锁定的工作区路径。
        """
        # [AOS 7.4] 使用局部变量而非实例属性，防止并发竞争
        effective_workspace = workspace_path if workspace_path else self.workspace_path
        
        # AOS 3.3: 基因共鸣 (Gene Resonance) 预先加载
        matched = self.skill_manager.match_genes(user_demand)
        for sname in matched:
            if sname not in self.skill_manager.loaded_skills:
                # 针对子专家等静默模式加载
                await self.skill_manager.load_skill(sname, workspace_path=effective_workspace)
            
        # [AOS 4.8] 如果是 LOCAL 模式，强制 M2M 协议以减少模型冗余输出
        if tier == "LOCAL":
            self.unified_client.force_m2m_protocol = True
            
        # 初始化或恢复针对该任务的记忆
        final_system_prompt = system_prompt
        if tier == "LOCAL":
            from prompts import AOS_GOD_MODE_PROMPT
            final_system_prompt = AOS_GOD_MODE_PROMPT + "\n\n" + system_prompt

        messages = self.loadMemory(context_id, final_system_prompt)
        messages.append({"role": "user", "content": user_demand})
        
        # 针对该任务重置预算
        self.token_budget.reset()
        
        MAX_ITERATIONS = max_iterations # [AOS 5.4] 真值对齐：支持外部强制关断
        call_history = []
        fingerprint_history = set() # [AOS 7.3] 物理指纹历史：(tool, args_hash, result_hash)
        recent_errors = [] # [AOS 2.9] 同错熔断检测
        success_count = 0  # [AOS 4.8] 工具执行成功计数
        failure_count = 0  # [AOS 4.8] 工具执行失败计数
        consecutive_stale_rounds = 0 # [AOS 7.3] 连续无产出轮次
        self.has_logical_delta = False    # [AOS 7.5.8] 逻辑位移：是否从工具中拿到了新数据
        
        current_max = MAX_ITERATIONS
        for iteration in range(100): # 物理硬上限，逻辑上限由 current_max 控制
            if iteration >= current_max:
                break
            # [AOS 4.5] 极速闭环：记录执行前的黑板指纹快照
            hash_before = self.blackboard.get_snapshot_hash()
            # [AOS 6.2] 执行前记录文件列表
            iteration_start_files = os.listdir(effective_workspace) if effective_workspace and os.path.exists(effective_workspace) else []
            has_logical_delta = False # 每轮重置逻辑位移

            # [AOS 2.9.1] 临终关怀：最后一步前注入警告提示
            if iteration == MAX_ITERATIONS - 1:
                warning_msg = f"[系统警告] 这是最后一次机会！你必须在本次回复中给出最终结论，或根据已搜集到的信息输出一份详尽的任务状态摘要。不允许再调用工具。"
                self.memories[context_id].append({"role": "user", "content": warning_msg})
                # 脱水记忆时确保警告也被包含（强制保留最后一条）
                pruned_history = self._prune_history(context_id, keep_last_n=3)
            else:
                pruned_history = self._prune_history(context_id, keep_last_n=3)
            if self.token_budget.exceeded:
                return f"🚨 [预算限制] 任务已停止 (消耗 {self.token_budget.consumed} tokens)"

            self._truncate_memory(context_id)
            
            fullContent = ""
            toolCallsDict = {}
            
            # [AOS 2.9] 上下文脱水与动态工具带：只保留最近 3 轮对话，并仅加载当前所需工具
            pruned_history = self._prune_history(context_id, keep_last_n=3)
            # 子 Agent 默认需要完整工具带以便协作
            current_tools = tools if tools is not None else self._get_combined_tools(slim=False) 
            
            # [AOS 6.2] 多维指纹快照：执行前记录 DB 状态
            db_hash_before = self.scheduler.get_state_snapshot() if hasattr(self, "scheduler") else "none"
            
            # 使用流式生成并累积结果（复用流量控制与模型降级逻辑）
            response_stream = self.unified_client.generate_stream(
                tier=tier,
                messages=pruned_history,
                tools=current_tools if iteration < MAX_ITERATIONS - 1 else None # 最后一步禁用工具
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
            
            # [AOS 2.9.3] 增强型工具解析：如果 SDK 未识别到 tool_calls，尝试从文本中正则提取
            if not toolCallsDict:
                # 尝试提取 DSML
                if "<｜DSML｜" in fullContent:
                    extracted_calls = self._extract_dsml_tool_calls(fullContent)
                    if extracted_calls:
                        toolCallsDict = {i: c for i, c in enumerate(extracted_calls)}
                        logger.info("🛠️ [AOS] 成功从文本中提取出 DSML 格式工具调用")
                
                # [AOS 3.7] 本地语者 (Local Whisperer)：尝试从 Markdown JSON 中抢救
                if not toolCallsDict and "```json" in fullContent:
                    try:
                        rescued = extract_json(fullContent)
                        # 如果是 dict，且包含 command/action，或者本身就是参数
                        if isinstance(rescued, dict):
                            t_name = rescued.get("command") or rescued.get("action") or rescued.get("tool")
                            t_args = rescued.get("parameters") or rescued.get("arguments") or rescued
                            if t_name:
                                toolCallsDict[0] = {
                                    "id": f"rescued_{int(time.time())}",
                                    "type": "function",
                                    "function": {"name": t_name, "arguments": json.dumps(t_args, ensure_ascii=False)}
                                }
                                logger.info("🔧 [AOS 3.7] 本地语者成功从 Markdown JSON 中抢救出工具调用: %s", t_name)
                    except:
                        pass

            if toolCallsDict:
                assistantMsg["tool_calls"] = list(toolCallsDict.values())
            self.memories[context_id].append(assistantMsg)
            
            # 如果没有工具调用，说明任务执行到阶段性终点
            if not toolCallsDict:
                # 🚨 AOS 3.9.8/3.9.9: 反演戏拦截器 (Anti-Simulation Interceptor)
                # 凡是带有模拟操作的代码块但没有任何真实 tool call，直接视为骗局拦截
                import re
                simulation_keywords = ["[工具返回结果]", "模拟抓取结果", "仿真测试", "假设执行"]
                is_simulation = False
                
                if re.search(r'```(?:python|bash|sh|javascript)\s*(?:import\s+|npm\s+|npx\s+|def\s+simulate_)', fullContent, re.IGNORECASE):
                    is_simulation = True
                
                if not is_simulation:
                    for kw in simulation_keywords:
                        if kw in fullContent:
                            is_simulation = True
                            break
                            
                if is_simulation:
                    error_msg = "⚠️ [反演戏拦截] 检测到模拟演戏或幻觉捏造特征 (Simulation Trap)！如果你想获取数据或执行命令，必须调用系统提供的真实工具。绝对不允许仅仅输出假设结果而不执行工具。"
                    logger.warning(error_msg)
                    
                    # [AOS 3.10.1] 幻觉连坐清除 (Simulation Trap Cleanup)
                    # 识别子专家 ID 并强制清空其黑板存档，防止 stale checkpoint 误导 Orchestrator
                    if context_id.startswith("task_"):
                        role_id = context_id[5:]
                        keys_to_clear = [f"_task_done_{role_id}", f"result_{role_id}", f"error_{role_id}"]
                        for k in keys_to_clear:
                            self.blackboard.write(k, None) # 彻底注销该键值
                        logger.info("🧹 [AOS 3.10.1] 已清除子专家 '%s' 的残留黑板证据，强制下一轮重跑", role_id)

                    self.memories[context_id].append({"role": "user", "content": error_msg})
                    continue # 强制退回并要求重新执行真实工具

                # [AOS 6.3] 逻辑欺诈拦截 (Tool-Sign Mandate)
                if context_id == "blitz_direct" and not toolCallsDict:
                    # 1. 检测伪代码块 (Pseudo-Code Detection)
                    pseudo_code_indicators = ["```python", "```bash", "```javascript", "def ", "import ", "const ", "with open"]
                    has_pseudo_code = any(indicator in fullContent for indicator in pseudo_code_indicators)
                    
                    # 2. 检测推卸辞令 (Advice/Apology Detection)
                    advice_keywords = ["可以使用", "可以直接", "建议你", "参考代码", "由于我无法", "抱歉", "sorry"]
                    has_loitering = any(kw in fullContent for kw in advice_keywords)

                    # 逻辑欺诈判定：写了代码但没有任何工具调用标识
                    is_logical_fraud = has_pseudo_code and not toolCallsDict

                    if is_logical_fraud or has_loitering or (has_pseudo_code and len(fullContent) > 80):
                        logger.warning(f"🚨 [AOS 6.3 Tool-Sign Mandate] 拦截到专家 {context_id} 的‘逻辑欺诈’尝试。")
                        fraud_feedback = (
                            "🚨 [内核指令：逻辑欺诈拦截] 严禁在 BLITZ 模式下编写代码块或提供文档建议！\n"
                            "你当前的任务是【执行】，禁止通过文字进行‘教学’或‘演示’。必须且只能调用实弹工具（如 add_scheduled_task）。\n"
                            "立刻清除所有 Markdown 代码块，直接输出工具调用指令。多说一个字即视为内核逻辑死锁。"
                        )
                        self.memories[context_id].append({"role": "user", "content": fraud_feedback})
                        continue # 强制重写

                # [AOS 6.2] 多维零增量拦截 (Multi-Dim Guard)：指纹、DB 与物理增量核对
                hash_after = self.blackboard.get_snapshot_hash()
                db_hash_after = self.scheduler.get_state_snapshot() if hasattr(self, "scheduler") else "none"
                
                # 判定是否有任何维度的物理产出
                has_fs_delta = len(self._get_workspace_delta(iteration_start_files, workspace_override=effective_workspace)) > 0
                has_bb_delta = hash_before != hash_after
                has_db_delta = db_hash_before != db_hash_after
                
                # 如果没有任何物理变化且非单纯对话，拦截
                if not toolCallsDict and not (has_fs_delta or has_bb_delta or has_db_delta):
                    # 允许极简的确认信息通过，但禁止长篇大论的“假报告”
                    if len(fullContent) > 80:
                        logger.warning(f"⚠️ [极速拦截] 专家 {context_id} 试图输出无意义对话（物理/逻辑零增量），强制纠偏！")
                        loitering_feedback = "🚨 [极速拦截] 检测到你的回复未产生任何物理变化（文件/黑板/数据库皆无更新）。请停止吹嘘，必须立即使用工具执行物理操作！"
                        self.memories[context_id].append({"role": "user", "content": loitering_feedback})
                        continue 

                self.token_budget.consume(self.token_budget.estimate_tokens(fullContent))
                return fullContent
                
            # [AOS 3.10.1] Serial Protocol Lock: 云端 ID 原子性对齐
            logger.info("🔒 [AOS 3.10.1] Serial Protocol Lock: 正在串行执行子任务工具循环...")
            
            # [AOS 3.9.9] 协议铁壁 (Protocol Strictness):
            # 将多重工具调用结果缓存起来，所有工具都执行完后，一次性、按顺序推入记忆
            current_tool_messages = []
            
            # 执行工具调用 (ReAct Action)
            for tc in assistantMsg["tool_calls"]:
                funcName = tc["function"]["name"]
                arguments = tc["function"]["arguments"]
                
                # 死循环防护
                call_sig = f"{funcName}:{arguments}"
                call_history.append(call_sig)
                
                # [AOS 7.0] 强硬熔断：下调侦察容错至 3 次
                max_tolerance = 3 if any(kw in funcName for kw in ["list_directory", "read_blackboard", "list_files", "get_file_contents"]) else 3
                
                if call_history.count(call_sig) >= max_tolerance:
                    logger.error("🚫 [物理熔断] 专家逻辑陷入死锁 (超限调用 %s)，已强行断路。", funcName)
                    return f"⚠️ [物理熔断] 检测到工具调用死循环 (%s)，已由内核强制终止任务链路。" % funcName

                logger.info("[%s] 正在执行子任务工具: %s", context_id, funcName)
                
                try:
                    # [AOS 3.8] 工具名纠偏归一化
                    actual_func = self._normalize_tool_name(funcName)

                    parsed_args = json.loads(arguments)
                    resultText = None # Initialize as None to allow fallback checks
                    # [AOS 3.6] 絕對路徑錨定攔截器：使用本輪局部工作區
                    self._anchor_tool_paths(actual_func, parsed_args, workspace_override=effective_workspace)
                    
                    # [AOS 7.5] 大文件手柄協議：攔截讀取操作
                    read_tools = ["read_file", "read_text_file", "filesystem_read_file", "get_file_contents"]
                    if actual_func in read_tools or any(kw in actual_func for kw in ["read", "content", "file"]):
                        path = parsed_args.get("path")
                        if path and os.path.exists(path) and os.path.isfile(path):
                            f_size = os.path.getsize(path)
                            if f_size > 30 * 1024: # 30KB 閾值
                                offset = parsed_args.get("offset", 0)
                                logger.warning("🛑 [AOS 7.5] 觸發大文件分片讀取: %s (size: %d, offset: %d)", path, f_size, offset)
                                resultText = self._read_file_chunked(path, f_size, offset)
                    
                    # 按照优先级处理工具：内部 -> 技能 -> MCP
                    if resultText is None:
                        resultText = await self._handle_internal_tool(actual_func, parsed_args)
                    if resultText is None:
                        resultText = await self.skill_manager.call_tool(actual_func, parsed_args)
                    if resultText is None:
                        result = await self.session.call_tool(funcName, arguments=parsed_args)
                        if result and hasattr(result, "content") and result.content:
                            resultText = "\n".join([
                                (item.text if hasattr(item, "text") else str(item))
                                for item in result.content
                            ])
                        else:
                            resultText = "Error: Tool returned no data (None)"
                    
                    # [AOS 5.2] Physical Convergence Lock (收敛强关补丁)
                    # 如果工具反馈暗示任务已完成或环境已处于目标状态，立即关断循环返回结果，拒绝 Round 2
                    stop_keywords = ["共 0 个", "清理完毕", "Already cleaned", "0 tasks found", "处于最新状态", "已被物理抹除", "当前无任何待执行"]
                    
                    # [AOS 5.3] INSTANT_KILL Protocol: 硬核物理截断
                    # 针对调度器工具的成功信号，实现“活干完立即关断”
                    instant_kill_signals = ["⏰ [调度器]", "💥 [调度器]"]
                    
                    if any(sig in resultText for sig in instant_kill_signals):
                        logger.info("⚡ [AOS 5.3] INSTANT_KILL: 调度器操作成功，强制物理断电。")
                        self.memories[context_id].append({"role": "tool", "tool_call_id": tc["id"], "content": resultText})
                        return f"INSTANT_KILL_PASS: {resultText}"

                    # 🚨 [Fix AOS 7.5.8] 核心修复：删除此处多余的 append，防止进入 memories 时 ID 重复导致 400
                    # 恢復 AOS 5.2 收斂邏輯
                    if any(sk.lower() in str(resultText).lower() for sk in stop_keywords):
                        logger.info("🛑 [AOS 5.2] Physical Convergence: 目标物理达成，强制收敛。")
                        self.memories[context_id].append({"role": "tool", "tool_call_id": tc["id"], "content": resultText})
                        return f"TASK_COMPLETED: 物理目标已达成 (AOS 5.2 强行收敛终止)。结果详细反馈: {resultText}"
                    # [AOS 4.9] 协议加固 (JSON Pipe Fix)
                    # 针对大尺寸结果（如 JS 源码）进行极致截断预览，防止撑破 OpenAI/MCP JSON 管道
                    # 刺客的任务是“拿回证据”，不是“在对话里展示源码”
                    if len(resultText) > 1000:
                        preview = resultText[:500] + "\n\n...(中间数据已物理截断以保护管道)...\n\n" + resultText[-500:]
                        resultText = f"【物理数据快照 (已截断)】\n{preview}\n\n⚠️ 提示：完整内容已存入物理文件，严禁要求在对话中输出完整源码！"
                    
                    # [AOS 4.8] 识别工具执行结果，更新成功/失败计数器
                    if "Error:" in resultText or "错误:" in resultText or "failed" in resultText.lower():
                        failure_count += 1
                    else:
                        success_count += 1
                        
                    # [AOS 7.3] 智能重复与原地踏步检测
                    import hashlib
                    # 🛡️ 保护：防止 resultText 为 None 导致 encode() 崩溃
                    safe_result = str(resultText or "None")
                    args_hash = hashlib.md5(arguments.encode()).hexdigest()
                    result_hash = hashlib.md5(safe_result.encode()).hexdigest()
                    fingerprint = f"{funcName}:{args_hash}:{result_hash}"
                    
                    if fingerprint in fingerprint_history:
                        logger.warning("🚫 [AOS 7.3] 检测到重复执行且无结果位移: %s", funcName)
                        # 如果重复，不计入成功产出，增加停机权重
                        consecutive_stale_rounds += 0.5 
                    else:
                        fingerprint_history.add(fingerprint)
                        if any(kw in funcName for kw in ["read", "list", "get_file", "search"]):
                             self.has_logical_delta = True
                             logger.info("🧠 [AOS 7.5.8] 侦测到逻辑位移（拿到了新信息）: %s", funcName)

                    self.token_budget.consume(self.token_budget.estimate_tokens(resultText))
                    
                except Exception as e:
                    error_msg = str(e)
                    logger.error("子任务工具调用失败: %s - %s", funcName, error_msg)
                    resultText = f"工具执行错误: {error_msg}"
                    
                    # [AOS 2.9] 同错熔断检测：如果连续两次报完全相同的错，直接断电
                    recent_errors.append(error_msg)
                    if len(recent_errors) >= 2 and recent_errors[-1] == recent_errors[-2]:
                        return f"🛑 [熔断警报] 子 Agent 陷入重复错误: {error_msg}。为保护 CFO 资金，强制停止探索！"

                current_tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": resultText,
                })
                
            # [AOS 3.9.9] 确保全量 tool_calls 回传，无论中途有无内部异常
            self.memories[context_id].extend(current_tool_messages)

            # [AOS 7.5.8] 物理或逻辑位移判定
            hash_after = self.blackboard.get_snapshot_hash()
            has_physical_delta = (hash_before != hash_after) or (len(self._get_workspace_delta(iteration_start_files, workspace_override=effective_workspace)) > 0)
            
            if has_physical_delta or self.has_logical_delta:
                consecutive_stale_rounds = 0
                # 如果有产出且接近上限，自动延展（最高至 50 轮，确保读完大文件）
                if iteration >= current_max - 2 and current_max < 50:
                    current_max += 5
                    logger.info("📈 [AOS 7.5.8] 检测到有效位移（物理:%s, 逻辑:%s），动态延展预算至 %d 轮", has_physical_delta, self.has_logical_delta, current_max)
            else:
                consecutive_stale_rounds += 1
                
            if consecutive_stale_rounds >= 3:
                logger.warning("🛑 [AOS 7.3] 连续 3 轮无物理增量，判定为无效循环，强行关断。")
                return fullContent + "\n\n🛑 [AOS 7.3 系统干预] 检测到原地踏步（连续 3 轮无有效产出），已强制终止循环以保护预算。"
        
        # [AOS 2.9] 自动整理动态加载的技能，任务结束清空，保持“冷酷无情”的低成本状态
        # asyncio.create_task(self.skill_manager.unload_all())
        
        # [AOS 4.8] 硬核主权：如果任务包含工具调用但全数失败，则返回明确的失败信号，防止伪装成功
        if failure_count > 0 and success_count == 0:
            return f"🚫 [AOS 4.8 物理拒绝] 任务关键工具库调用失败 ({failure_count} 次错误)。物理路径未打通，拒绝撰写 Markdown 报告。底层错误细节：{fullContent[:300]}"

        # [AOS 2.9.1] 温柔终止：如果跑满 10 步还没完，返回最后一次的原始内容，而不是报错。
        return fullContent or f"🛑 [提示] 已达到最大限制 ({MAX_ITERATIONS} 步)，任务已暂停。当前进度：{fullContent[:200] if fullContent else '无输出'}"

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

    async def multiAgentReview(self, repo_urls: str | list[str]) -> AsyncGenerator[str, None]:
        """
        混合多 Agent 评审流：支持单个或多个项目的并发评审与对比分析。
        """
        if isinstance(repo_urls, str):
            repo_urls = [repo_urls]

        # [AOS 7.1] 激活隔离工作区
        wsp = self._setup_action_workspace("review")
        yield f"📁 [隔離] 正在 Review 空間建立沙盒: {wsp}\n"
        
        print(f"\n🚀 [Hybrid Mode: {self.mode}] 啟動 OpenClaw 多專家聯合評委會...")
        
        all_experts_results = []
        
        for url in repo_urls:
            yield f"🔍 正在加載項目數據: {url}...\n"
            # 1. 自適應加載數據 (基礎掃描)
            try:
                project_data = await self.adaptive_load_data(url)
            except Exception as e:
                yield f"❌ 加載 {url} 失敗: {e}\n"
                continue
            
            # 2. 調度專家評審 (AUTO/TURBO 模式併發)
            experts = list(EXPERT_REGISTRY.keys())
            if "Deployment_Executor" in experts:
                experts.remove("Deployment_Executor")
            if "scheduler_configurator" in experts:
                experts.remove("scheduler_configurator")
            if "SkillCurator" in experts:
                experts.remove("SkillCurator")
                
            tasks = []
            for name in experts:
                tasks.append(self.expertReview(name, EXPERT_REGISTRY[name], project_data))
                
            yield f"🕵️  正在調度 {len(tasks)} 位專家評審 {url}...\n"
            results = await asyncio.gather(*tasks)
            all_experts_results.append({
                "url": url,
                "results": results
            })

        # 3. 匯總/對比報告 (Coordinator)
        if len(all_experts_results) == 0:
            yield "❌ 未能在任何地址執行有效的專家評審。"
            return

        reviews_summary = ""
        for item in all_experts_results:
            url = item["url"]
            results = item["results"]
            reviews_summary += f"\n=== 項目評審: {url} ===\n"
            reviews_summary += "\n".join([
                f"- {r.get('dimension', '專家')} (得分: {r.get('score', '?')}): {r.get('summary', '')}" 
                for r in results if r
            ]) + "\n"
        
        if len(all_experts_results) > 1:
            system_prompt = COORDINATOR_SYSTEM_PROMPT + "\n\n請注意：當前有多個項目，請重點進行【橫向對比】，並在報告結尾給出明確的最佳推薦結論。"
        else:
            system_prompt = COORDINATOR_SYSTEM_PROMPT
            
        user_input = f"請根據以下專家評審意見生成最終報告。{'涉及對比' if len(all_experts_results) > 1 else ''}\n意見如下:\n{reviews_summary}"
        
        async for chunk in self.unified_client.generate_stream("PREMIUM", [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ]):
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

        # [AOS 7.1] 激活隔离工作区
        wsp = self._setup_action_workspace("deploy")
        yield f"📁 [隔离] 正在 Deploy 空间建立沙盒: {wsp}\n"
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

                        # [AOS 7.1] 归档生成的 Dockerfile
                        try:
                            df_path = os.path.join(self.workspace_path, "Dockerfile")
                            with open(df_path, "w", encoding="utf-8") as f:
                                f.write(dockerfile_content)
                            yield f"- **物理归档**: `Dockerfile` -> `{self.workspace_path}`\n"
                        except: pass

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

    def _get_skill_radar_menu(self) -> str:
        """
        [AOS 3.2] 生成简化的技能菜单。
        格式: name:description | ...
        """
        available = self.skill_manager.list_available()
        items = []
        for s in available:
            # 排除已加载的技能
            if not s.get("loaded"):
                desc = s.get("description", "")
                if "：" in desc: desc = desc.split("：")[-1]
                elif ": " in desc: desc = desc.split(": ")[-1]
                desc = desc[:50]
                items.append(f"{s['name']}: {desc}")
        
        return " | ".join(items) if items else "暂无沉睡技能"

    def _get_workspace_delta(self, initial_files: list[str], workspace_override: str | None = None) -> list[str]:
        """
        [AOS 6.1] 计算工作区文件增量。
        """
        wsp = workspace_override if workspace_override else self.workspace_path
        if not wsp or not os.path.exists(wsp):
            return []
        try:
            current_files = os.listdir(wsp)
            return [f for f in current_files if f not in initial_files]
        except Exception as e:
            logger.error(f"误差计算失败: {e}")
            return []

    def _read_file_chunked(self, path: str, size: int, offset: int = 0) -> str:
        """
        [AOS 7.5.8] 核心改进：支持大文件分片读取，每分片 30KB。
        """
        CHUNK_SIZE = 30 * 1024 # 30KB (用户要求升级)
        try:
            with open(path, 'rb') as f:
                f.seek(offset)
                data = f.read(CHUNK_SIZE)
                content = data.decode('utf-8', errors='ignore')
                
            next_offset = offset + len(data)
            has_more = next_offset < size
            
            summary = [
                f"📝 [AOS 7.5.8 分片读取] 当前偏移量: {offset} byte, 读取长度: {len(data)} bytes, 总大小: {size} bytes",
                "--- START CHUNK ---",
                content,
                "--- END CHUNK ---",
            ]
            
            if has_more:
                summary.append(f"\n💡 [系统建议] 文件尚未读完（已完成 {next_offset/size*100:.1f}%）。如需继续读取，请再次调用 read_file 并传入参数: {{\"path\": \"{path}\", \"offset\": {next_offset}}}")
            else:
                summary.append("\n✅ [系统反馈] 已到达文件末尾，全文读取完毕。")
                
            return "\n".join(summary)
        except Exception as e:
            return f"❌ [AOS 7.5.8] 分片读取失败: {e}"
