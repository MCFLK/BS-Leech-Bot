FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y aria2 ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD aria2c --enable-rpc --rpc-listen-port=6800 --daemon=true \
    --seed-time=0 --file-allocation=none --max-concurrent-downloads=2 \
    --bt-stop-timeout=900 -d /app/downloads \
    && python bot.py
