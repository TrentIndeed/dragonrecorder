FROM python:3.12-slim

WORKDIR /app

# ffmpeg: cut edits (remove fillers/silences) render here now, so toggling
# them keeps working when the desktop app is closed
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/app/data \
    DB_PATH=/app/data/dragonrecorder.sqlite3

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
