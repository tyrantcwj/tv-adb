FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends adb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
COPY server ./server

ENV PORT=8765 \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data", "/root/.android"]
EXPOSE 8765
CMD ["python", "app.py"]
