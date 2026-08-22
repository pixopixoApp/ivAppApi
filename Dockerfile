FROM python:3.12-slim-bookworm

# Set to false to skip Playwright/Chromium (e.g. offline local builds).
# HTML import browser QA then won't be available, but the API/worker run fine.
ARG INSTALL_PLAYWRIGHT=true

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
# Skippable with --build-arg INSTALL_PLAYWRIGHT=false for offline local runs.
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
      playwright install --with-deps chromium; \
    fi

COPY app /app/app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
COPY scripts /app/scripts

ENV PYTHONUNBUFFERED=1
EXPOSE 8100

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8100"]
