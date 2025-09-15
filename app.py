import os
import re
import sys
import uuid
import time
import json
import asyncio
import aiohttp
import threading
import zipfile
import io
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
import requests
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'md'}
app.secret_key = 'super-secret-key'  # 生产环境请替换为随机字符串

# 确保目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# 存储批量处理状态
batch_status = {}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def safe_filename(filename):
    """安全的文件名处理，支持中文字符"""
    import re
    import unicodedata
    
    # 如果secure_filename能正确处理，直接使用
    secure_name = secure_filename(filename)
    if secure_name and secure_name != filename:
        # 如果secure_filename改变了文件名，说明原文件名有问题
        # 但我们需要保留中文字符，所以使用自定义处理
        pass
    else:
        # secure_filename没有改变文件名，说明文件名是安全的
        return filename
    
    # 自定义处理：保留中文字符和基本ASCII字符
    # 移除或替换危险字符，但保留中文
    safe_chars = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', filename)
    
    # 移除前后空格和点
    safe_chars = safe_chars.strip('. ')
    
    # 限制长度
    if len(safe_chars) > 100:
        safe_chars = safe_chars[:100]
    
    # 确保不为空
    if not safe_chars:
        safe_chars = "file"
    
    return safe_chars

def generate_batch_directory(custom_name=None):
    """生成批量处理目录名"""
    if custom_name and custom_name.strip():
        # 使用自定义名称
        clean_name = clean_directory_name(custom_name.strip())
        return clean_name
    else:
        # 使用随机名称
        timestamp = int(time.time())
        random_id = str(uuid.uuid4())[:8]
        return f"batch_{timestamp}_{random_id}"

def clean_directory_name(name):
    """清理目录名称，移除非法字符"""
    import re
    # 移除或替换非法字符
    clean_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # 移除前后空格和点
    clean_name = clean_name.strip('. ')
    # 限制长度
    if len(clean_name) > 50:
        clean_name = clean_name[:50]
    # 确保不为空
    if not clean_name:
        clean_name = "custom_batch"
    return clean_name

def clean_text(text, options=None):
    """清理文本，移除 Markdown 语法、表情符号、URL 链接等"""
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    
    cleaned_text = text
    options = options or {}
    
    # 移除 Markdown 语法
    if options.get('remove_markdown', True):
        # 移除图片链接
        cleaned_text = re.sub(r'!\[.*?\]\(.*?\)', '', cleaned_text)
        # 移除链接，保留文本内容
        cleaned_text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', cleaned_text)
        # 移除粗体标记
        cleaned_text = re.sub(r'\*\*(.*?)\*\*', r'\1', cleaned_text)
        cleaned_text = re.sub(r'__(.*?)__', r'\1', cleaned_text)
        # 移除斜体标记
        cleaned_text = re.sub(r'\*(.*?)\*', r'\1', cleaned_text)
        cleaned_text = re.sub(r'_(.*?)_', r'\1', cleaned_text)
        # 移除代码块标记
        cleaned_text = re.sub(r'`([^`]+)`', r'\1', cleaned_text)
        # 移除标题标记
        cleaned_text = re.sub(r'^#{1,6}\s*', '', cleaned_text, flags=re.MULTILINE)
        # 移除列表标记
        cleaned_text = re.sub(r'^\s*[-*+]\s*', '', cleaned_text, flags=re.MULTILINE)
        # 移除数字列表标记
        cleaned_text = re.sub(r'^\s*\d+\.\s*', '', cleaned_text, flags=re.MULTILINE)
    
    # 移除 URL 链接
    if options.get('remove_urls', True):
        cleaned_text = re.sub(r'https?://[^\s]+', '', cleaned_text)
    
    # 移除表情符号
    if options.get('remove_emoji', True):
        # 使用更精确的表情符号范围，避免误删中文字符
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map symbols
            "\U0001F1E0-\U0001F1FF"  # flags (iOS)
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-a
            "\U00002600-\U000026FF"  # miscellaneous symbols
            "\U00002700-\U000027BF"  # dingbats
            "]+", flags=re.UNICODE)
        cleaned_text = emoji_pattern.sub('', cleaned_text)
    
    # 移除引用标记
    if options.get('remove_citation_numbers', True):
        cleaned_text = re.sub(r'\[\d+\]', '', cleaned_text)
        cleaned_text = re.sub(r'【\d+】', '', cleaned_text)
    
    # 合并为单行文本
    if options.get('remove_line_breaks', True):
        # 移除所有换行符
        cleaned_text = re.sub(r'(\r\n|\n|\r)', ' ', cleaned_text)
        # 合并多个连续空格为单个空格
        cleaned_text = re.sub(r'\s+', ' ', cleaned_text)
    else:
        # 只合并非换行的连续空格
        cleaned_text = re.sub(r'[ \t]+', ' ', cleaned_text)
    
    return cleaned_text.strip()

