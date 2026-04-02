ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Build context is the trading-bot/ directory (set by Home Assistant supervisor).
# backend/ and frontend/ are committed directly inside this folder.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip3 install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
