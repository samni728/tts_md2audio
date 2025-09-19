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
import random
import contextlib
from collections import deque, defaultdict
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

# 默认提交接口的清洗配置
DEFAULT_CLEANING_OPTIONS = {
    "remove_markdown": True,
    "remove_emoji": True,
    "remove_urls": True,
    "remove_line_breaks": True,
    "remove_citation_numbers": True
}

# 最小音频有效性判定配置
MIN_AUDIO_SIZE_BYTES = int(os.environ.get("TTS_MIN_AUDIO_SIZE_BYTES", 4096))
MIN_AUDIO_BYTES_PER_CHAR = float(
    os.environ.get("TTS_MIN_AUDIO_BYTES_PER_CHAR", "3.0")
)

# 全局API并发上限（仅当显式配置 >0 时启用；默认禁用，按服务器独立处理）
def _init_global_semaphore():
    value = os.environ.get('GLOBAL_CONCURRENCY_LIMIT', '0')
    try:
        limit = int(value)
    except ValueError:
        limit = 0
    if limit and limit > 0:
        return asyncio.Semaphore(limit)
    return None

global_api_semaphore = _init_global_semaphore()

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

async def async_text_to_speech(session, text, output_path, voice="zh-CN-XiaoxiaoNeural", speed=1.0, api_url=None, api_key=None, timeout_seconds: int = 300, pitch: float = 1.0, cleaning_options=None, response_format: str = "mp3"):
    """异步调用TTS API转换文本为语音（固定超时，移除按字数动态超时）。

    返回 (success, status_code, error_detail) 元组，便于上层针对限流/超时等情况做精细化处理。
    当出现网络异常、超时等情况时 status_code 可能为 None，同时 error_detail 提供简短说明。
    """
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
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"
    }

    effective_cleaning = DEFAULT_CLEANING_OPTIONS.copy()
    if cleaning_options:
        effective_cleaning.update(cleaning_options)

    data = {
        "model": "tts-1",
        "input": text,
        "voice": voice,
        "speed": speed,
        "pitch": pitch,
        "cleaning_options": effective_cleaning
    }

    if response_format:
        data["response_format"] = response_format
    
    try:
        text_length = len(text)
        print(f"⏱️ 固定超时: {timeout_seconds}秒，文本长度: {text_length:,} 字符")
        
        # 异步发送请求并获取响应
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with session.post(api_url, headers=headers, json=data, timeout=timeout) as response:
            if response.status == 200:
                # 异步读取响应内容
                content = await response.read()
                
                # 保存音频文件
                with open(output_path, 'wb') as f:
                    f.write(content)

                # 基于文本长度和固定阈值检测音频是否过短/为空
                expected_min_size = max(
                    MIN_AUDIO_SIZE_BYTES,
                    int(len(text) * MIN_AUDIO_BYTES_PER_CHAR)
                )
                actual_size = os.path.getsize(output_path)

                if actual_size < expected_min_size:
                    with contextlib.suppress(Exception):
                        os.remove(output_path)
                    warning_msg = (
                        f"⚠️ 音频文件疑似异常 (大小 {actual_size}B < 预期 {expected_min_size}B, "
                        f"文本长度 {len(text)}). 将视为失败并计划重试。"
                    )
                    print(warning_msg, file=sys.stderr)
                    return False, response.status, 'audio_too_small'

                return True, response.status, None
            else:
                # 尝试读取错误响应内容
                error_detail = None
                try:
                    error_content = await response.text()
                    print(f"❌ TTS API返回错误状态码: {response.status} ({api_url})", file=sys.stderr)
                    print(f"📄 错误响应内容: {error_content[:200]}...", file=sys.stderr)
                    error_detail = error_content[:200]
                except:
                    print(f"❌ TTS API返回错误状态码: {response.status} ({api_url}) - 无法读取错误详情", file=sys.stderr)
                    error_detail = None
                return False, response.status, error_detail
                
    except asyncio.TimeoutError:
        print(f"⏰ TTS转换超时 ({api_url}) - 固定超时: {timeout_seconds}s, 文本长度: {len(text):,} 字符", file=sys.stderr)
        return False, None, 'timeout'
    except aiohttp.ClientConnectorError as e:
        print(f"🔌 连接错误 ({api_url}): {str(e)} - 可能原因: DNS解析失败、服务器不可达、端口被拒绝", file=sys.stderr)
        return False, None, str(e)
    except aiohttp.ClientError as e:
        print(f"🌐 网络错误 ({api_url}): {str(e)} - 可能原因: 网络中断、SSL握手失败", file=sys.stderr)
        return False, None, str(e)
    except Exception as e:
        print(f"💥 TTS转换失败 ({api_url}): {str(e)} - 未知错误", file=sys.stderr)
        return False, None, str(e)

def text_to_speech(text, output_path, voice="zh-CN-XiaoxiaoNeural", speed=1.0, api_url=None, api_key=None, pitch: float = 1.0, cleaning_options=None, response_format: str = "mp3"):
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
        "Content-Type": "application/json",
        "Accept": "audio/mpeg"
    }

    effective_cleaning = DEFAULT_CLEANING_OPTIONS.copy()
    if cleaning_options:
        effective_cleaning.update(cleaning_options)

    data = {
        "model": "tts-1",
        "input": text,  # 直接发送原始文本，让API端处理清理
        "voice": voice,
        "speed": speed,
        "pitch": pitch,
        "cleaning_options": effective_cleaning
    }

    if response_format:
        data["response_format"] = response_format
    
    try:
        # 发送请求并获取响应
        response = requests.post(api_url, headers=headers, json=data)
        response.raise_for_status()
        
        # 保存音频文件
        with open(output_path, 'wb') as f:
            f.write(response.content)

        expected_min_size = max(
            MIN_AUDIO_SIZE_BYTES,
            int(len(text) * MIN_AUDIO_BYTES_PER_CHAR)
        )
        actual_size = os.path.getsize(output_path)

        if actual_size < expected_min_size:
            with contextlib.suppress(Exception):
                os.remove(output_path)
            raise ValueError(
                f"audio_too_small (size={actual_size}, expected>={expected_min_size}, text_len={len(text)})"
            )
        
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
    """异步处理文件，支持选择负载均衡器"""
    if batch_id not in batch_status:
        return
    
    batch_info = batch_status[batch_id]
    
    # 根据配置选择使用哪个负载均衡器
    if USE_SIMPLE_BALANCER:
        print("⚡ 使用调度官负载均衡器 (V5)")
        await dispatcher_balancer_v5(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files)
        return
    
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