async def async_text_to_speech(session, text, output_path, voice="zh-CN-XiaoxiaoNeural", speed=1.0, api_url=None, api_key=None):
    """异步调用TTS API转换文本为语音"""
    # 使用传入的API信息，如果没有则使用默认值
    if not api_url:
        api_url = "http://127.0.0.1:5050/v1/audio/speech"
    else:
        # 确保URL格式正确
        if not api_url.endswith('/v1/audio/speech'):
            api_url = api_url.rstrip('/') + '/v1/audio/speech'
    
    if not api_key:
        api_key = "b77cf8cf852f4080bb56a4adcfc6a685"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "tts-1",
        "input": text,  # 直接发送原始文本，让API端处理清理
        "voice": voice,
        "speed": speed,
        "cleaning_options": {
            "remove_markdown": True,
            "remove_emoji": True,
            "remove_urls": True,
            "remove_line_breaks": True,
            "remove_citation_numbers": True
        }
    }
    
    try:
        # 根据文本长度动态调整超时时间
        text_length = len(text)
        if text_length < 10000:  # 小于1万字符
            timeout_seconds = 300  # 5分钟
        elif text_length < 50000:  # 1-5万字符
            timeout_seconds = 600  # 10分钟
        elif text_length < 100000:  # 5-10万字符
            timeout_seconds = 900  # 15分钟
        else:  # 超过10万字符
            timeout_seconds = 1200  # 20分钟
        
        print(f"📏 文本长度: {text_length:,} 字符，设置超时: {timeout_seconds}秒")
        
        # 异步发送请求并获取响应
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with session.post(api_url, headers=headers, json=data, timeout=timeout) as response:
            if response.status == 200:
                # 异步读取响应内容
                content = await response.read()
                
                # 保存音频文件
                with open(output_path, 'wb') as f:
                    f.write(content)
                
                return True
            else:
                print(f"TTS API返回错误状态码: {response.status} ({api_url})", file=sys.stderr)
                return False
                
    except asyncio.TimeoutError:
        print(f"⏰ TTS转换超时 ({api_url}) - 文本长度: {len(text):,} 字符", file=sys.stderr)
        return False
    except aiohttp.ClientConnectorError as e:
        print(f"🔌 连接错误 ({api_url}): {str(e)}", file=sys.stderr)
        return False
    except aiohttp.ClientError as e:
        print(f"🌐 网络错误 ({api_url}): {str(e)}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"💥 TTS转换失败 ({api_url}): {str(e)}", file=sys.stderr)
        return False

def text_to_speech(text, output_path, voice="zh-CN-XiaoxiaoNeural", speed=1.0, api_url=None, api_key=None):
    """同步版本的TTS调用（保持向后兼容）"""
    # 使用传入的API信息，如果没有则使用默认值
    if not api_url:
        api_url = "http://127.0.0.1:5050/v1/audio/speech"
    else:
        # 确保URL格式正确
        if not api_url.endswith('/v1/audio/speech'):
            api_url = api_url.rstrip('/') + '/v1/audio/speech'
    
    if not api_key:
        api_key = "b77cf8cf852f4080bb56a4adcfc6a685"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "tts-1",
        "input": text,  # 直接发送原始文本，让API端处理清理
        "voice": voice,
        "speed": speed,
        "cleaning_options": {
            "remove_markdown": True,
            "remove_emoji": True,
            "remove_urls": True,
            "remove_line_breaks": True,
            "remove_citation_numbers": True
        }
    }
    
    try:
        # 发送请求并获取响应
        response = requests.post(api_url, headers=headers, json=data)
        response.raise_for_status()
        
        # 保存音频文件
        with open(output_path, 'wb') as f:
            f.write(response.content)
        
        return True
    except Exception as e:
        print(f"TTS转换失败 ({api_url}): {str(e)}", file=sys.stderr)
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_files():
    if 'files' not in request.files:
        return jsonify({'error': '没有选择文件'}), 400
    
    files = request.files.getlist('files')
    # 获取声音和语速参数
    voice = request.form.get('voice', 'zh-CN-XiaoxiaoNeural')
    speed = float(request.form.get('speed', 1.0))
    custom_directory = request.form.get('custom_directory', '').strip()
    
    # 获取API服务器信息
    api_servers_json = request.form.get('api_servers', '[]')
    concurrency = int(request.form.get('concurrency', 1))
    
    # 解析API服务器列表
    try:
        api_servers = json.loads(api_servers_json)
        # 过滤出启用的服务器
        enabled_servers = [server for server in api_servers if server.get('enabled', True)]
        if not enabled_servers:
            return jsonify({'error': '没有可用的API服务器'}), 400
        
        # 调试日志：显示启用的服务器
        print(f"🔧 启用的API服务器列表:")
        for i, server in enumerate(enabled_servers):
            print(f"  {i+1}. {server.get('name', 'Unknown')} - {server.get('url', 'No URL')}")
        print(f"📊 总共 {len(enabled_servers)} 个启用的服务器，并发度: {concurrency}")
        
    except json.JSONDecodeError:
        return jsonify({'error': 'API服务器配置格式错误'}), 400
    
    # 生成批量处理目录
    batch_dir = generate_batch_directory(custom_directory)
    batch_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], batch_dir)
    
    # 创建批量处理目录
    os.makedirs(batch_upload_dir, exist_ok=True)
    
    # 初始化批量状态
    batch_id = str(uuid.uuid4())
    valid_files = [f for f in files if f and allowed_file(f.filename)]
    batch_status[batch_id] = {
        'total_files': len(valid_files),
        'completed_files': 0,
        'current_file': 0,
        'files': {},
        'server_statuses': {},  # 添加服务器状态跟踪
        'upload_dir': batch_upload_dir  # 保存上传目录路径
    }
    
    # 先保存所有文件并初始化状态
    for file in valid_files:
        filename = safe_filename(file.filename)
        file_id = f"{batch_id}_{filename}"
        
        # 保存上传的Markdown文件到批量目录
        md_path = os.path.join(batch_upload_dir, filename)
        file.save(md_path)
        
        # 初始化文件状态
        batch_status[batch_id]['files'][file_id] = {
            'filename': filename,
            'status': 'waiting',
            'progress': 0,
            'stage': '等待处理'
        }
    
    # 启动后台处理任务
    import threading
    thread = threading.Thread(target=run_async_processing, args=(batch_id, batch_upload_dir, voice, speed, enabled_servers, concurrency))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'batch_id': batch_id,
        'batch_directory': batch_dir,
        'total_files': len(valid_files)
    })

