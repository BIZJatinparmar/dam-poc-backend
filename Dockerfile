FROM python:3.14-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini start.sh ./
COPY alembic ./alembic
COPY atlas_backend ./atlas_backend
RUN chmod +x start.sh
ENV ATLAS_DATA_DIR=/tmp/atlas
EXPOSE 8000
CMD ["./start.sh"]
