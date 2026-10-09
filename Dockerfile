# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
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
COPY . .
RUN chmod -R a+rX /app \
    && groupadd --gid 10001 core && useradd --uid 10001 --gid core --no-create-home core \
    && mkdir -p staticfiles && chown core:core staticfiles
USER core
EXPOSE 8000
CMD [".venv/bin/gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
