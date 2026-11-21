FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY server.py ./
COPY webapp ./webapp
CMD ["python", "server.py"]
