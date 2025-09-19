"""
简化的负载均衡系统
- 异步轮询任务状态
- 简单的服务器选择策略
- 超时重分配机制
"""

import asyncio
import time
import aiohttp
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from enum import Enum

class ServerStatus(Enum):
    UNUSED = "unused"      # 从未使用过
    IDLE = "idle"          # 当前闲置
    BUSY = "busy"          # 正在处理任务
    FAILED = "failed"      # 最近失败过

class TaskStatus(Enum):
    PENDING = "pending"    # 等待分配
    ASSIGNED = "assigned"  # 已分配
    PROCESSING = "processing"  # 处理中
    COMPLETED = "completed"    # 已完成
    FAILED = "failed"      # 失败
    TIMEOUT = "timeout"    # 超时

@dataclass
class Server:
    id: int
    name: str
    url: str
    status: ServerStatus = ServerStatus.UNUSED
    last_used: float = 0
    failed_count: int = 0
    completed_tasks: int = 0
    total_time: float = 0

@dataclass
class Task:
    file_id: str
    filename: str
    status: TaskStatus = TaskStatus.PENDING
    assigned_server: Optional[int] = None
    assigned_time: float = 0
    retry_count: int = 0
    failed_servers: Set[int] = None
    
    def __post_init__(self):
        if self.failed_servers is None:
            self.failed_servers = set()

