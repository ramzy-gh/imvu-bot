FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PORT=3000
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py personality.py permissions.py identity.py state_store.py ./
COPY offline_chat.py offline_seed.txt ./
COPY wardrobe.py ./
COPY room_control.py ./
COPY local_control.py botctl.py ./
RUN useradd --create-home bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot
EXPOSE 3000
CMD ["python", "bot.py"]
