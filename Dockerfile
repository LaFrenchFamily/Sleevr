FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py pokemon_fr.json ./
COPY static ./static
ENV DATA_DIR=/data PORT=8000 TZ=Europe/Paris
VOLUME /data
EXPOSE 8000
# 1 seul worker : le relevé quotidien des prix tourne dans le processus
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8000", "app:app"]
