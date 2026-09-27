FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY registerer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY utils ./utils
COPY registerer/register.py ./
