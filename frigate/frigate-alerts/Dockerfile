FROM python:3.11-slim

WORKDIR /app

# Install Docker CLI for disk monitoring
RUN apt-get update && \
    apt-get install -y docker.io && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir paho-mqtt requests

COPY alerts.py .

CMD ["python", "-u", "alerts.py"]