async def dynamic_worker_balancer_v4(batch_id, batch_upload_dir, voice, speed, api_servers, specific_files=None):
    """V4：基于持久化工作节点与队列的事件驱动动态负载均衡。"""
    if batch_id not in batch_status:
        return

    batch_info = batch_status[batch_id]

    # 1) 任务列表
    files_to_process = specific_files or list(batch_info['files'].keys())
    total_tasks_count = len(files_to_process)

    warmup_primary = max(10, MAX_CONCURRENCY * 2)
    warmup_secondary = max(10, MAX_CONCURRENCY)
    WARMUP_COUNT = min(total_tasks_count, warmup_primary)
    SECOND_STAGE_COUNT = max(0, min(total_tasks_count - WARMUP_COUNT, warmup_secondary))

    print("🚀 启动动态工作节点负载均衡器 (V4):")
    print(f"  📊 总任务数: {total_tasks_count}")
    print(f"  🖥️ 服务器节点数: {len(api_servers)}")

    # 初始化 WebUI 服务器状态
    batch_info['server_statuses'] = {}
    for i, server in enumerate(api_servers):
        batch_info['server_statuses'][i] = {
            'name': server.get('name', f'Server-{i}'),
            'status': 'idle',
            'load': 0,
            'max_load': 1,
            'completed_tasks': 0,
            'timeout_tasks': 0,
            'failed_tasks': 0,
            'total_time': 0.0,
        }

    # 2) 队列：放 (file_id, retry_count)
    MAX_RETRIES = 3
    task_queue = asyncio.Queue()
    for file_id in files_to_process:
        task_queue.put_nowait((file_id, 0))

    # 停止信号（每个工作节点一个）
    STOP_SIGNAL = object()
    for _ in range(len(api_servers)):
        task_queue.put_nowait(STOP_SIGNAL)

    batch_info['completed_files'] = 0

    # 3) 工作节点定义
    async def worker_node(server_id, server_info):
        server_name = server_info.get('name', f"Server-{server_id}")
        server_url = server_info.get('url')
        api_key = server_info.get('apiKey', server_info.get('api_key', ''))
        stats = {'success': 0, 'fail': 0, 'total_time': 0.0}

        print(f"👷 工作节点 {server_name} 已启动并待命。")

        async with aiohttp.ClientSession() as session:
            while True:
                task = await task_queue.get()

                if task is STOP_SIGNAL:
                    task_queue.task_done()
                    break

                file_id, retry_count = task
                if batch_id not in batch_status or file_id not in batch_status[batch_id]['files']:
                    task_queue.task_done()
                    continue

                filename = batch_info['files'][file_id]['filename']
                batch_info['files'][file_id]['stage'] = f'处理中 @{server_name}'
                batch_info['files'][file_id]['status'] = 'processing'

                # WebUI：节点负载更新
                batch_info['server_statuses'][server_id]['status'] = 'busy'
                batch_info['server_statuses'][server_id]['load'] = 1

                print(f"  -> 节点 {server_name} 接收任务: {filename} (第 {retry_count + 1} 次尝试)")

                start_time = time.time()
                success = False
                status_code = None
                try:
                    input_path = os.path.join(batch_upload_dir, filename)
                    output_path = os.path.join(batch_upload_dir, filename.replace('.md', '.mp3'))
                    with open(input_path, 'r', encoding='utf-8') as f:
                        text = f.read()

                    # 若设置GLOBAL_CONCURRENCY_LIMIT>0，则启用总闸门；否则直接调用
                    if global_api_semaphore is not None:
                        async with global_api_semaphore:
                            success, status_code, error_detail = await async_text_to_speech(
                                session, text, output_path, voice, speed, server_url, api_key, timeout_seconds=300
                            )
                    else:
                        success, status_code, error_detail = await async_text_to_speech(
                            session, text, output_path, voice, speed, server_url, api_key, timeout_seconds=300
                        )
                    if not success:
                        raise Exception("API返回非200状态码")

                except Exception as e:
                    if status_code is not None:
                        print(f"❌ 节点 {server_name} 任务失败: {filename}, 状态码: {status_code}, 原因: {type(e).__name__}")
                    else:
                        print(f"❌ 节点 {server_name} 任务失败: {filename}, 原因: {type(e).__name__}")

                processing_time = time.time() - start_time

                # 统计与UI更新
                batch_info['server_statuses'][server_id]['total_time'] += processing_time

                if success:
                    stats['success'] += 1
                    stats['total_time'] += processing_time
                    batch_info['files'][file_id]['status'] = 'completed'
                    batch_info['files'][file_id]['stage'] = '✅ 完成'
                    batch_info['completed_files'] += 1
                    batch_info['current_file'] = batch_info['completed_files']
                    batch_info['server_statuses'][server_id]['completed_tasks'] += 1
                    print(f"✅ 任务完成: {filename} (服务器: {server_name}, 耗时: {processing_time:.2f}秒)")
                else:
                    stats['fail'] += 1
                    if retry_count < MAX_RETRIES:
                        # 指数退避 + 抖动
                        delay = (2 ** retry_count) + random.uniform(0, 1)
                        print(f"🔄 任务将重试 (第 {retry_count+1} 次)，将在 {delay:.2f} 秒后执行... [{filename}]")
                        # 延迟后放回队列，不绑定同一节点
                        async def requeue_after_delay(delay_s: float, item):
                            await asyncio.sleep(delay_s)
                            await task_queue.put(item)
                        asyncio.create_task(requeue_after_delay(delay, (file_id, retry_count + 1)))
                        batch_info['files'][file_id]['stage'] = f'等待重试 ({retry_count+1}/{MAX_RETRIES})'
                    else:
                        batch_info['files'][file_id]['status'] = 'failed'
                        batch_info['files'][file_id]['stage'] = '❌ 失败 (已达上限)'
                        batch_info['completed_files'] += 1
                        batch_info['current_file'] = batch_info['completed_files']

                task_queue.task_done()
                # WebUI：节点恢复空闲
                batch_info['server_statuses'][server_id]['load'] = 0
                batch_info['server_statuses'][server_id]['status'] = 'idle'

        print(f"🏁 节点 {server_name} 停止。统计: 成功 {stats['success']}, 失败 {stats['fail']}.")

    # 4) 启动所有工作节点（每台服务器一个并发=1的节点）
    worker_tasks = [asyncio.create_task(worker_node(i, s)) for i, s in enumerate(api_servers)]

    # 5) 等待所有任务完成
    await task_queue.join()
    await asyncio.gather(*worker_tasks)

    print("🎉 V4 动态负载均衡器处理完成！")

