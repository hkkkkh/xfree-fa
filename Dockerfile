FROM python:3.12-slim
WORKDIR /app
COPY proxy/ ./proxy/
EXPOSE 10000
CMD ["python", "proxy/app.py"]
