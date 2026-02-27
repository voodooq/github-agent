import asyncio
import json
import logging
import os
import re
from typing import AsyncGenerator
from openai import AsyncOpenAI
from mcp import ClientSession
from tool_converter import convertMcpToolsToOpenai
from prompts import EXPERT_REGISTRY, COORDINATOR_SYSTEM_PROMPT
from docker_sandbox import DockerSandboxAgent

# Logger configuration
logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """
    从模型输出中稳健地提取 JSON 对象。支持 markdown 自动剥离。
    """
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


class UnifiedClient:
    """
    统一 LLM 客户端：支持云端/本地路由与自动降级回退。
    云端使用 AsyncOpenAI SDK，本地使用 Ollama 原生 HTTP API（绕过兼容层 bug）。
    """

    def __init__(self, cloud_config: dict, local_config: dict):
        self.cloud_config = cloud_config
        self.local_config = local_config
        
        # 检测可用性
        self.cloud_available = bool(cloud_config.get("api_key") and cloud_config.get("model"))
        self.local_available = bool(local_config.get("model") and local_config.get("base_url"))
        
        # 内部诊断：打印加载情况（非敏感信息）
        print(f"DEBUG: Cloud Available={self.cloud_available}, Local Available={self.local_available}")
        print(f"DEBUG: Local Model='{local_config.get('model')}', BaseURL='{local_config.get('base_url')}'")

        
        # 初始化云端客户端
        self.cloud_client = AsyncOpenAI(
            api_key=cloud_config.get("api_key", "none"),
            base_url=cloud_config.get("base_url", "https://api.deepseek.com/v1"),
            timeout=60,
            max_retries=1,
        )
        
        # 初始化本地端参数
        local_base = local_config.get("base_url", "http://localhost:11434/v1").replace("/v1", "").rstrip("/")
        self._local_api_url = f"{local_base}/api/chat"
        self._local_model = local_config.get("model", "")
        
        # Ollama 单 GPU 串行推理，信号量防止并发排队
        self._local_semaphore = asyncio.Semaphore(1)

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
            return data["message"]["content"]

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
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        # 动态决定优先级
        if not self.cloud_available and not self.local_available:
            raise Exception("未配置任何可用模型（云端或本地）")

        if not self.cloud_available:
            order = ["LOCAL"]
        elif not self.local_available:
            order = ["CLOUD"]
        elif tier in ("PREMIUM", "LONG_CONTEXT"):
            order = ["CLOUD", "LOCAL"]
        else:
            order = ["LOCAL", "CLOUD"]



        last_error = None
        for label in order:
            try:
                if label == "LOCAL":
                    print(f"🏠 正在调用本地模型 ({self._local_model})...")
                    async with self._local_semaphore:
                        fmt = "json" if response_format and response_format.get("type") == "json_object" else None
                        return await self._call_ollama(messages, format=fmt)
                else:
                    if not self.cloud_available: continue
                    kwargs = {"model": self.cloud_config["model"], "messages": messages}
                    if response_format:
                        kwargs["response_format"] = response_format
                    response = await self.cloud_client.chat.completions.create(**kwargs)
                    return response.choices[0].message.content
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                logger.warning(f"模型 {label}({self._local_model if label == 'LOCAL' else self.cloud_config['model']}) 调用失败: {error_msg}，正在尝试降级...")
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

        if not self.cloud_available:
            order = ["LOCAL"]
        elif not self.local_available:
            order = ["CLOUD"]
        elif tier in ("PREMIUM", "LONG_CONTEXT"):
            order = ["CLOUD", "LOCAL"]  # 高级任务云端优先
        else:
            order = ["LOCAL", "CLOUD"]  # 日常对话本地优先，省 Token




        last_error = None
        for label in order:
            try:
                if label == "LOCAL":
                    print(f"🏠 正在并行启动本地流式模型 ({self._local_model}) @ {self._local_api_url}...")
                    async with self._local_semaphore:


                        async for chunk in self._call_ollama_stream(messages):
                            yield chunk
                        return # 成功完成则退出
                else:
                    if not self.cloud_available: continue
                    kwargs = {
                        "model": self.cloud_config["model"],
                        "messages": messages,
                        "stream": True
                    }
                    if tools:
                        kwargs["tools"] = tools
                    
                    response = await self.cloud_client.chat.completions.create(**kwargs)
                    async for chunk in response:
                        yield chunk
                    return
            except (Exception, asyncio.CancelledError) as e:
                import traceback
                error_msg = f"{type(e).__name__}: {str(e)}"
                logger.warning(f"流式模型 {label} 启动失败: {error_msg}，尝试降级...")
                # logger.debug(traceback.format_exc())
                last_error = error_msg
                continue
        
        raise Exception(f"所有流式算力层级均彻底不可用。最后一次错误: {last_error}")






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
        # Docker 沙盒代理
        self.docker_sandbox = DockerSandboxAgent()
        
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

    async def chat(self, userInput: str, tier: str = "LOCAL") -> AsyncGenerator[str, None]:
        """
        核心 ReAct 循环（流式版）：接收用户输入，产生流式回复块。
        tier 决定路由策略：LOCAL=本地优先（省 Token），PREMIUM=云端优先（支持工具调用）。
        """
        if "main" not in self.memories:
            self.memories["main"] = [{"role": "system", "content": self.systemPrompt}]
            
        self.memories["main"].append({"role": "user", "content": userInput})
        MAX_ITERATIONS = 10
        for _ in range(MAX_ITERATIONS):
            # 动态截断上下文，防止超长报错 (如 DeepSeek 128k 限制)
            self._truncate_memory("main")
            
            kwargs: dict = {
                "messages": self.memories["main"],
            }
            # tier 由外部传入，决定本地/云端优先级

            
            fullContent = ""
            toolCallsDict = {}  # 用于按索引累计 tool_calls 数据

            response = self.unified_client.generate_stream(
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
                    # 对工具输出进行长度保护
                    MAX_TOOL_OUTPUT = 30000
                    if len(resultText) > MAX_TOOL_OUTPUT:
                        resultText = resultText[:MAX_TOOL_OUTPUT] + f"\n\n...(输出过长，已截断前 {MAX_TOOL_OUTPUT} 字符数据)"
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

    def saveAllMemories(self):
        """保存当前所有已加载的上下文记忆，并清理沙盒"""
        for context_id in list(self.memories.keys()):
            self.saveMemory(context_id)
        # 退出清理 Docker 沙盒
        if hasattr(self, "docker_sandbox"):
            self.docker_sandbox.cleanup_all()
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
            content = chunk.choices[0].delta.content if hasattr(chunk.choices[0].delta, "content") else ""
            if content:
                yield content

    async def deploy_project(self, repo_url: str) -> AsyncGenerator[str, None]:
        """
        一键部署项目到 Docker 沙盒
        """
        owner, repo = self._parse_github_url(repo_url)
        if not owner:
            yield f"❌ 无效的 GitHub URL: {repo_url}"
            return

        yield f"🔍 正在初始化 [{repo}] 的部署流程...\n"
        
        # 1. 加载部署上下文
        yield "📂 正在扫描项目文件以提取技术栈...\n"
        context_data = await self.adaptive_load_data(repo_url, expert_config=EXPERT_REGISTRY["Deployment_Executor"])
        
        # 2. 调用部署专家生成 Dockerfile
        yield "🤖 部署专家正在编写专属 Dockerfile...\n"
        dockerfile_content = await self.unified_client.generate(
            "PREMIUM", 
            EXPERT_REGISTRY["Deployment_Executor"]["prompt"], 
            f"请为以下项目数据生成最优 Dockerfile：\n\n{context_data}"
        )
        
        # 剥离可能的 markdown 标记
        if "```" in dockerfile_content:
            match = re.search(r"```(?:dockerfile)?\s*(.*?)\s*```", dockerfile_content, re.DOTALL)
            if match:
                dockerfile_content = match.group(1)
        
        # 3. 执行沙盒部署
        yield "🐳 正在启动本地 Docker 沙盒进行构建与部署...\n"
        
        # 由于 deploy_in_sandbox 现在是生成器，我们需要在线程中迭代它
        # 或者在主线程迭代，但 docker-py 的 build 是同步阻塞的
        # 这里我们直接迭代以简化逻辑，AsyncGenerator 会确保流式输出
        try:
            for event in self.docker_sandbox.deploy_in_sandbox(repo, dockerfile_content, repo_url):
                evt_type = event.get("type")
                msg = event.get("message", "")
                
                if evt_type == "progress":
                    yield f"  {msg}\n"
                elif evt_type == "log":
                    # 仅显示非空日志
                    if msg:
                        yield f"    ┃ {msg}\n"
                elif evt_type == "success":
                    ports_str = " | ".join(event.get("ports", []))
                    # 将部署成功的信息记录到主记忆中，以便后续对话感知
                    self.memories["main"].append({
                        "role": "assistant", 
                        "content": f"【系统通知】项目已成功部署。映射端口：{ports_str} (容器ID: {event['container_id']})"
                    })
                    yield f"\n✅ **部署成功！**\n"
                    yield f"- **端口映射**: `{ports_str}`\n"
                    yield f"- **容器 ID**: `{event['container_id']}`\n"
                    yield f"- **项目名称**: {repo}\n"
                    
                    if event.get("logs"):
                        yield f"\n📋 **启动日志摘要 (前10行)**:\n```\n{event['logs']}\n```\n"
                    
                    yield "💡 温馨提示：容器仅在 Agent 运行时存活，关闭程序或使用 /clear 将自动销毁。\n"
                    yield "如果无法访问，请检查日志确保应用已绑定到 0.0.0.0 并正确监听端口。\n\n"
                elif evt_type == "error":
                    yield f"\n❌ **部署失败**\n"
                    yield f"- 原因: {msg}\n"
                    if event.get("details"):
                        yield f"- 详情: \n```\n{event['details']}\n```\n"
                    return
        except Exception as e:
            yield f"❌ **部署过程发生异常**: {str(e)}\n"