def run_async_processing(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files=None):
    """运行异步处理的主函数"""
    try:
        # 创建新的事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 运行异步处理
        loop.run_until_complete(process_files_async(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files))
        
    except Exception as e:
        print(f"异步处理异常: {str(e)}", file=sys.stderr)
    finally:
        loop.close()

async def process_files_async(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files=None):
    """异步处理文件，使用真正的动态负载均衡"""
    if batch_id not in batch_status:
        return
    
    batch_info = batch_status[batch_id]
    
    # 如果指定了特定文件，只处理这些文件；否则处理所有文件
    if specific_files:
        files_to_process = specific_files
        print(f"🔄 重试模式: 只处理指定的 {len(files_to_process)} 个文件")
    else:
        files_to_process = list(batch_info['files'].keys())
        print(f"🆕 全新处理: 处理所有 {len(files_to_process)} 个文件")
    
    # 调试日志：显示异步处理配置
    print(f"🚀 开始动态负载均衡处理:")
    print(f"  📁 批次ID: {batch_id}")
    print(f"  📄 文件数量: {len(files_to_process)}")
    print(f"  🖥️ 可用服务器: {len(api_servers)}")
    print(f"  ⚡ 并发度: {concurrency}")
    
    # 创建aiohttp会话，优化连接池设置
    connector = aiohttp.TCPConnector(
        limit=concurrency * 3,  # 总连接池限制
        limit_per_host=concurrency * 2,  # 每个主机的连接限制
        keepalive_timeout=60,  # 保持连接60秒
        enable_cleanup_closed=True  # 自动清理关闭的连接
    )
    # 会话级别的超时设置更宽松，因为单个请求的超时由请求级别控制
    timeout = aiohttp.ClientTimeout(total=1800)  # 30分钟总超时（会话级别）
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # 创建任务队列和服务器状态跟踪
        task_queue = asyncio.Queue()
        server_stats = {i: {'active_tasks': 0, 'completed_tasks': 0, 'total_time': 0} for i in range(len(api_servers))}
        
        # 初始化服务器状态
        for i in range(len(api_servers)):
            batch_info['server_statuses'][i] = {
                'name': api_servers[i]['name'],
                'status': 'idle',
                'load': 0,
                'max_load': concurrency,
                'completed_tasks': 0,
                'total_time': 0
            }
        
        # 将所有文件添加到队列
        for file_id in files_to_process:
            await task_queue.put(file_id)
        
        print(f"📤 创建任务队列: {len(files_to_process)} 个文件")
        
        # 创建真正的动态负载均衡处理器
        async def dynamic_load_balancer():
            """真正的动态负载均衡处理器 - 任务完成即分配新任务，失败任务自动重试"""
            start_time = time.time()
            total_tasks = len(files_to_process)
            completed_tasks = 0
            failed_tasks = []
            retry_queue = asyncio.Queue()  # 失败任务重试队列
            
            print(f"🚀 启动动态负载均衡器:")
            print(f"  📊 总任务数: {total_tasks}")
            print(f"  🖥️ 可用服务器: {len(api_servers)}")
            print(f"  ⚡ 每服务器并发度: {concurrency}")
            print(f"  🎯 理论最大并发: {len(api_servers) * concurrency}")
            
            # 创建任务完成回调函数
            async def on_task_completed(file_id, server_id, success, processing_time):
                nonlocal completed_tasks
                
                # 更新服务器统计
                server_stats[server_id]['active_tasks'] -= 1
                server_stats[server_id]['total_time'] += processing_time
                
                # 更新服务器状态
                current_load = server_stats[server_id]['active_tasks']
                batch_info['server_statuses'][server_id]['load'] = current_load
                batch_info['server_statuses'][server_id]['total_time'] = server_stats[server_id]['total_time']
                
                if current_load == 0:
                    batch_info['server_statuses'][server_id]['status'] = 'idle'
                elif current_load >= concurrency:
                    batch_info['server_statuses'][server_id]['status'] = 'full'
                else:
                    batch_info['server_statuses'][server_id]['status'] = 'busy'
                
                # 检查是否超时（超过5分钟认为超时）
                if processing_time > 300:
                    print(f"⏰ 任务超时检测: {file_id} 耗时 {processing_time:.2f}秒，可能存在问题")
                
                if success:
                    server_stats[server_id]['completed_tasks'] += 1
                    batch_info['server_statuses'][server_id]['completed_tasks'] = server_stats[server_id]['completed_tasks']
                    completed_tasks += 1
                    # 更新批次完成计数
                    batch_info['completed_files'] = completed_tasks
                    batch_info['current_file'] = completed_tasks
                    print(f"✅ 任务完成: {file_id} (服务器: {api_servers[server_id]['name']}, 耗时: {processing_time:.2f}秒)")
                else:
                    # 任务失败，加入重试队列（包含失败服务器信息）
                    failed_tasks.append(file_id)
                    retry_info = {
                        'file_id': file_id,
                        'failed_server_id': server_id,
                        'failed_server_name': api_servers[server_id]['name']
                    }
                    await retry_queue.put(retry_info)
                    print(f"❌ 任务失败: {file_id} (服务器: {api_servers[server_id]['name']}, 耗时: {processing_time:.2f}秒) - 加入重试队列")
                
                # 立即尝试分配新任务给这个服务器
                print(f"🎯 任务完成，检查服务器 {api_servers[server_id]['name']} 是否可以接收新任务 (当前负载: {current_load}/{concurrency})")
                print(f"📊 队列状态: 主队列={task_queue.qsize()}, 重试队列={retry_queue.qsize()}, 已完成={completed_tasks}/{total_tasks}")
                await assign_next_task(server_id)
                
                # 检查是否所有任务都已完成
                if completed_tasks >= total_tasks:
                    print(f"🎉 所有任务已完成！")
                    return
            
            # 任务分配函数
            async def assign_next_task(server_id):
                """为指定服务器分配下一个任务"""
                current_load = server_stats[server_id]['active_tasks']
                server_name = api_servers[server_id]['name']
                
                print(f"🔍 检查服务器 {server_name} 任务分配 (当前负载: {current_load}/{concurrency})")
                
                if current_load >= concurrency:
                    print(f"⚠️ 服务器 {server_name} 已满，跳过任务分配")
                    return  # 服务器已满
                
                # 优先从重试队列获取失败的任务
                file_id = None
                if not retry_queue.empty():
                    try:
                        retry_info = retry_queue.get_nowait()
                        file_id = retry_info['file_id']
                        failed_server_id = retry_info['failed_server_id']
                        failed_server_name = retry_info['failed_server_name']
                        
                        # 检查是否分配给不同的服务器
                        if server_id != failed_server_id:
                            print(f"🔄 重试任务: {file_id} → {server_name} (原失败服务器: {failed_server_name})")
                        else:
                            # 如果还是同一个服务器，放回队列等待其他服务器
                            await retry_queue.put(retry_info)
                            file_id = None
                            print(f"⚠️ 跳过重试: {file_id} 避免分配给同一失败服务器 {failed_server_name}")
                    except asyncio.QueueEmpty:
                        pass
                
                # 如果重试队列为空，从主队列获取
                if file_id is None and not task_queue.empty():
                    try:
                        file_id = task_queue.get_nowait()
                        print(f"📤 新任务: {file_id} → {server_name}")
                    except asyncio.QueueEmpty:
                        print(f"📭 服务器 {server_name} 无任务可分配")
                        return
                
                if file_id is not None:
                    # 更新服务器状态
                    server_stats[server_id]['active_tasks'] += 1
                    current_load = server_stats[server_id]['active_tasks']
                    batch_info['server_statuses'][server_id]['load'] = current_load
                    
                    if current_load >= concurrency:
                        batch_info['server_statuses'][server_id]['status'] = 'full'
                    else:
                        batch_info['server_statuses'][server_id]['status'] = 'busy'
                    
                    server_name = api_servers[server_id]['name']
                    print(f"🎯 动态分配: {file_id} → {server_name} (负载:{current_load}/{concurrency})")
                    print(f"✅ 任务分配确认: {file_id} 已成功分配给 {server_name}")
                    
                    # 启动任务
                    task = asyncio.create_task(
                        process_single_file_with_callback(
                            session, batch_id, batch_upload_dir, voice, speed, 
                            api_servers, file_id, server_id, server_stats, concurrency,
                            on_task_completed
                        )
                    )
                else:
                    # 没有任务可分配
                    print(f"📭 服务器 {server_name} 无任务可分配 (重试队列: {retry_queue.qsize()}, 主队列: {task_queue.qsize()})")
                    
                    # 如果所有队列都为空，检查是否可以退出
                    if task_queue.empty() and retry_queue.empty():
                        active_tasks = sum(server_stats[i]['active_tasks'] for i in range(len(api_servers)))
                        if active_tasks == 0:
                            print(f"🎯 所有任务已完成，所有服务器空闲")
                            return
            
            # 初始分配：为所有服务器分配初始任务
            print(f"🚀 开始初始任务分配...")
            for server_id in range(len(api_servers)):
                for _ in range(min(concurrency, total_tasks)):
                    await assign_next_task(server_id)
                    if task_queue.empty():
                        break
                print(f"  📤 服务器 {api_servers[server_id]['name']} 初始分配完成，当前负载: {server_stats[server_id]['active_tasks']}")
            
            print(f"📊 初始分配完成，剩余队列任务: {task_queue.qsize()}")
            
            # 等待所有任务完成（事件驱动，无需轮询）
            print(f"🎯 启动事件驱动负载均衡，等待任务完成...")
            
            # 创建任务完成等待器
            async def wait_for_completion():
                while completed_tasks < total_tasks:
                    # 检查是否有活跃任务
                    active_tasks = sum(server_stats[i]['active_tasks'] for i in range(len(api_servers)))
                    if active_tasks == 0 and task_queue.empty() and retry_queue.empty():
                        print(f"⚠️ 所有任务已完成但计数不匹配，强制退出")
                        break
                    await asyncio.sleep(0.5)  # 减少检查频率
            
            await wait_for_completion()
            
            total_time = time.time() - start_time
            print(f"🎉 动态负载均衡处理完成 (总耗时: {total_time:.2f}秒)")
            print(f"📊 最终统计: 完成 {completed_tasks}/{total_tasks} 个任务")
            
            # 输出详细的服务器统计信息
            print(f"📊 服务器性能统计:")
            for i, stats in server_stats.items():
                server_name = api_servers[i]['name']
                if stats['completed_tasks'] > 0:
                    avg_time = stats['total_time'] / stats['completed_tasks']
                    throughput = stats['completed_tasks'] / total_time if total_time > 0 else 0
                    print(f"  🖥️ {server_name}:")
                    print(f"    ✅ 完成任务: {stats['completed_tasks']} 个")
                    print(f"    ⏱️ 平均耗时: {avg_time:.2f}秒/任务")
                    print(f"    🚀 吞吐量: {throughput:.2f}任务/秒")
                    print(f"    📈 效率评分: {1.0/max(avg_time, 0.1):.2f}")
                else:
                    print(f"  🖥️ {server_name}: 未处理任务")
        
        # 运行动态负载均衡处理器
        await dynamic_load_balancer()
        
        # 统计最终结果
        success_count = sum(1 for file_id in files_to_process 
                           if batch_info['files'][file_id]['status'] == 'completed')
        
        print(f"🎉 异步处理完成: {success_count}/{len(files_to_process)} 个文件成功")
        print(f"📊 使用了 {len(api_servers)} 个服务器，并发度: {concurrency}")

