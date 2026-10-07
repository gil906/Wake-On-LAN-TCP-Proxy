FROM python:3.11-slim
WORKDIR /app
COPY wakeforward.py .
CMD ["python", "-u", "wakeforward.py"]
