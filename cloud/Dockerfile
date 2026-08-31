# Fables cloud — stateless MCP service + sync API.
# Built for AWS Lightsail (or any Docker host). Caddy terminates TLS and
# reverse-proxies to the stdlib server on :8000.
FROM python:3.14-slim

WORKDIR /app
COPY mcp_protocol.py providers.py ./
COPY cloud/ ./cloud/

EXPOSE 8000
CMD ["python3", "cloud/fables-cloud.py", "--port", "8000", "--data", "/data"]
