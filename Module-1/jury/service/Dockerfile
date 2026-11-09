# The Module-1 jury service: relay + reference jurors (Module-1/jury/SPEC.md §12, §13).
# Build from the repository root (the compose file does this):
#   docker build -f Module-1/jury/service/Dockerfile -t vector-module1-jury .
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/Module-1:/app/shared/python:/app/Module-1/jury \
    APEX_NETWORK=mainnet

RUN pip install --no-cache-dir "pycardano==0.19.2" "cbor2>=5.6,<6" "requests>=2.31" \
 && groupadd --system --gid 10001 jury \
 && useradd --system --uid 10001 --gid jury --home-dir /var/lib/vector-jury --no-create-home \
            --shell /usr/sbin/nologin jury \
 && mkdir -p /var/lib/vector-jury \
 && chown jury:jury /var/lib/vector-jury \
 && chmod 700 /var/lib/vector-jury

WORKDIR /app
COPY shared/python/vector_deploy.py shared/python/aiken_simulate.py /app/shared/python/
COPY Module-1/simulation /app/Module-1/simulation
COPY Module-1/deploy/identity /app/Module-1/deploy/identity
COPY Module-1/jury/vector_jury /app/Module-1/jury/vector_jury
COPY Module-1/jury/jurors /app/Module-1/jury/jurors
COPY Module-1/jury/deploy /app/Module-1/jury/deploy
COPY Module-1/jury/service/config.example.json /app/Module-1/jury/service/config.example.json

USER jury
VOLUME ["/var/lib/vector-jury"]
ENTRYPOINT ["python", "-m", "vector_jury.service"]
CMD ["--config", "/config/config.json"]
