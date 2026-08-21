FROM python:3.12-slim-bookworm

# Domestic Debian mirrors (DEB822), see apt.sources
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
COPY apt.sources /etc/apt/sources.list.d/debian.sources

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    ffmpeg \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  --trusted-host pypi.tuna.tsinghua.edu.cn \
  -r /app/requirements.txt
# The reviewed HTML import endpoint runs its browser QA before objects become
# publishable. Chromium is installed in the image, never on the host volume.
RUN playwright install --with-deps chromium

COPY app /app/app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY scripts /app/scripts

ENV PYTHONUNBUFFERED=1
EXPOSE 8100

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8100"]
