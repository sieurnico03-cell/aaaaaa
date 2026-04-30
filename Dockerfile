FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ships.json ./data/ships.json
COPY data/ship_images.json ./data/ship_images.json
COPY run.py .

ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

CMD ["python", "run.py"]
