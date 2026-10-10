# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN --mount=type=secret,id=build_ca,required=false \
    set -eu; \
    if [ -f /run/secrets/build_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/build_ca > /tmp/build-ca.pem; \
      export SSL_CERT_FILE=/tmp/build-ca.pem PIP_CERT=/tmp/build-ca.pem; \
    fi; \
    pip install --no-cache-dir uv==0.12.19; \
    rm -f /tmp/build-ca.pem
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=build_ca,required=false \
    set -eu; \
    if [ -f /run/secrets/build_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/build_ca > /tmp/build-ca.pem; \
      export SSL_CERT_FILE=/tmp/build-ca.pem; \
    fi; \
    uv sync --frozen --no-dev --system-certs; \
    rm -f /tmp/build-ca.pem
COPY server.py identity_client.py integration_client.py core_client.py app-spec.json index.html ui.js test_server.py security-contract.json ./
RUN chmod -R a+rX /app && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app && mkdir /data && chown app:app /data
USER app
EXPOSE 8080
CMD [".venv/bin/gunicorn", "server:application", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "1", "--access-logfile", "/dev/null"]
