FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime packages for `python -m web.dashboard` (WORKDIR /app is PYTHONPATH).
COPY data/ data/
COPY web/ web/
COPY alertsdb/ alertsdb/
COPY market/ market/
COPY notify/ notify/

EXPOSE 8080
CMD ["python", "-m", "web.dashboard"]
