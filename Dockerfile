FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/scheduler.py /app/scheduler.py
COPY src/templates /app/templates
COPY src/static /app/static

CMD ["python", "/app/scheduler.py"]
