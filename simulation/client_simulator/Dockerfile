FROM python:3.11-alpine

WORKDIR /app

RUN apk add --no-cache curl bash iputils

COPY client_simulator.py /app/client_simulator.py

ENTRYPOINT ["python", "/app/client_simulator.py"]