class SimpleLoadBalancer:
    def __init__(self, servers: List[Dict], timeout: int = 300, poll_interval: int = 10):
        self.servers = [Server(i, s['name'], s['url']) for i, s in enumerate(servers)]
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.tasks: Dict[str, Task] = {}
        self.task_queue = asyncio.Queue()
        self.completed_tasks = 0
        self.total_tasks = 0
        
    def add_task(self, file_id: str, filename: str):
        """添加任务到队列"""
        task = Task(file_id, filename)
        self.tasks[file_id] = task
        self.task_queue.put_nowait(file_id)
        self.total_tasks += 1
        print(f"📝 添加任务: {filename} (ID: {file_id})")
    
    def select_best_server(self, task: Task) -> Optional[Server]:
        """选择最优服务器：未用过优先 -> 最久闲置优先"""
        available_servers = []
        
        for server in self.servers:
            # 跳过失败的服务器（除非所有服务器都失败过）
            if server.id in task.failed_servers:
                continue
                
            # 跳过忙碌的服务器
            if server.status == ServerStatus.BUSY:
                continue
                
            available_servers.append(server)
        
        if not available_servers:
            # 如果所有服务器都不可用，选择失败次数最少的
            available_servers = [s for s in self.servers if s.status != ServerStatus.BUSY]
            if not available_servers:
                return None
        
        # 优先选择未使用过的服务器
        unused_servers = [s for s in available_servers if s.status == ServerStatus.UNUSED]
        if unused_servers:
            return unused_servers[0]
        
        # 其次选择闲置时间最长的服务器
        idle_servers = [s for s in available_servers if s.status == ServerStatus.IDLE]
        if idle_servers:
            return max(idle_servers, key=lambda s: s.last_used)
        
        # 最后选择失败次数最少的服务器
        return min(available_servers, key=lambda s: s.failed_count)
    
    async def assign_task(self, task: Task, server: Server) -> bool:
        """分配任务到服务器"""
        try:
            # 更新任务状态
            task.status = TaskStatus.ASSIGNED
            task.assigned_server = server.id
            task.assigned_time = time.time()
            
            # 更新服务器状态
            server.status = ServerStatus.BUSY
            server.last_used = time.time()
            
            print(f"📤 分配任务: {task.filename} → {server.name}")
            
            # 启动异步轮询
            asyncio.create_task(self.poll_task_status(task, server))
            
            return True
            
        except Exception as e:
            print(f"❌ 分配任务失败: {task.filename} → {server.name}, 错误: {e}")
            return False
    
    async def poll_task_status(self, task: Task, server: Server):
        """异步轮询任务状态"""
        start_time = time.time()
        
        while time.time() - start_time < self.timeout:
            try:
                # 这里应该调用实际的TTS API来检查任务状态
                # 为了演示，我们模拟一个简单的检查
                success = await self.check_task_status(task, server)
                
                if success is not None:
                    if success:
                        await self.handle_task_completion(task, server, True)
                    else:
                        await self.handle_task_completion(task, server, False)
                    return
                    
            except Exception as e:
                print(f"⚠️ 轮询任务状态失败: {task.filename}, 错误: {e}")
            
            await asyncio.sleep(self.poll_interval)
        
        # 超时处理
        print(f"⏰ 任务超时: {task.filename} (服务器: {server.name})")
        await self.handle_task_timeout(task, server)
    
    async def check_task_status(self, task: Task, server: Server) -> Optional[bool]:
        """检查任务状态 - 这里需要实现实际的API调用"""
        # 模拟API调用
        await asyncio.sleep(1)
        
        # 这里应该调用实际的TTS API
        # 返回 None 表示还在处理中
        # 返回 True 表示成功
        # 返回 False 表示失败
        
        # 为了演示，我们随机返回结果
        import random
        if random.random() < 0.1:  # 10% 概率返回结果
            return random.random() < 0.8  # 80% 成功率
        return None
    
    async def handle_task_completion(self, task: Task, server: Server, success: bool):
        """处理任务完成"""
        processing_time = time.time() - task.assigned_time
        
        if success:
            task.status = TaskStatus.COMPLETED
            server.completed_tasks += 1
            server.total_time += processing_time
            server.status = ServerStatus.IDLE
            self.completed_tasks += 1
            
            print(f"✅ 任务完成: {task.filename} (服务器: {server.name}, 耗时: {processing_time:.2f}秒)")
        else:
            task.status = TaskStatus.FAILED
            task.failed_servers.add(server.id)
            server.failed_count += 1
            server.status = ServerStatus.FAILED
            
            print(f"❌ 任务失败: {task.filename} (服务器: {server.name}, 耗时: {processing_time:.2f}秒)")
            
            # 如果重试次数未超限，重新加入队列
            if task.retry_count < 3:
                task.retry_count += 1
                task.status = TaskStatus.PENDING
                task.assigned_server = None
                await self.task_queue.put(task.file_id)
                print(f"🔄 重试任务: {task.filename} (第{task.retry_count}次重试)")
        
        # 触发下一个任务分配
        await self.process_next_task()
    
    async def handle_task_timeout(self, task: Task, server: Server):
        """处理任务超时"""
        task.status = TaskStatus.TIMEOUT
        task.failed_servers.add(server.id)
        server.failed_count += 1
        server.status = ServerStatus.FAILED
        
        print(f"⏰ 任务超时: {task.filename} (服务器: {server.name})")
        
        # 重新分配任务
        if task.retry_count < 3:
            task.retry_count += 1
            task.status = TaskStatus.PENDING
            task.assigned_server = None
            await self.task_queue.put(task.file_id)
            print(f"🔄 超时重试: {task.filename} (第{task.retry_count}次重试)")
        
        # 触发下一个任务分配
        await self.process_next_task()
    
    async def process_next_task(self):
        """处理下一个任务"""
        if self.task_queue.empty():
            return
        
        try:
            file_id = self.task_queue.get_nowait()
            task = self.tasks[file_id]
            
            if task.status != TaskStatus.PENDING:
                return
            
            server = self.select_best_server(task)
            if server:
                await self.assign_task(task, server)
            else:
                # 没有可用服务器，重新加入队列
                await self.task_queue.put(file_id)
                print(f"⚠️ 没有可用服务器，任务重新入队: {task.filename}")
                
        except asyncio.QueueEmpty:
            pass
    
    async def start_processing(self):
        """开始处理任务"""
        print(f"🚀 开始处理 {self.total_tasks} 个任务")
        
        # 初始分配任务
        initial_tasks = min(len(self.servers), self.total_tasks)
        for _ in range(initial_tasks):
            await self.process_next_task()
        
        # 等待所有任务完成
        while self.completed_tasks < self.total_tasks:
            await asyncio.sleep(1)
        
        print(f"🎉 所有任务处理完成: {self.completed_tasks}/{self.total_tasks}")
        
        # 输出服务器统计
        print("\n📊 服务器性能统计:")
        for server in self.servers:
            if server.completed_tasks > 0:
                avg_time = server.total_time / server.completed_tasks
                print(f"  🖥️ {server.name}:")
                print(f"    ✅ 完成任务: {server.completed_tasks} 个")
                print(f"    ⏱️ 平均耗时: {avg_time:.2f}秒/任务")
                print(f"    ❌ 失败次数: {server.failed_count} 次")

# 使用示例
async def main():
    # 模拟服务器列表
    servers = [
        {"name": "服务器1", "url": "http://server1:5050"},
        {"name": "服务器2", "url": "http://server2:5050"},
        {"name": "服务器3", "url": "http://server3:5050"},
    ]
    
    # 创建负载均衡器
    lb = SimpleLoadBalancer(servers, timeout=300, poll_interval=10)
    
    # 添加任务
    for i in range(10):
        lb.add_task(f"file_{i}", f"文件_{i}.md")
    
    # 开始处理
    await lb.start_processing()

if __name__ == "__main__":
    asyncio.run(main())
