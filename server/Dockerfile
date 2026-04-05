FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY web/ ./web/

ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}


EXPOSE 8457
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8457"]
