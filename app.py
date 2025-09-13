import os
import re
import sys
import uuid
import time
import json
import asyncio
import aiohttp
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import requests
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MP3_FOLDER'] = 'mp3'
app.config['ALLOWED_EXTENSIONS'] = {'md'}
app.secret_key = 'super-secret-key'  # 生产环境请替换为随机字符串

# 确保目录存在
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['MP3_FOLDER'], exist_ok=True)

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
        # 异步发送请求并获取响应
        timeout = aiohttp.ClientTimeout(total=300)  # 5分钟超时
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
        print(f"TTS转换超时 ({api_url})", file=sys.stderr)
        return False
    except Exception as e:
        print(f"TTS转换失败 ({api_url}): {str(e)}", file=sys.stderr)
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
        'files': {}
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

def run_async_processing(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency):
    """运行异步处理的主函数"""
    try:
        # 创建新的事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 运行异步处理
        loop.run_until_complete(process_files_async(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency))
        
    except Exception as e:
        print(f"异步处理异常: {str(e)}", file=sys.stderr)
    finally:
        loop.close()

async def process_files_async(batch_id, batch_upload_dir, voice, speed, api_servers, concurrency):
    """异步处理文件，使用真正的并发"""
    if batch_id not in batch_status:
        return
    
    batch_info = batch_status[batch_id]
    files_to_process = list(batch_info['files'].keys())
    
    # 调试日志：显示异步处理配置
    print(f"🚀 开始异步处理:")
    print(f"  📁 批次ID: {batch_id}")
    print(f"  📄 文件数量: {len(files_to_process)}")
    print(f"  🖥️ 可用服务器: {len(api_servers)}")
    print(f"  ⚡ 并发度: {concurrency}")
    
    # 创建aiohttp会话
    connector = aiohttp.TCPConnector(limit=concurrency * 2)  # 连接池限制
    timeout = aiohttp.ClientTimeout(total=300)  # 5分钟总超时
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # 创建信号量来控制并发数
        semaphore = asyncio.Semaphore(concurrency)
        
        # 创建所有任务
        tasks = []
        for file_id in files_to_process:
            task = process_single_file_async(session, semaphore, batch_id, batch_upload_dir, voice, speed, api_servers, file_id)
            tasks.append(task)
        
        # 使用asyncio.gather同时执行所有任务
        print(f"📤 同时提交 {len(tasks)} 个TTS请求...")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        success_count = 0
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                print(f"❌ 任务 {i+1} 异常: {result}")
            elif result:
                success_count += 1
        
        print(f"🎉 异步处理完成: {success_count}/{len(files_to_process)} 个文件成功")
        print(f"📊 使用了 {len(api_servers)} 个服务器，并发度: {concurrency}")

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

if __name__ == '__main__':
    app.run(debug=True)