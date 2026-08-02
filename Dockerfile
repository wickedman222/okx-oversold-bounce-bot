FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install deps first (better Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY config.py main.py ./
COPY bot/ ./bot/

# Railway injects PORT; main.py starts a health server on it
EXPOSE 8080
CMD ["python", "-u", "main.py"]
