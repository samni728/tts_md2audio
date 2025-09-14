#!/bin/bash

# TTS批量转换工具 - Docker停止脚本

echo "🛑 停止TTS批量转换工具..."

# 停止并删除容器
docker-compose down

echo "✅ 服务已停止"