async def process_single_file_with_callback(session, batch_id, batch_upload_dir, voice, speed, api_servers, file_id, server_id, server_stats, concurrency, callback):
    """异步处理单个文件，带回调机制"""
    start_time = time.time()
    
    try:
        batch_info = batch_status[batch_id]
        file_info = batch_info['files'][file_id]
        filename = file_info['filename']
        selected_server = api_servers[server_id]
        
        # 更新文件状态
        file_info['status'] = 'processing'
        file_info['progress'] = 10
        file_info['stage'] = '📖 读取文件...'
        
        # 读取完整的Markdown文件内容
        md_path = os.path.join(batch_upload_dir, filename)
        
        with open(md_path, 'r', encoding='utf-8') as f:
            full_text = f.read()
        
        file_info['progress'] = 20
        file_info['stage'] = '📤 准备发送到TTS API...'
        
        # 生成MP3文件路径
        mp3_filename = os.path.splitext(filename)[0] + '.mp3'
        mp3_path = os.path.join(batch_upload_dir, mp3_filename)
        
        file_info['progress'] = 30
        file_info['stage'] = f'🎵 正在转换 (服务器: {selected_server["name"]})...'
        
        # 调用异步TTS转换
        api_key = selected_server.get('apiKey', selected_server.get('api_key', ''))
        success = await async_text_to_speech(
            session, full_text, mp3_path, voice, speed, 
            selected_server['url'], api_key
        )
        
        end_time = time.time()
        processing_time = end_time - start_time
        
        if success:
            file_info['status'] = 'completed'
            file_info['progress'] = 100
            file_info['stage'] = '✅ 转换完成'
            print(f"✅ {filename} 转换成功 (服务器: {selected_server['name']}, 耗时: {processing_time:.2f}秒)")
        else:
            file_info['status'] = 'failed'
            file_info['progress'] = 100
            file_info['stage'] = f'❌ 转换失败 (服务器: {selected_server["name"]})'
            print(f"❌ {filename} 转换失败 (服务器: {selected_server['name']}, 耗时: {processing_time:.2f}秒)")
        
        # 调用回调函数
        await callback(file_id, server_id, success, processing_time)
        return success
            
    except Exception as e:
        end_time = time.time()
        processing_time = end_time - start_time
        
        print(f"💥 处理文件 {file_id} 时出错: {str(e)}", file=sys.stderr)
        if batch_id in batch_status:
            batch_info = batch_status[batch_id]
            if file_id in batch_info['files']:
                file_info = batch_info['files'][file_id]
                file_info['status'] = 'failed'
                file_info['progress'] = 100
                file_info['stage'] = f'💥 处理异常: {str(e)}'
        
        # 调用回调函数
        await callback(file_id, server_id, False, processing_time)
        return False

