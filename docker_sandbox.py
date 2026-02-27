import docker
import os
import random
import subprocess
import shutil
import logging

logger = logging.getLogger("mcp-agent")

class DockerSandboxAgent:
    def __init__(self):
        # 连接到本地 Docker Daemon
        try:
            self.client = docker.from_env()
        except Exception as e:
            logger.error(f"无法连接到 Docker: {e}")
            self.client = None
            
        self.sandbox_dir = os.path.abspath("./sandbox_workspace")
        os.makedirs(self.sandbox_dir, exist_ok=True)

    def clone_repo(self, repo_url, project_name):
        """克隆仓库到沙盒目录"""
        # 处理可能重复的路径名
        safe_name = project_name.replace("/", "_")
        project_dir = os.path.join(self.sandbox_dir, safe_name)
        
        if os.path.exists(project_dir):
            try:
                shutil.rmtree(project_dir)
            except Exception as e:
                logger.warning(f"无法清理旧目录: {e}")
        
        print(f"🚚 正在克隆仓库 {repo_url} 到沙盒...")
        result = subprocess.run(["git", "clone", "--depth", "1", repo_url, project_dir], capture_output=True, text=True)
        if result.returncode != 0:
            return False, result.stderr
        return True, project_dir

    def deploy_in_sandbox(self, project_name, dockerfile_content, repo_url):
        """
        供部署专家调用的核心工具：在独立沙盒中构建并运行代码。
        这是一个生成器，会 yield 进度信息。
        """
        if not self.client:
            yield {"type": "error", "message": "Docker 未运行或未安装，请确保 Docker Desktop 已启动。"}
            return

        # 1. 克隆代码
        yield {"type": "progress", "message": f"🚚 正在克隆仓库 {repo_url}..."}
        success, res = self.clone_repo(repo_url, project_name)
        if not success:
            yield {"type": "error", "message": "代码克隆失败", "details": res}
            return
            
        project_dir = res
        
        # 2. 将大模型写好的 Dockerfile 写入本地临时目录
        dockerfile_path = os.path.join(project_dir, "Dockerfile")
        with open(dockerfile_path, "w", encoding="utf-8") as f:
            f.write(dockerfile_content)

        try:
            # 3. 隔离构建镜像 (使用底层 API 获取流式日志)
            image_tag = f"agent-sandbox-{project_name.lower().replace('/', '-')}"
            yield {"type": "progress", "message": f"⏳ 正在构建隔离镜像 [{image_tag}]..."}
            
            # 使用 API client 进行流式构建
            # decode=True 会自动将响应解析为字典
            for chunk in self.client.api.build(
                path=project_dir,
                tag=image_tag,
                rm=True,
                decode=True
            ):
                if 'stream' in chunk:
                    yield {"type": "log", "message": chunk['stream'].strip()}
                elif 'error' in chunk:
                    yield {"type": "error", "message": "镜像构建失败", "details": chunk['error']}
                    return

            # 4. 动态分配随机可用端口并启动容器
            host_port = random.randint(10000, 20000)
            
            yield {"type": "progress", "message": f"🚀 正在启动容器，映射端口 {host_port}..."}
            container_name = f"sandbox_{project_name.lower().replace('/', '_')}_{random.randint(100, 999)}"
            
            # 由于 build 之后镜像已经存在，可以通过 tag 运行
            container = self.client.containers.run(
                image_tag,
                detach=True,
                mem_limit="1g",
                ports={
                    '80/tcp': host_port, 
                    '8080/tcp': host_port, 
                    '3000/tcp': host_port,
                    '5000/tcp': host_port
                },
                name=container_name
            )
            
            yield {
                "type": "success",
                "message": f"✅ 项目已在沙盒中成功启动！",
                "container_id": container.short_id,
                "access_url": f"http://localhost:{host_port}"
            }

        except Exception as e:
            logger.error(f"沙盒部署异常: {e}")
            yield {"type": "error", "message": "沙盒部署异常", "details": str(e)}

    def destroy_sandbox(self, container_id, project_name):
        """
        阅后即焚：销毁指定项目的容器、镜像和本地临时文件
        """
        print(f"🧹 正在清理 [{project_name}] 的沙盒资源...")
        try:
            # 1. 停止并删除容器
            try:
                container = self.client.containers.get(container_id)
                container.stop()
                container.remove(v=True) 
            except Exception as e:
                logger.warning(f"容器销毁失败(可能已由于 auto_remove 自动删除): {e}")
            
            # 2. 删除构建的专属镜像 (可选，根据磁盘压力决定)
            # image_tag = f"agent-sandbox-{project_name.lower().replace('/', '-')}"
            # try:
            #     self.client.images.remove(image_tag, force=True)
            # except:
            #     pass
                
            # 3. 清理本地临时源码
            safe_name = project_name.replace("/", "_")
            project_dir = os.path.join(self.sandbox_dir, safe_name)
            if os.path.exists(project_dir):
                shutil.rmtree(project_dir)
                
            return {"status": "success", "message": "✅ 沙盒已彻底销毁，本地资源已释放"}
        except Exception as e:
            return {"status": "error", "message": f"清理失败: {str(e)}"}

    def system_prune(self):
        """
        全局大扫除：清理所有悬空镜像、停止的容器以及本地源码缓存
        """
        if not self.client: return
        
        # 1. Docker 资源清理
        self.client.containers.prune()
        self.client.images.prune() 
        self.client.networks.prune()
        
        # 2. 本地源码清理
        if os.path.exists(self.sandbox_dir):
            try:
                shutil.rmtree(self.sandbox_dir)
                os.makedirs(self.sandbox_dir, exist_ok=True)
                local_msg = "，本地源码工作区已重置"
            except Exception as e:
                local_msg = f"，但本地源码清理失败: {e}"
        else:
            local_msg = ""
            
        return f"✅ 宿主机 Docker 垃圾清理完成{local_msg}"

    def cleanup_all(self):
        """退出时清理所有正在运行的沙盒容器"""
        if not self.client: return
        containers = self.client.containers.list(all=True)
        for c in containers:
            if c.name.startswith("sandbox_"):
                try:
                    print(f"💀 发现残留容器 {c.name}，正在回收...")
                    c.stop()
                    c.remove(v=True)
                except:
                    pass
        # 清理临时目录
        if os.path.exists(self.sandbox_dir):
            try:
                shutil.rmtree(self.sandbox_dir)
                os.makedirs(self.sandbox_dir, exist_ok=True)
            except:
                pass
