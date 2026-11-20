FROM python:3.11-slim
RUN apt-get update && apt-get install -y aria2 ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD aria2c --enable-rpc --rpc-listen-port=6800 --daemon=true --seed-time=0 \
    --max-connection-per-server=8 --bt-stop-timeout=600 -d /app/downloads \
    && python bot.py