async def process_single_file_with_server_tracking(session, batch_id, batch_upload_dir, voice, speed, api_servers, file_id, server_id, server_stats, concurrency):
    """异步处理单个文件，带服务器状态跟踪"""
    start_time = time.time()
    
    try:
        batch_info = batch_status[batch_id]
        file_info = batch_info['files'][file_id]
        filename = file_info['filename']
        selected_server = api_servers[server_id]
        
        # 更新文件状态
        file_info['status'] = 'processing'
        file_info['progress'] = 10
        file_info['stage'] = '📖 读取文件...'
        
        # 读取完整的Markdown文件内容
        md_path = os.path.join(batch_upload_dir, filename)
        
        with open(md_path, 'r', encoding='utf-8') as f:
            full_text = f.read()
        
        file_info['progress'] = 20
        file_info['stage'] = '📤 准备发送到TTS API...'
        
        # 生成MP3文件路径
        mp3_filename = os.path.splitext(filename)[0] + '.mp3'
        mp3_path = os.path.join(batch_upload_dir, mp3_filename)
        
        file_info['progress'] = 30
        file_info['stage'] = f'🎵 正在转换 (服务器: {selected_server["name"]})...'
        
        # 调用异步TTS转换
        api_key = selected_server.get('apiKey', selected_server.get('api_key', ''))
        success = await async_text_to_speech(
            session, full_text, mp3_path, voice, speed, 
            selected_server['url'], api_key
        )
        
        # 更新服务器统计
        end_time = time.time()
        processing_time = end_time - start_time
        server_stats[server_id]['active_tasks'] -= 1
        server_stats[server_id]['completed_tasks'] += 1
        server_stats[server_id]['total_time'] += processing_time
        
        # 更新服务器状态
        if batch_id in batch_status:
            batch_info = batch_status[batch_id]
            current_load = server_stats[server_id]['active_tasks']
            batch_info['server_statuses'][server_id]['load'] = current_load
            batch_info['server_statuses'][server_id]['completed_tasks'] = server_stats[server_id]['completed_tasks']
            batch_info['server_statuses'][server_id]['total_time'] = server_stats[server_id]['total_time']
            
            if current_load == 0:
                batch_info['server_statuses'][server_id]['status'] = 'idle'
            elif current_load >= concurrency:
                batch_info['server_statuses'][server_id]['status'] = 'full'
            else:
                batch_info['server_statuses'][server_id]['status'] = 'busy'
        
        if success:
            file_info['status'] = 'completed'
            file_info['progress'] = 100
            file_info['stage'] = '✅ 转换完成'
            print(f"✅ {filename} 转换成功 (服务器: {selected_server['name']}, 耗时: {processing_time:.2f}秒)")
            return True
        else:
            file_info['status'] = 'failed'
            file_info['progress'] = 100
            file_info['stage'] = f'❌ 转换失败 (服务器: {selected_server["name"]})'
            print(f"❌ {filename} 转换失败 (服务器: {selected_server['name']}, 耗时: {processing_time:.2f}秒)")
            return False
            
    except Exception as e:
        # 更新服务器统计
        end_time = time.time()
        processing_time = end_time - start_time
        server_stats[server_id]['active_tasks'] -= 1
        server_stats[server_id]['completed_tasks'] += 1
        server_stats[server_id]['total_time'] += processing_time
        
        # 更新服务器状态
        if batch_id in batch_status:
            batch_info = batch_status[batch_id]
            current_load = server_stats[server_id]['active_tasks']
            batch_info['server_statuses'][server_id]['load'] = current_load
            batch_info['server_statuses'][server_id]['completed_tasks'] = server_stats[server_id]['completed_tasks']
            batch_info['server_statuses'][server_id]['total_time'] = server_stats[server_id]['total_time']
            
            if current_load == 0:
                batch_info['server_statuses'][server_id]['status'] = 'idle'
            elif current_load >= concurrency:
                batch_info['server_statuses'][server_id]['status'] = 'full'
            else:
                batch_info['server_statuses'][server_id]['status'] = 'busy'
        
        print(f"💥 处理文件 {file_id} 时出错: {str(e)}", file=sys.stderr)
        if batch_id in batch_status:
            batch_info = batch_status[batch_id]
            if file_id in batch_info['files']:
                file_info = batch_info['files'][file_id]
                file_info['status'] = 'failed'
                file_info['progress'] = 100
                file_info['stage'] = f'💥 处理异常: {str(e)}'
        return False

