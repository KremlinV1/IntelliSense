FROM python:3.11-slim

WORKDIR /app

COPY shared/veilwatch_contracts /opt/veilwatch_contracts
RUN pip install --no-cache-dir /opt/veilwatch_contracts

COPY services/tracker/pyproject.toml ./pyproject.toml
COPY services/tracker/src ./src
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "tracker.main"]
