FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY invoker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY utils ./utils
COPY invoker/invoke.py ./
COPY ndn-scripts/use-func/use_average.ndn ./use_average.ndn