async def process_single_file_async(session, semaphore, batch_id, batch_upload_dir, voice, speed, api_servers, file_id):
    """异步处理单个文件"""
    async with semaphore:  # 控制并发数
        try:
            batch_info = batch_status[batch_id]
            file_info = batch_info['files'][file_id]
            filename = file_info['filename']
            
            # 更新文件状态
            file_info['status'] = 'processing'
            file_info['progress'] = 10
            file_info['stage'] = '📖 读取文件...'
            
            # 读取完整的Markdown文件内容
            md_path = os.path.join(batch_upload_dir, filename)
            
            with open(md_path, 'r', encoding='utf-8') as f:
                full_text = f.read()
            
            file_info['progress'] = 20
            file_info['stage'] = '📤 准备发送到TTS API...'
            
            # 生成MP3文件路径
            mp3_filename = os.path.splitext(filename)[0] + '.mp3'
            mp3_path = os.path.join(batch_upload_dir, mp3_filename)
            
            file_info['progress'] = 30
            file_info['stage'] = '⏳ 等待TTS处理中...'
            
            # 负载均衡：轮询选择服务器
            server_index = hash(file_id) % len(api_servers)  # 使用哈希确保一致性
            selected_server = api_servers[server_index]
            
            server_name = selected_server.get('name', 'Unknown')
            api_url = selected_server.get('url', '')
            api_key = selected_server.get('apiKey', '')
            
            # 调试日志：显示服务器分配
            print(f"🔄 文件 {filename} 分配给服务器: {server_name} ({api_url})")
            
            file_info['stage'] = f'⏳ 使用服务器 {server_name} 处理中...'
            
            # 异步调用TTS转换
            success = await async_text_to_speech(session, full_text, mp3_path, voice, speed, api_url, api_key)
            
            if success:
                file_info['progress'] = 90
                file_info['stage'] = '💾 保存音频文件...'
                
                file_info['status'] = 'completed'
                file_info['progress'] = 100
                file_info['stage'] = '✅ 转换完成'
                print(f"✅ {filename} 转换成功 (服务器: {server_name})")
            else:
                file_info['status'] = 'failed'
                file_info['progress'] = 100
                file_info['stage'] = f'❌ 转换失败 (服务器: {server_name})'
                print(f"❌ {filename} 转换失败 (服务器: {server_name})")
            
            # 更新完成计数
            batch_info['completed_files'] += 1
            batch_info['current_file'] = batch_info['completed_files']
            
            return success
            
        except Exception as e:
            file_info['status'] = 'failed'
            file_info['progress'] = 100
            file_info['stage'] = f'❌ 处理异常: {str(e)}'
            print(f"❌ {filename} 处理异常: {str(e)}")
            batch_info['completed_files'] += 1
            batch_info['current_file'] = batch_info['completed_files']
            return False

