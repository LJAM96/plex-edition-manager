# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    QT_X11_NO_MITSHM=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libx11-xcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxi6 \
        libxtst6 \
        libxrandr2 \
        libxkbcommon0 \
        libxkbcommon-x11-0 \
        libxdamage1 \
        libsm6 \
        libice6 \
        libfontconfig1 \
        libfreetype6 \
        libegl1 \
        libdbus-1-3 \
        libxcb-render0 \
        libxcb-shape0 \
        libxcb-shm0 \
        libxcb-xinerama0 \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libpango-1.0-0 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "edition-manager-gui.py"]