async def dynamic_worker_balancer_v4_1(batch_id, batch_upload_dir, voice, speed, api_servers, specific_files=None):
    """V4.1：修复工作节点生命周期，使用stop_event实现动态竞争与优雅退出。"""
    if batch_id not in batch_status:
        return

    batch_info = batch_status[batch_id]

    MAX_RETRIES = 3
    files_to_process = specific_files or list(batch_info['files'].keys())
    total_tasks_count = len(files_to_process)

    print("🚀 启动动态工作节点负载均衡器 (V4.1 - 补丁版):")
    print(f"  📊 总任务数: {total_tasks_count}")
    print(f"  🖥️ 服务器节点数: {len(api_servers)}")

    # 初始化 WebUI 服务器状态
    batch_info['server_statuses'] = {}
    for i, server in enumerate(api_servers):
        batch_info['server_statuses'][i] = {
            'name': server.get('name', f'Server-{i}'),
            'status': 'idle',
            'load': 0,
            'max_load': 1,
            'completed_tasks': 0,
            'timeout_tasks': 0,
            'failed_tasks': 0,
            'total_time': 0.0,
        }

    # 任务队列：仅放普通任务
    task_queue = asyncio.Queue()
    for file_id in files_to_process:
        task_queue.put_nowait((file_id, 0))

    # 停止事件：由监控协程在队列完成后统一发出
    stop_event = asyncio.Event()

    batch_info['completed_files'] = 0

    async def worker_node(server_id, server_info):
        server_name = server_info.get('name', f"Server-{server_id}")
        server_url = server_info.get('url')
        api_key = server_info.get('apiKey', server_info.get('api_key', ''))
        stats = {'success': 0, 'fail': 0, 'total_time': 0.0}

        print(f"👷 工作节点 {server_name} 已启动并进入监听循环。")

        async with aiohttp.ClientSession() as session:
            while not stop_event.is_set():
                try:
                    task = await asyncio.wait_for(task_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                # 解析任务
                try:
                    file_id, retry_count = task
                except Exception:
                    task_queue.task_done()
                    continue

                if batch_id not in batch_status or file_id not in batch_status[batch_id]['files']:
                    task_queue.task_done()
                    continue

                filename = batch_info['files'][file_id]['filename']
                batch_info['files'][file_id]['stage'] = f'处理中 @{server_name}'
                batch_info['files'][file_id]['status'] = 'processing'

                # WebUI：节点置为忙碌
                batch_info['server_statuses'][server_id]['status'] = 'busy'
                batch_info['server_statuses'][server_id]['load'] = 1

                print(f"  -> 节点 {server_name} 接收任务: {filename} (第 {retry_count + 1} 次尝试)")

                start_time = time.time()
                success = False
                status_code = None
                try:
                    input_path = os.path.join(batch_upload_dir, filename)
                    output_path = os.path.join(batch_upload_dir, filename.replace('.md', '.mp3'))
                    with open(input_path, 'r', encoding='utf-8') as f:
                        text = f.read()

                    success, status_code, error_detail = await async_text_to_speech(
                        session, text, output_path, voice, speed, server_url, api_key, timeout_seconds=300
                    )
                    if not success:
                        raise Exception("API返回非200状态码")

                except Exception as e:
                    if status_code is not None:
                        print(f"❌ 节点 {server_name} 任务失败: {filename}, 状态码: {status_code}, 原因: {type(e).__name__}")
                    else:
                        print(f"❌ 节点 {server_name} 任务失败: {filename}, 原因: {type(e).__name__}")

                processing_time = time.time() - start_time

                # 统计时间
                batch_info['server_statuses'][server_id]['total_time'] += processing_time

                if success:
                    stats['success'] += 1
                    stats['total_time'] += processing_time
                    batch_info['files'][file_id]['status'] = 'completed'
                    batch_info['files'][file_id]['stage'] = '✅ 完成'
                    batch_info['completed_files'] += 1
                    batch_info['current_file'] = batch_info['completed_files']
                    batch_info['server_statuses'][server_id]['completed_tasks'] += 1
                    print(f"✅ 任务完成: {filename} (服务器: {server_name}, 耗时: {processing_time:.2f}秒)")
                else:
                    stats['fail'] += 1
                    if retry_count < MAX_RETRIES:
                        delay = (2 ** retry_count) + random.uniform(0, 1)
                        print(f"🔄 任务将重试 (第 {retry_count+1} 次)，将在 {delay:.2f} 秒后执行... [{filename}]")
                        async def requeue_after_delay(delay_s: float, item):
                            await asyncio.sleep(delay_s)
                            await task_queue.put(item)
                        asyncio.create_task(requeue_after_delay(delay, (file_id, retry_count + 1)))
                        batch_info['files'][file_id]['stage'] = f'等待重试 ({retry_count+1}/{MAX_RETRIES})'
                    else:
                        batch_info['files'][file_id]['status'] = 'failed'
                        batch_info['files'][file_id]['stage'] = '❌ 失败 (已达上限)'
                        batch_info['completed_files'] += 1
                        batch_info['current_file'] = batch_info['completed_files']

                # 标记任务处理完成（无论成功失败）
                task_queue.task_done()
                # 节点恢复空闲
                batch_info['server_statuses'][server_id]['load'] = 0
                batch_info['server_statuses'][server_id]['status'] = 'idle'

        print(f"🏁 节点 {server_name} 收到停止信号并退出。统计: 成功 {stats['success']}, 失败 {stats['fail']}")

    # 启动所有工作节点
    worker_tasks = [asyncio.create_task(worker_node(i, s)) for i, s in enumerate(api_servers)]

    # 监控完成：队列清空后发出停止事件
    async def monitor_completion():
        await task_queue.join()
        print("✅ 所有任务已处理完成，向所有工作节点发送停止信号...")
        stop_event.set()

    await monitor_completion()

    # 等待所有工作者优雅退出
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    print("🎉 V4.1 负载均衡器处理完成！")

async def dispatcher_balancer_v5(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files=None):
    return await dispatcher_balancer_v5_1(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files)


async def dispatcher_balancer_v5_1(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files=None):
    """V5.1：调度官模型升级版，包含预热与自适应速率控制。"""
    if batch_id not in batch_status:
        return

    batch_info = batch_status[batch_id]

    # --- 1. 关键参数 ---
    total_workers = max(1, len(api_servers))

    env_limit_raw = os.environ.get('BALANCER_MAX_CONCURRENCY', '').strip()
    env_limit = 0
    if env_limit_raw:
        try:
            env_limit = int(env_limit_raw)
        except ValueError:
            print(f"⚠️ 无法解析 BALANCER_MAX_CONCURRENCY={env_limit_raw!r}，忽略该限制。")
            env_limit = 0

    if env_limit > 0:
        MAX_CONCURRENCY = max(1, min(env_limit, total_workers))
    else:
        MAX_CONCURRENCY = total_workers


    INITIAL_DISPATCH_INTERVAL = 1.0
    SECOND_STAGE_INTERVAL = 0.5
    NORMAL_DISPATCH_INTERVAL = 0.2
    MAX_RETRIES = 6
    RATE_LIMIT_MAX_RETRIES = 10
    TIMEOUT_MAX_RETRIES = 6

    # 预热阶段任务数量根据并发和总任务自适应
    WARMUP_COUNT = 0
    SECOND_STAGE_COUNT = 0

    # 自适应节流参数
    ADAPTIVE_WINDOW = 20
    FAILURE_RATE_THRESHOLD = 0.2
    RECOVERY_RATE_THRESHOLD = 0.1
    ADAPTIVE_FAIL_INTERVAL = 0.5
    ADAPTIVE_INCREASE_STEP = 0.1
    ADAPTIVE_DECREASE_STEP = 0.05
    ADAPTIVE_MAX_INTERVAL = 1.5
    MIN_SAMPLE_SIZE = 5

    files_to_process = specific_files or list(batch_info['files'].keys())
    total_tasks_count = len(files_to_process)

    warmup_primary = max(10, MAX_CONCURRENCY * 2)
    warmup_secondary = max(10, MAX_CONCURRENCY)
    WARMUP_COUNT = min(total_tasks_count, warmup_primary)
    SECOND_STAGE_COUNT = max(0, min(total_tasks_count - WARMUP_COUNT, warmup_secondary))

    if env_limit > 0:
        concurrency_source = f"环境限制 {env_limit}"
    else:
        concurrency_source = f"可用节点 {total_workers}"

    print("🚀 启动精细化调度官 (V5.1):")
    print(f"  🎯 全局并发上限: {MAX_CONCURRENCY} ({concurrency_source})")
    print(f"  ⏱️ 预热/正常间隔: {INITIAL_DISPATCH_INTERVAL}s / {NORMAL_DISPATCH_INTERVAL}s")
    print(f"  🔄 次级预热间隔: 前{WARMUP_COUNT}个 -> {INITIAL_DISPATCH_INTERVAL}s, 后续{SECOND_STAGE_COUNT}个 -> {SECOND_STAGE_INTERVAL}s")

    # --- 2. 初始化队列和控制器 ---
    task_queue = asyncio.Queue()
    for file_id in files_to_process:
        task_queue.put_nowait((file_id, 0))

    worker_queue = asyncio.Queue()
    for i in range(len(api_servers)):
        worker_queue.put_nowait(i)

    concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    batch_info['server_statuses'] = {}
    for i, server in enumerate(api_servers):
        batch_info['server_statuses'][i] = {
            'name': server.get('name', f'Server-{i}'),
            'status': 'idle',
            'load': 0,
            'max_load': 1,
            'completed_tasks': 0,
            'timeout_tasks': 0,
            'failed_tasks': 0,
            'total_time': 0.0,
        }

    completion_event = asyncio.Event()
    finished_files = set()

    recent_results = deque(maxlen=ADAPTIVE_WINDOW)
    adaptive_interval = NORMAL_DISPATCH_INTERVAL
    metrics_lock = asyncio.Lock()
    rate_limit_counters = defaultdict(int)
    timeout_counters = defaultdict(int)

    async def update_rate_metrics(success: bool):
        nonlocal adaptive_interval
        async with metrics_lock:
            recent_results.append(1 if success else 0)
            if len(recent_results) < MIN_SAMPLE_SIZE:
                return

            success_ratio = sum(recent_results) / len(recent_results)
            failure_rate = 1 - success_ratio

            if failure_rate >= FAILURE_RATE_THRESHOLD:
                new_interval = min(
                    ADAPTIVE_MAX_INTERVAL,
                    max(adaptive_interval, ADAPTIVE_FAIL_INTERVAL) + ADAPTIVE_INCREASE_STEP,
                )
                if new_interval > adaptive_interval:
                    print(f"⚠️ 最近失败率 {failure_rate:.0%}，派发间隔提升至 {new_interval:.2f}s")
                adaptive_interval = new_interval
            elif adaptive_interval > NORMAL_DISPATCH_INTERVAL and failure_rate <= RECOVERY_RATE_THRESHOLD:
                new_interval = max(NORMAL_DISPATCH_INTERVAL, adaptive_interval - ADAPTIVE_DECREASE_STEP)
                if new_interval < adaptive_interval:
                    print(f"✅ 失败率回落至 {failure_rate:.0%}，派发间隔回调至 {new_interval:.2f}s")
                adaptive_interval = new_interval

    batch_info['completed_files'] = 0

    async def worker(worker_id, file_id, retry_count):
        server_info = api_servers[worker_id]
        server_name = server_info.get('name', f"Server-{worker_id}")
        server_url = server_info.get('url')
        api_key = server_info.get('apiKey', server_info.get('api_key', ''))

        success = False
        skip_metrics = False

        try:
            if batch_id not in batch_status or file_id not in batch_status[batch_id]['files']:
                skip_metrics = True
                return

            filename = batch_info['files'][file_id]['filename']
            batch_info['files'][file_id]['status'] = 'processing'
            batch_info['files'][file_id]['stage'] = f'处理中 @{server_name}'
            batch_info['server_statuses'][worker_id]['status'] = 'busy'
            batch_info['server_statuses'][worker_id]['load'] = 1

            input_path = os.path.join(batch_upload_dir, filename)
            output_path = os.path.join(batch_upload_dir, filename.replace('.md', '.mp3'))
            with open(input_path, 'r', encoding='utf-8') as f:
                text = f.read()

            await asyncio.sleep(random.uniform(0.0, 0.05))

            start_time = time.time()
            status_code = None
            error_detail = None
            async with aiohttp.ClientSession() as session:
                if global_api_semaphore is not None:
                    async with global_api_semaphore:
                        success, status_code, error_detail = await async_text_to_speech(
                            session, text, output_path, voice, speed, server_url, api_key, timeout_seconds=300
                        )
                else:
                    success, status_code, error_detail = await async_text_to_speech(
                        session, text, output_path, voice, speed, server_url, api_key, timeout_seconds=300
                    )

            error_text = (error_detail or "").lower()
            is_timeout = (error_detail == 'timeout') or ('timeout' in error_text)
            is_rate_limited = (
                status_code in {429, 503}
                or 'too many requests' in error_text
                or 'too many subrequests' in error_text
                or 'rate limit' in error_text
            )
            if status_code == 500 and 'too many' in error_text:
                is_rate_limited = True

            cost = time.time() - start_time
            batch_info['server_statuses'][worker_id]['total_time'] += cost

            if success:
                rate_limit_counters.pop(file_id, None)
                timeout_counters.pop(file_id, None)
                batch_info['files'][file_id]['status'] = 'completed'
                batch_info['files'][file_id]['stage'] = '✅ 完成'
                if file_id not in finished_files:
                    finished_files.add(file_id)
                    batch_info['completed_files'] += 1
                    batch_info['current_file'] = batch_info['completed_files']
                batch_info['server_statuses'][worker_id]['completed_tasks'] += 1
                print(f"✅ 任务完成: {filename} (服务器: {server_name}, 耗时: {cost:.2f}秒)")
            else:
                batch_info['server_statuses'][worker_id]['status'] = 'error'
                if is_rate_limited:
                    rate_limit_counters[file_id] += 1
                    rate_limit_attempt = rate_limit_counters[file_id]
                    if rate_limit_attempt > RATE_LIMIT_MAX_RETRIES:
                        stage_msg = f'❌ 限流失败 (已重试{RATE_LIMIT_MAX_RETRIES}次)'
                        batch_info['files'][file_id]['status'] = 'failed'
                        batch_info['files'][file_id]['stage'] = stage_msg
                        rate_limit_counters.pop(file_id, None)
                        timeout_counters.pop(file_id, None)
                        batch_info['server_statuses'][worker_id]['failed_tasks'] += 1
                        if file_id not in finished_files:
                            finished_files.add(file_id)
                            batch_info['completed_files'] += 1
                            batch_info['current_file'] = batch_info['completed_files']
                        print(
                            f"❌ 限流重试耗尽: {filename} (服务器: {server_name}, 状态码: {status_code}, 耗时: {cost:.2f}秒)"
                        )
                    else:
                        delay_exponent = min(6, rate_limit_attempt + 1)
                        delay = (2 ** delay_exponent) + random.uniform(0, 2.0)
                        print(
                            f"🛑 限流 {status_code}: {filename} @ {server_name}，第{rate_limit_attempt}次等待，{delay:.2f}s 后重试"
                        )

                        async def requeue_rate_limit(delay_s: float, item):
                            await asyncio.sleep(delay_s)
                            await task_queue.put(item)

                        asyncio.create_task(requeue_rate_limit(delay, (file_id, retry_count)))
                        batch_info['files'][file_id]['stage'] = (
                            f'等待限流恢复 ({rate_limit_attempt}/{RATE_LIMIT_MAX_RETRIES})'
                        )
                elif is_timeout:
                    batch_info['server_statuses'][worker_id]['timeout_tasks'] += 1
                    timeout_counters[file_id] += 1
                    timeout_attempt = timeout_counters[file_id]
                    if timeout_attempt > TIMEOUT_MAX_RETRIES:
                        batch_info['files'][file_id]['status'] = 'failed'
                        batch_info['files'][file_id]['stage'] = (
                            f'❌ 超时超出上限 ({TIMEOUT_MAX_RETRIES}次)'
                        )
                        rate_limit_counters.pop(file_id, None)
                        timeout_counters.pop(file_id, None)
                        batch_info['server_statuses'][worker_id]['failed_tasks'] += 1
                        if file_id not in finished_files:
                            finished_files.add(file_id)
                            batch_info['completed_files'] += 1
                            batch_info['current_file'] = batch_info['completed_files']
                        print(
                            f"❌ 超时重试耗尽: {filename} (服务器: {server_name}, 耗时: {cost:.2f}秒)"
                        )
                    else:
                        delay = 5.0 * timeout_attempt + random.uniform(0, 3.0)
                        print(
                            f"⏳ 超时重试: {filename} @ {server_name} 第{timeout_attempt}次，将在 {delay:.2f}s 后重试"
                        )

                        async def requeue_timeout(delay_s: float, item):
                            await asyncio.sleep(delay_s)
                            await task_queue.put(item)

                        asyncio.create_task(requeue_timeout(delay, (file_id, retry_count)))
                        batch_info['files'][file_id]['stage'] = (
                            f'等待超时恢复 ({timeout_attempt}/{TIMEOUT_MAX_RETRIES})'
                        )
                elif retry_count < MAX_RETRIES:
                    batch_info['server_statuses'][worker_id]['failed_tasks'] += 1
                    delay = (2 ** (retry_count + 1)) + random.uniform(0, 2.0)
                    print(
                        f"❌ 任务失败: {filename} (服务器: {server_name}, 状态码: {status_code}, 耗时: {cost:.2f}秒)，将在 {delay:.2f}s 后重试"
                    )

                    async def requeue_general(delay_s: float, item):
                        await asyncio.sleep(delay_s)
                        await task_queue.put(item)

                    asyncio.create_task(requeue_general(delay, (file_id, retry_count + 1)))
                    batch_info['files'][file_id]['stage'] = f'等待重试 ({retry_count+1}/{MAX_RETRIES})'
                else:
                    rate_limit_counters.pop(file_id, None)
                    timeout_counters.pop(file_id, None)
                    batch_info['server_statuses'][worker_id]['failed_tasks'] += 1
                    batch_info['files'][file_id]['status'] = 'failed'
                    batch_info['files'][file_id]['stage'] = '❌ 失败 (已达上限)'
                    if file_id not in finished_files:
                        finished_files.add(file_id)
                        batch_info['completed_files'] += 1
                        batch_info['current_file'] = batch_info['completed_files']
        except Exception as e:
            print(f"💥 工人 {server_name} 异常: {file_id} -> {e}")
            batch_info['server_statuses'][worker_id]['status'] = 'error'
            batch_info['server_statuses'][worker_id]['failed_tasks'] += 1
            if retry_count < MAX_RETRIES:
                delay = (2 ** (retry_count + 1)) + random.uniform(0, 2.0)

                async def requeue_exception(delay_s: float, item):
                    await asyncio.sleep(delay_s)
                    await task_queue.put(item)

                asyncio.create_task(requeue_exception(delay, (file_id, retry_count + 1)))
                batch_info['files'][file_id]['stage'] = f'等待重试 ({retry_count+1}/{MAX_RETRIES})'
            else:
                if batch_id in batch_status and file_id in batch_status[batch_id]['files']:
                    rate_limit_counters.pop(file_id, None)
                    timeout_counters.pop(file_id, None)
                    batch_info['files'][file_id]['status'] = 'failed'
                    batch_info['files'][file_id]['stage'] = '💥 处理异常'
                    if file_id not in finished_files:
                        finished_files.add(file_id)
                        batch_info['completed_files'] += 1
                        batch_info['current_file'] = batch_info['completed_files']
        finally:
            if not skip_metrics:
                await update_rate_metrics(success)

            batch_info['server_statuses'][worker_id]['load'] = 0
            batch_info['server_statuses'][worker_id]['status'] = 'idle'

            await worker_queue.put(worker_id)
            concurrency_semaphore.release()

            if len(finished_files) >= total_tasks_count:
                completion_event.set()

    async def dispatcher():
        dispatched_count = 0
        try:
            while not completion_event.is_set():
                await concurrency_semaphore.acquire()

                if completion_event.is_set():
                    concurrency_semaphore.release()
                    break

                try:
                    worker_id = await worker_queue.get()
                except asyncio.CancelledError:
                    concurrency_semaphore.release()
                    raise

                if completion_event.is_set():
                    await worker_queue.put(worker_id)
                    concurrency_semaphore.release()
                    break

                try:
                    file_id, retry_count = task_queue.get_nowait()
                except asyncio.QueueEmpty:
                    await worker_queue.put(worker_id)
                    concurrency_semaphore.release()
                    if completion_event.is_set():
                        break
                    await asyncio.sleep(0.1)
                    continue

                asyncio.create_task(worker(worker_id, file_id, retry_count))
                task_queue.task_done()
                dispatched_count += 1

                remaining = task_queue.qsize()
                idle_workers = worker_queue.qsize()

                if dispatched_count <= WARMUP_COUNT:
                    base_interval = INITIAL_DISPATCH_INTERVAL
                elif dispatched_count <= WARMUP_COUNT + SECOND_STAGE_COUNT:
                    base_interval = SECOND_STAGE_INTERVAL
                else:
                    base_interval = NORMAL_DISPATCH_INTERVAL

                interval = max(base_interval, adaptive_interval)

                print(
                    f"🧭 派发任务: {dispatched_count} | 剩余队列: {remaining} | 空闲服务器: {idle_workers} | 当前间隔: {interval:.2f}s"
                )

                if interval > 0:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    dispatcher_task = asyncio.create_task(dispatcher())
    await completion_event.wait()
    dispatcher_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await dispatcher_task

    print("🎉 V5.1 精细化调度处理完成！")

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
        success, _, _ = await async_text_to_speech(
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
        success, _, _ = await async_text_to_speech(
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
            success, _, _ = await async_text_to_speech(session, full_text, mp3_path, voice, speed, api_url, api_key)
            
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

# 继续未完成：扫描文件夹中缺失的MP3并仅处理这些MD
@app.route('/api/continue/<folder_name>', methods=['POST'])
def continue_folder(folder_name):
    try:
        # 安全检查
        if '..' in folder_name or '/' in folder_name or '\\' in folder_name:
            return jsonify({'error': '无效的文件夹名称'}), 400

        upload_dir = app.config['UPLOAD_FOLDER']
        folder_path = os.path.join(upload_dir, folder_name)
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return jsonify({'error': '文件夹不存在'}), 404

        # 读取客户端配置
        api_servers_json = request.form.get('api_servers', '[]')
        concurrency = int(request.form.get('concurrency', 1))
        voice = request.form.get('voice', 'zh-CN-XiaoxiaoNeural')
        speed = float(request.form.get('speed', 1.0))

        try:
            api_servers = json.loads(api_servers_json)
        except json.JSONDecodeError:
            return jsonify({'error': 'API服务器配置格式错误'}), 400

        enabled_servers = [s for s in api_servers if s.get('enabled', True)]
        if not enabled_servers:
            return jsonify({'error': '没有可用的API服务器'}), 400

        # 找出缺失的MP3对应的MD
        files = os.listdir(folder_path)
        md_files = [f for f in files if f.endswith('.md')]
        missing_md_files = []
        for md in md_files:
            mp3 = os.path.splitext(md)[0] + '.mp3'
            if mp3 not in files:
                missing_md_files.append(md)

        if not missing_md_files:
            return jsonify({'success': True, 'message': '没有缺失的任务，全部已完成', 'batch_id': None, 'retry_files': 0})

        # 创建新的batch以复用现有进度与轮询机制
        batch_id = str(uuid.uuid4())
        batch_status[batch_id] = {
            'total_files': len(missing_md_files),
            'completed_files': 0,
            'current_file': 0,
            'files': {},
            'server_statuses': {},
            'upload_dir': folder_path
        }

        # 初始化文件状态并构造specific_files列表（使用batch_id前缀的file_id）
        specific_files = []
        for md in missing_md_files:
            file_id = f"{batch_id}_{md}"
            batch_status[batch_id]['files'][file_id] = {
                'filename': md,
                'status': 'waiting',
                'progress': 0,
                'stage': '等待处理'
            }
            specific_files.append(file_id)

        # 启动异步处理，仅处理缺失项
        thread = threading.Thread(
            target=run_async_processing,
            args=(batch_id, folder_path, voice, speed, enabled_servers, concurrency, specific_files)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'success': True,
            'message': f'已开始继续处理 {len(missing_md_files)} 个未完成文件',
            'batch_id': batch_id,
            'retry_files': len(missing_md_files)
        })

    except Exception as e:
        print(f"继续未完成处理时出错: {str(e)}", file=sys.stderr)
        return jsonify({'error': f'继续处理失败: {str(e)}'}), 500

# 简化的负载均衡器配置
USE_SIMPLE_BALANCER = os.environ.get('USE_SIMPLE_BALANCER', 'true').lower() == 'true'

async def simple_load_balancer(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency, specific_files=None):
    """超简单的负载均衡器：派发任务 → 300秒超时监控 → 轮询检查"""
    if batch_id not in batch_status:
        return
    
    batch_info = batch_status[batch_id]
    
    # 确定要处理的文件
    if specific_files:
        files_to_process = specific_files
        print(f"🔄 重试模式: 处理指定的 {len(files_to_process)} 个文件")
    else:
        # 从batch_info中获取文件列表
        files_to_process = list(batch_info['files'].keys())
        print(f"🆕 全新处理: 处理所有 {len(files_to_process)} 个文件")
    
    if not files_to_process:
        print("📁 没有找到要处理的文件")
        return
    
    # 全局并发控制 - 核心优化
    GLOBAL_CONCURRENCY_LIMIT = 8  # 全局并发限制，避免触发API速率限制
    global_semaphore = asyncio.Semaphore(GLOBAL_CONCURRENCY_LIMIT)
    
    print(f"🚀 启动超简单负载均衡器:")
    print(f"  📊 总任务数: {len(files_to_process)}")
    print(f"  🖥️ 可用服务器: {len(api_servers)}")
    print(f"  ⏰ 超时时间: 300秒")
    print(f"  ⚡ 并发度: {concurrency}")
    print(f"  🎯 全局并发限制: {GLOBAL_CONCURRENCY_LIMIT} (防止API速率限制)")

    # 服务器状态管理（容量=每台服务器允许的并发，默认使用全局concurrency，可被单台覆盖）
    num_servers = len(api_servers)
    server_capacity = [int(api_servers[i].get('concurrency', concurrency)) for i in range(num_servers)]
    server_active = [0] * num_servers  # 当前活跃任务数
    server_used_count = [0] * num_servers  # 被派发过次数
    server_last_used = [0.0] * num_servers  # 最后一次派发时间
    server_completed = [0] * num_servers
    server_failed = [0] * num_servers
    server_consecutive_failures = [0] * num_servers  # 连续失败次数（用于熔断）
    last_dispatch_index = 0  # 轮询指针，打散同等条件服务器的选择

    # 初始化WebUI的服务器状态（简化模式也上报）
    for i in range(num_servers):
        batch_info['server_statuses'][i] = {
            'name': api_servers[i]['name'],
            'status': 'idle',
            'load': 0,
            'max_load': server_capacity[i],
            'completed_tasks': 0,
            'total_time': 0
        }

    # 任务队列和状态
    task_queue = asyncio.Queue()
    retry_queue = asyncio.Queue()
    active_tasks = set()
    completed_tasks = 0
    total_tasks = len(files_to_process)
    task_retries = {}
    max_retries = 3
    server_cooldown_until = [0.0] * num_servers  # 故障冷却截止时间戳

    # 初始化任务队列
    for file_id in files_to_process:
        await task_queue.put(file_id)

    def find_available_server():
        """找可用服务器：未用过优先 → 最小负载 → 失败率低 → 最久未用（含熔断检查）"""
        now_ts = time.time()
        candidates = [
            i for i in range(num_servers)
            if server_active[i] < server_capacity[i] and now_ts >= server_cooldown_until[i]
        ]
        if not candidates:
            return None

        # 轮询打散，避免总是选到相同索引
        rotated = candidates[last_dispatch_index % len(candidates):] + candidates[:last_dispatch_index % len(candidates)]

        # 计算排序键
        def sort_key(i: int):
            total = server_completed[i] + server_failed[i]
            fail_rate = (server_failed[i] / total) if total > 0 else 0.0
            return (
                0 if server_used_count[i] == 0 else 1,  # 未用过优先
                server_active[i],                          # 当前负载越小越优先
                server_used_count[i],                      # 使用次数更少更优先（促均衡）
                fail_rate,                                  # 失败率更低优先
                server_last_used[i]                         # 最久未用优先（时间更早）
            )

        return min(rotated, key=sort_key)
    
    async def process_task(file_id, server_id, pre_reserved: bool = False):
        """处理单个任务：派发 → 300秒超时监控 → 轮询检查"""
        nonlocal completed_tasks
        
        # 全局并发控制 - 获取信号量许可
        async with global_semaphore:
            server_name = api_servers[server_id]['name']
            print(f"📤 派发任务: {file_id} → {server_name} (全局并发: {GLOBAL_CONCURRENCY_LIMIT - global_semaphore._value}/{GLOBAL_CONCURRENCY_LIMIT})")
            
            # 更新文件状态
            if file_id in batch_info['files']:
                file_info = batch_info['files'][file_id]
                file_info['status'] = 'processing'
                file_info['progress'] = 10
                file_info['stage'] = f'🎵 正在转换 (服务器: {server_name})...'
            
            # 预占容量：若未在派发环节预占，这里补充占用
            if not pre_reserved:
                server_active[server_id] += 1
            server_used_count[server_id] += 1
            server_last_used[server_id] = time.time()
            active_tasks.add(file_id)

            # 更新WebUI服务器状态
            batch_info['server_statuses'][server_id]['load'] = server_active[server_id]
            batch_info['server_statuses'][server_id]['status'] = (
                'full' if server_active[server_id] >= server_capacity[server_id] else 'busy'
            )
            
            start_time = time.time()
            success = False
            
            try:
                # 读取文件内容
                filename = batch_info['files'][file_id]['filename']
                input_path = os.path.join(batch_upload_dir, filename)
                output_path = os.path.join(batch_upload_dir, filename.replace('.md', '.mp3'))
                
                if os.path.exists(input_path):
                    with open(input_path, 'r', encoding='utf-8') as f:
                        text = f.read()
                    
                    # 创建HTTP会话并调用TTS
                    async with aiohttp.ClientSession() as session:
                        success, status_code, error_detail = await async_text_to_speech(
                            session, text, output_path, voice, speed,
                            api_servers[server_id]['url'], 
                            api_servers[server_id].get('apiKey', api_servers[server_id].get('api_key', '')),
                            timeout_seconds=300  # 明确传递超时参数
                        )
                    
                    processing_time = time.time() - start_time
                    
                    if success:
                        server_completed[server_id] += 1
                        completed_tasks += 1
                        batch_info['completed_files'] = completed_tasks
                        batch_info['current_file'] = completed_tasks
                        
                        # 更新文件状态
                        if file_id in batch_info['files']:
                            file_info = batch_info['files'][file_id]
                            file_info['status'] = 'completed'
                            file_info['progress'] = 100
                            file_info['stage'] = '✅ 转换完成'
                        
                        # 成功后清除该服务器的冷却、重试计数和连续失败计数
                        server_cooldown_until[server_id] = 0.0
                        server_consecutive_failures[server_id] = 0  # 重置连续失败计数
                        if file_id in task_retries:
                            task_retries[file_id] = 0
                        
                        print(f"✅ 任务完成: {filename} (服务器: {server_name}, 耗时: {processing_time:.2f}秒)")
                    else:
                        server_failed[server_id] += 1
                        server_consecutive_failures[server_id] += 1  # 增加连续失败计数
                        if status_code is not None:
                            print(f"❌ 任务失败: {filename} (服务器: {server_name}, 状态码: {status_code}, 耗时: {processing_time:.2f}秒)")
                        else:
                            print(f"❌ 任务失败: {filename} (服务器: {server_name}, 耗时: {processing_time:.2f}秒)")
                        
                        # 服务器熔断：连续失败3次后熔断60秒
                        if server_consecutive_failures[server_id] >= 3:
                            server_cooldown_until[server_id] = time.time() + 60.0
                            print(f"🔥 服务器 {server_name} 熔断60秒 (连续失败{server_consecutive_failures[server_id]}次)")
                        else:
                            # 短暂冷却该服务器，避免持续派发到不健康节点
                            server_cooldown_until[server_id] = time.time() + 10.0
                        
                        # 更新文件状态
                        if file_id in batch_info['files']:
                            file_info = batch_info['files'][file_id]
                            file_info['status'] = 'failed'
                            file_info['progress'] = 100
                            file_info['stage'] = f'❌ 转换失败 (服务器: {server_name})'
                        
                        # 智能重试：指数退避
                        current_retry = task_retries.get(file_id, 0)
                        if current_retry < max_retries:
                            task_retries[file_id] = current_retry + 1
                            # 计算退避时间：2^retry + 随机抖动
                            backoff_time = (2 ** current_retry) + random.uniform(0, 1)
                            print(f"🔄 任务将重试 (第 {current_retry+1} 次)，将在 {backoff_time:.2f} 秒后执行...")
                            # 延迟重试
                            await asyncio.sleep(backoff_time)
                            await retry_queue.put(file_id)
                else:
                    print(f"❌ 文件不存在: {filename}")
                    current_retry = task_retries.get(file_id, 0)
                    if current_retry < max_retries:
                        task_retries[file_id] = current_retry + 1
                        await retry_queue.put(file_id)
                    
            except asyncio.TimeoutError:
                processing_time = time.time() - start_time
                server_failed[server_id] += 1
                server_consecutive_failures[server_id] += 1  # 增加连续失败计数
                print(f"⏰ 任务超时: {filename} (服务器: {server_name}, 耗时: {processing_time:.2f}秒)")
                
                # 服务器熔断：连续失败3次后熔断60秒
                if server_consecutive_failures[server_id] >= 3:
                    server_cooldown_until[server_id] = time.time() + 60.0
                    print(f"🔥 服务器 {server_name} 熔断60秒 (连续失败{server_consecutive_failures[server_id]}次)")
                else:
                    server_cooldown_until[server_id] = time.time() + 10.0
                
                # 更新文件状态
                if file_id in batch_info['files']:
                    file_info = batch_info['files'][file_id]
                    file_info['status'] = 'failed'
                    file_info['progress'] = 100
                    file_info['stage'] = f'⏰ 转换超时 (服务器: {server_name})'
                
                # 智能重试：指数退避
                current_retry = task_retries.get(file_id, 0)
                if current_retry < max_retries:
                    task_retries[file_id] = current_retry + 1
                    backoff_time = (2 ** current_retry) + random.uniform(0, 1)
                    print(f"🔄 任务将重试 (第 {current_retry+1} 次)，将在 {backoff_time:.2f} 秒后执行...")
                    await asyncio.sleep(backoff_time)
                    await retry_queue.put(file_id)
                
            except Exception as e:
                processing_time = time.time() - start_time
                server_failed[server_id] += 1
                server_consecutive_failures[server_id] += 1  # 增加连续失败计数
                print(f"❌ 任务异常: {filename} (服务器: {server_name}, 耗时: {processing_time:.2f}秒, 错误: {e})")
                
                # 服务器熔断：连续失败3次后熔断60秒
                if server_consecutive_failures[server_id] >= 3:
                    server_cooldown_until[server_id] = time.time() + 60.0
                    print(f"🔥 服务器 {server_name} 熔断60秒 (连续失败{server_consecutive_failures[server_id]}次)")
                else:
                    server_cooldown_until[server_id] = time.time() + 10.0
                
                # 更新文件状态
                if file_id in batch_info['files']:
                    file_info = batch_info['files'][file_id]
                    file_info['status'] = 'failed'
                    file_info['progress'] = 100
                    file_info['stage'] = f'💥 处理异常: {str(e)}'
                
                # 智能重试：指数退避
                current_retry = task_retries.get(file_id, 0)
                if current_retry < max_retries:
                    task_retries[file_id] = current_retry + 1
                    backoff_time = (2 ** current_retry) + random.uniform(0, 1)
                    print(f"🔄 任务将重试 (第 {current_retry+1} 次)，将在 {backoff_time:.2f} 秒后执行...")
                    await asyncio.sleep(backoff_time)
                    await retry_queue.put(file_id)
            
            finally:
                # 释放服务器（与派发时的预占对应，仅减一次）
                server_active[server_id] = max(0, server_active[server_id] - 1)
                active_tasks.discard(file_id)
                # 更新WebUI服务器状态
                batch_info['server_statuses'][server_id]['load'] = server_active[server_id]
                batch_info['server_statuses'][server_id]['completed_tasks'] = server_completed[server_id]
                if server_active[server_id] == 0:
                    batch_info['server_statuses'][server_id]['status'] = 'idle'
                elif server_active[server_id] >= server_capacity[server_id]:
                    batch_info['server_statuses'][server_id]['status'] = 'full'
                else:
                    batch_info['server_statuses'][server_id]['status'] = 'busy'
                print(f"🔄 服务器 {server_name} 已释放 (负载: {server_active[server_id]}/{server_capacity[server_id]})")
    
    # 主处理循环
    print(f"🎯 开始任务分配循环...")
    
    while completed_tasks < total_tasks:
        assigned_any = False
        # 尽可能在同一循环内填满所有可用容量
        while True:
            server_id = find_available_server()
            if server_id is None:
                break

            # 选择任务：重试优先
            file_id = None
            if not retry_queue.empty():
                try:
                    file_id = retry_queue.get_nowait()
                    print(f"🔄 重试任务: {file_id}")
                except asyncio.QueueEmpty:
                    pass
            if file_id is None and not task_queue.empty():
                try:
                    file_id = task_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            if file_id is None:
                break

            # 指数退避（重试任务）
            retries = task_retries.get(file_id, 0)
            if retries > 0:
                backoff = min(60.0, (2 ** (retries - 1)) + random.uniform(0, 0.5))
                await asyncio.sleep(backoff)

            # 预占容量
            server_active[server_id] += 1
            batch_info['server_statuses'][server_id]['load'] = server_active[server_id]
            batch_info['server_statuses'][server_id]['status'] = (
                'full' if server_active[server_id] >= server_capacity[server_id] else 'busy'
            )
            asyncio.create_task(process_task(file_id, server_id, pre_reserved=True))
            last_dispatch_index = (last_dispatch_index + 1) % max(1, len([i for i in range(num_servers) if server_capacity[i] > 0]))
            assigned_any = True

        if not assigned_any:
            # 无可派发则等待
            active_count = sum(server_active)
            total_capacity = sum(server_capacity)
            idle_count = total_capacity - active_count
            
            if active_count >= total_capacity:
                print(f"⏳ 所有服务器容量已满 ({active_count}/{total_capacity})，等待任务完成...")
                await asyncio.sleep(1)
            elif task_queue.empty() and retry_queue.empty():
                if active_count > 0:
                    print(f"📭 队列为空，等待 {active_count} 个正在运行的任务完成...")
                else:
                    print(f"✅ 所有任务处理完毕，系统空闲。")
                await asyncio.sleep(1)
            else:
                print(f"🔄 部分服务器空闲 ({idle_count}/{total_capacity})，继续分配任务...")
                await asyncio.sleep(0.2)
    
    # 等待所有活跃任务完成
    while active_tasks:
        print(f"⏳ 等待 {len(active_tasks)} 个活跃任务完成...")
        await asyncio.sleep(1)
    
    print(f"🎉 超简单负载均衡器处理完成")
    print(f"📊 最终统计: 完成 {completed_tasks}/{total_tasks} 个任务")
    
    # 输出详细的服务器统计
    print(f"📊 服务器性能统计:")
    for i in range(len(api_servers)):
        server_name = api_servers[i]['name']
        total_used = server_completed[i] + server_failed[i]
        if total_used > 0:
            success_rate = (server_completed[i] / total_used) * 100
            print(f"  🖥️ {server_name}:")
            print(f"    ✅ 完成任务: {server_completed[i]} 个")
            print(f"    ❌ 失败任务: {server_failed[i]} 个")
            print(f"    📈 成功率: {success_rate:.1f}%")
            print(f"    🔄 总使用: {total_used} 次")

if __name__ == '__main__':
    # 支持Docker部署，监听所有接口
    import os
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 5055))
    debug = os.environ.get('FLASK_ENV', 'development') == 'development'
    
    print(f"🚀 启动TTS批量转换服务...")
    print(f"📍 监听地址: {host}:{port}")
    print(f"🔧 调试模式: {debug}")
    if USE_SIMPLE_BALANCER:
        print(f"⚡ 使用动态工作节点负载均衡器 (V4)")
    else:
        print(f"⚡ 使用复杂负载均衡器（旧版路径）")
    
    app.run(host=host, port=port, debug=debug)