def process_files_with_load_balancing(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency):
    """使用负载均衡和并发处理文件"""
    if batch_id not in batch_status:
        return
    
    batch_info = batch_status[batch_id]
    files_to_process = list(batch_info['files'].keys())
    
    # 调试日志：显示负载均衡配置
    print(f"🚀 开始负载均衡处理:")
    print(f"  📁 批次ID: {batch_id}")
    print(f"  📄 文件数量: {len(files_to_process)}")
    print(f"  🖥️ 可用服务器: {len(api_servers)}")
    print(f"  ⚡ 并发度: {concurrency}")
    
    # 创建服务器轮询索引
    server_index = 0
    
    # 使用线程池进行并发处理
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import queue
    
    # 创建任务队列
    task_queue = queue.Queue()
    for file_id in files_to_process:
        task_queue.put(file_id)
    
    # 创建线程池
    max_workers = min(concurrency, len(files_to_process), len(api_servers))
    
    def process_single_file(file_id):
        """处理单个文件"""
        try:
            file_info = batch_info['files'][file_id]
            filename = file_info['filename']
            
            # 更新文件状态
            file_info['status'] = 'processing'
            file_info['progress'] = 10
            file_info['stage'] = '📖 读取文件...'
            
            # 读取完整的Markdown文件内容
            md_path = os.path.join(batch_upload_dir, filename)
            
            with open(md_path, 'r', encoding='utf-8') as f:
                full_text = f.read()
            
            file_info['progress'] = 20
            file_info['stage'] = '📤 准备发送到TTS API...'
            
            # 生成MP3文件路径
            mp3_filename = os.path.splitext(filename)[0] + '.mp3'
            mp3_path = os.path.join(batch_upload_dir, mp3_filename)
            
            file_info['progress'] = 30
            file_info['stage'] = '⏳ 等待TTS处理中...'
            
            # 负载均衡：轮询选择服务器
            nonlocal server_index
            selected_server = api_servers[server_index % len(api_servers)]
            server_index += 1
            
            server_name = selected_server.get('name', 'Unknown')
            api_url = selected_server.get('url', '')
            api_key = selected_server.get('apiKey', '')
            
            # 调试日志：显示服务器分配
            print(f"🔄 文件 {filename} 分配给服务器: {server_name} ({api_url})")
            
            file_info['stage'] = f'⏳ 使用服务器 {server_name} 处理中...'
            
            # 调用TTS转换
            success = text_to_speech(full_text, mp3_path, voice, speed, api_url, api_key)
            
            if success:
                file_info['progress'] = 90
                file_info['stage'] = '💾 保存音频文件...'
                
                file_info['status'] = 'completed'
                file_info['progress'] = 100
                file_info['stage'] = '✅ 转换完成'
                print(f"✅ {filename} 转换成功 (服务器: {server_name})")
            else:
                file_info['status'] = 'failed'
                file_info['progress'] = 100
                file_info['stage'] = f'❌ 转换失败 (服务器: {server_name})'
                print(f"❌ {filename} 转换失败 (服务器: {server_name})")
            
            return file_id, success
            
        except Exception as e:
            file_info['status'] = 'failed'
            file_info['progress'] = 100
            file_info['stage'] = f'❌ 处理异常: {str(e)}'
            print(f"❌ {filename} 处理异常: {str(e)}")
            return file_id, False
    
    # 使用线程池执行任务
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        future_to_file = {
            executor.submit(process_single_file, file_id): file_id 
            for file_id in files_to_process
        }
        
        # 处理完成的任务
        for future in as_completed(future_to_file):
            file_id, success = future.result()
            batch_info['completed_files'] += 1
            
            # 更新当前处理文件计数
            batch_info['current_file'] = batch_info['completed_files']
    
    print(f"🎉 批量处理完成: {batch_info['completed_files']}/{batch_info['total_files']} 个文件")
    print(f"📊 使用了 {len(api_servers)} 个服务器，并发度: {max_workers}")

@app.route('/progress/<batch_id>')
def get_progress(batch_id):
    """获取批量处理进度"""
    if batch_id not in batch_status:
        return jsonify({'error': '批次不存在'}), 404
    
    status = batch_status[batch_id]
    return jsonify({
        'batch_id': batch_id,
        'total_files': status['total_files'],
        'completed_files': status['completed_files'],
        'current_file': status.get('current_file', 0),
        'files': status['files']
    })

@app.route('/server_status/<batch_id>')
def get_server_status(batch_id):
    """获取服务器状态信息"""
    if batch_id not in batch_status:
        return jsonify({'error': '批次不存在'}), 404
    
    # 从batch_status中获取服务器状态信息
    status = batch_status[batch_id]
    server_statuses = status.get('server_statuses', {})
    
    return jsonify({
        'batch_id': batch_id,
        'server_statuses': server_statuses,
        'timestamp': time.time()
    })

