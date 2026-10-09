FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chmod=644 examples/marketplace_app.py ./app.py
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
