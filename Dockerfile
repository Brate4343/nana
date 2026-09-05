FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Port değişkenini doğrudan shell üzerinden aktarıyoruz
CMD gunicorn bot:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
