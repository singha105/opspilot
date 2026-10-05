FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY shopfront_svc.py .

USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "/app/shopfront_svc.py"]
