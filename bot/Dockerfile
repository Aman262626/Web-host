FROM python:3.12-slim

# git: deploy from repos | nodejs/npm: run Node apps
RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "webhost.py"]
