FROM python:3.11-slim

WORKDIR /app

# Só o necessário para o tracker
RUN pip install --no-cache-dir flask

# Copia APENAS o tracker (ignora todo o resto do projeto)
COPY tracker.py .

# Fly.io injeta PORT via env var
ENV PORT=8080

EXPOSE 8080

CMD ["python", "tracker.py"]
