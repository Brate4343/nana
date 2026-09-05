FROM python:3.10-slim
WORKDIR /app
requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Render'ın atadığı portu dinamik olarak yakalaması için $PORT kullanıyoruz
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 bot:app
