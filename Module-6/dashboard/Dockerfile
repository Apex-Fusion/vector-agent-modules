FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GAME6_ROOT=/app

WORKDIR /app

# Build context is Module-6/ (see docker-compose.yml build.context),
# so paths below are relative to Module-6/.
COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dashboard/server.py .
COPY dashboard/static/ ./static/
COPY deploy/testnet/deployment.json ./deploy/testnet/deployment.json
COPY deploy/mainnet/deployment.json ./deploy/mainnet/deployment.json

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
