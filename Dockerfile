# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
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
# The laptop simulation has no SDK dependency. The deployed Core worker needs
# this pinned SDK to assume its separately onboarded deployment role.
RUN --mount=type=secret,id=build_ca,required=false \
    set -eu; \
    if [ -f /run/secrets/build_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/build_ca > /tmp/build-ca.pem; \
      export SSL_CERT_FILE=/tmp/build-ca.pem; \
    fi; \
    uv pip install --python .venv/bin/python --system-certs --requirements deployment/cloud-runner-requirements.txt; \
    rm -f /tmp/build-ca.pem
RUN chmod -R a+rX /app \
    && groupadd --gid 10001 core && useradd --uid 10001 --gid core --no-create-home core \
    && mkdir -p staticfiles && chown core:core staticfiles
USER core
EXPOSE 8000
CMD [".venv/bin/gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
