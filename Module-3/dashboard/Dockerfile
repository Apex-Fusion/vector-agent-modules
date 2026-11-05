FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODULE3_ROOT=/app \
    DEPLOYMENT_NETWORK=testnet

WORKDIR /app

COPY dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# SDK + indexer package (for indexer.api, indexer.storage, reputation_staking)
COPY python/ ./python/

# Dashboard server + static assets
COPY dashboard/server.py ./dashboard/server.py
COPY dashboard/static/ ./dashboard/static/

# Deploy state (consumed by /api/config)
COPY deploy/deploy_state.json ./deploy/deploy_state.json
COPY deploy/mainnet/deploy_state.json ./deploy/mainnet/deploy_state.json
# Published state of the identity release; the identity instance selects it
# with MODULE3_DEPLOY_STATE and --deploy-state (docker-compose.apex-web-identity.yml).
COPY deploy/identity/mainnet/deploy_state.json ./deploy/identity/mainnet/deploy_state.json

WORKDIR /app/dashboard
ENV PYTHONPATH=/app/python

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
