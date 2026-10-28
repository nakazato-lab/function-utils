FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY registerer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY utils ./utils
COPY registerer/register.py ./
