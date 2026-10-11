FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY app ./app

RUN mkdir -p /data

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import os,time; assert time.time()-os.path.getmtime('/data/heartbeat') < 1200" || exit 1

CMD ["python", "-m", "app.main"]