@app.route('/retry_failed', methods=['POST'])
def retry_failed_files():
    """重试失败的文件"""
    try:
        batch_id = request.form.get('batch_id')
        api_servers_json = request.form.get('api_servers')
        concurrency = int(request.form.get('concurrency', 2))
        voice = request.form.get('voice', 'zh-CN-XiaoxiaoNeural')
        speed = float(request.form.get('speed', 1.0))
        
        if not batch_id or batch_id not in batch_status:
            return jsonify({'error': '批次不存在'}), 404
        
        # 解析API服务器列表
        try:
            api_servers = json.loads(api_servers_json) if api_servers_json else []
        except json.JSONDecodeError:
            return jsonify({'error': 'API服务器配置格式错误'}), 400
        
        # 过滤启用的服务器
        enabled_servers = [server for server in api_servers if server.get('enabled', False)]
        if not enabled_servers:
            return jsonify({'error': '没有启用的API服务器'}), 400
        
        batch_info = batch_status[batch_id]
        
        # 找出失败的文件
        failed_files = []
        for file_id, file_info in batch_info['files'].items():
            if file_info['status'] == 'failed':
                failed_files.append(file_id)
        
        if not failed_files:
            return jsonify({'error': '没有失败的文件需要重试'}), 400
        
        print(f"🔄 开始重试失败文件:")
        print(f"  📁 批次ID: {batch_id}")
        print(f"  📄 失败文件数量: {len(failed_files)}")
        print(f"  🖥️ 可用服务器: {len(enabled_servers)}")
        print(f"  ⚡ 并发度: {concurrency}")
        
        # 重置失败文件的状态
        for file_id in failed_files:
            file_info = batch_info['files'][file_id]
            file_info['status'] = 'pending'
            file_info['progress'] = 0
            file_info['stage'] = '⏳ 等待重试...'
            file_info['error'] = None
        
        # 更新批次状态
        batch_info['status'] = 'processing'
        batch_info['completed_files'] = batch_info['total_files'] - len(failed_files)
        batch_info['current_file'] = batch_info['completed_files']
        
        # 获取批次目录
        batch_upload_dir = batch_info['upload_dir']
        
        # 启动异步重试处理
        retry_thread = threading.Thread(
            target=run_async_processing,
            args=(batch_id, batch_upload_dir, voice, speed, enabled_servers, concurrency, failed_files)
        )
        retry_thread.daemon = True
        retry_thread.start()
        
        return jsonify({
            'success': True,
            'message': f'开始重试 {len(failed_files)} 个失败文件',
            'retry_files': len(failed_files)
        })
        
    except Exception as e:
        print(f"重试失败文件时出错: {str(e)}", file=sys.stderr)
        return jsonify({'error': f'重试失败: {str(e)}'}), 500

@app.route('/api/folders')
def get_folders():
    """获取uploads目录下的所有文件夹列表"""
    try:
        upload_dir = app.config['UPLOAD_FOLDER']
        if not os.path.exists(upload_dir):
            return jsonify({'folders': []})
        
        folders = []
        for item in os.listdir(upload_dir):
            item_path = os.path.join(upload_dir, item)
            if os.path.isdir(item_path):
                # 获取文件夹信息
                files = os.listdir(item_path)
                md_files = [f for f in files if f.endswith('.md')]
                mp3_files = [f for f in files if f.endswith('.mp3')]
                
                # 获取文件夹创建时间
                create_time = os.path.getctime(item_path)
                
                folders.append({
                    'name': item,
                    'path': item_path,
                    'md_count': len(md_files),
                    'mp3_count': len(mp3_files),
                    'total_files': len(files),
                    'create_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(create_time))
                })
        
        # 按创建时间倒序排列
        folders.sort(key=lambda x: x['create_time'], reverse=True)
        return jsonify({'folders': folders})
    
    except Exception as e:
        return jsonify({'error': f'获取文件夹列表失败: {str(e)}'}), 500

@app.route('/api/download/<folder_name>')
def download_folder(folder_name):
    """下载指定文件夹的ZIP包"""
    try:
        # 安全检查：防止路径遍历攻击
        if '..' in folder_name or '/' in folder_name or '\\' in folder_name:
            return jsonify({'error': '无效的文件夹名称'}), 400
        
        upload_dir = app.config['UPLOAD_FOLDER']
        folder_path = os.path.join(upload_dir, folder_name)
        
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return jsonify({'error': '文件夹不存在'}), 404
        
        # 创建内存中的ZIP文件
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    # 计算相对路径，保持文件夹结构
                    arcname = os.path.relpath(file_path, folder_path)
                    zipf.write(file_path, arcname)
        
        memory_file.seek(0)
        
        # 生成下载文件名
        download_filename = f"{folder_name}.zip"
        
        return send_file(
            memory_file,
            as_attachment=True,
            download_name=download_filename,
            mimetype='application/zip'
        )
    
    except Exception as e:
        return jsonify({'error': f'下载失败: {str(e)}'}), 500

@app.route('/api/delete/<folder_name>', methods=['DELETE'])
def delete_folder(folder_name):
    """删除指定文件夹"""
    try:
        # 安全检查：防止路径遍历攻击
        if '..' in folder_name or '/' in folder_name or '\\' in folder_name:
            return jsonify({'error': '无效的文件夹名称'}), 400
        
        upload_dir = app.config['UPLOAD_FOLDER']
        folder_path = os.path.join(upload_dir, folder_name)
        
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return jsonify({'error': '文件夹不存在'}), 404
        
        # 删除文件夹及其内容
        import shutil
        shutil.rmtree(folder_path)
        
        return jsonify({'message': f'文件夹 {folder_name} 删除成功'})
    
    except Exception as e:
        return jsonify({'error': f'删除失败: {str(e)}'}), 500

if __name__ == '__main__':
    # 支持Docker部署，监听所有接口
    import os
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 5055))
    debug = os.environ.get('FLASK_ENV', 'development') == 'development'
    
    print(f"🚀 启动TTS批量转换服务...")
    print(f"📍 监听地址: {host}:{port}")
    print(f"🔧 调试模式: {debug}")
    
    app.run(host=host, port=port, debug=debug)