FROM python:3.12-slim

# "stable" for the normal image; "v2-broken" builds the faulty release used by evals.
ARG BUILD=stable
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHOPFRONT_BUILD=${BUILD}

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY shopfront_svc.py .

USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "/app/shopfront_svc.py"]
