FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY COPY backend/requirements.txt  .
RUN pip install --no-cache-dir -r requirements.txt

COPY COPY backend/ .

EXPOSE 8000

CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120"]
