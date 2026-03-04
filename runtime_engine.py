"""
AOS P2: RuntimeEngine (运行时引擎)
支持“生成 Dockerfile + 沙箱部署 + 端口审计”闭环。
"""

import os
import aiohttp
import asyncio
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class RuntimeEngine:
    def __init__(self, docker_client=None):
        self.client = docker_client

    def generate_dockerfile(self, tech_stack: str, entry_point: str) -> str:
        """根据技术栈识别结果生成 Dockerfile"""
        tech_stack = tech_stack.lower()
        if "python" in tech_stack or "flask" in tech_stack or "fastapi" in tech_stack:
            return f"""FROM python:3.10-slim
WORKDIR /app
COPY . /app
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
EXPOSE 8080
CMD ["python", "{entry_point}"]
"""
        elif "node" in tech_stack or "react" in tech_stack or "vue" in tech_stack:
            return f"""FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "start"]
"""
        else:
            # Fallback
            return f"""FROM ubuntu:22.04
WORKDIR /app
COPY . /app
CMD ["echo", "Unsupported tech stack"]
"""

    async def deploy_workspace(self, workspace_path: str, dockerfile_content: str, port_bindings: dict = None) -> dict:
        """在 Docker 沙盒中部署项目，返回 {container_id, ports}"""
        if not self.client:
            return {"status": "error", "message": "Docker client not initialized"}
        
        try:
            # 1. Write Dockerfile
            df_path = os.path.join(workspace_path, "Dockerfile")
            with open(df_path, "w", encoding="utf-8") as f:
                f.write(dockerfile_content)
                
            logger.info("🛠️ [RuntimeEngine] 已生成 Dockerfile: %s", df_path)
            
            # 2. Build Image
            image_tag = f"aos_runtime_{os.path.basename(workspace_path).lower()}"
            logger.info("🐳 [RuntimeEngine] 正在构建镜像 %s...", image_tag)
            
            # Assuming docker_client is a docker py client
            def build_sync():
                image, logs = self.client.images.build(path=workspace_path, rm=True, tag=image_tag)
                return image
                
            await asyncio.to_thread(build_sync)
            
            # 3. Run Container
            logger.info("🚀 [RuntimeEngine] 正在启动容器...")
            
            def run_sync():
                # bindings format: {'8080/tcp': 8080}
                bindings = port_bindings or {'8080/tcp': None, '3000/tcp': None} 
                container = self.client.containers.run(
                    image_tag,
                    detach=True,
                    ports=bindings,
                    mem_limit="512m",
                    cpu_quota=50000,
                    network_mode="bridge"
                )
                return container

            container = await asyncio.to_thread(run_sync)
            
            # Need to reload to get assigned host ports
            def reload_sync():
                container.reload()
                return container.ports
                
            mapped_ports = await asyncio.to_thread(reload_sync)
            
            logger.info("✅ [RuntimeEngine] 部署成功, 容器ID: %s, 端口映射: %s", container.short_id, mapped_ports)
            
            return {
                "status": "success",
                "container_id": container.id,
                "short_id": container.short_id,
                "ports": mapped_ports
            }
            
        except Exception as e:
            logger.error("❌ [RuntimeEngine] 部署失败: %s", e)
            return {"status": "error", "message": str(e)}

    async def port_probe(self, host: str, port: int, timeout: int = 10, path: str = "/") -> dict:
        """HTTP 健康探针：只有 200 才算 PASS"""
        url = f"http://{host}:{port}{path}"
        logger.info("📡 [RuntimeEngine] 正在探测 HTTP 端口: %s", url)
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as response:
                    status = response.status
                    if status == 200:
                        logger.info("✅ [RuntimeEngine] 探针 PASS (200 OK)")
                        return {"status": "PASS", "code": status, "message": "Service is up and returning 200 OK"}
                    else:
                        logger.warning("⚠️ [RuntimeEngine] 探针 FAIL (Status: %d)", status)
                        return {"status": "FAIL", "code": status, "message": f"Service returned status {status}"}
        except Exception as e:
            logger.error("❌ [RuntimeEngine] 探针 FAIL (Exception): %s", e)
            return {"status": "FAIL", "code": 0, "message": str(e)}
