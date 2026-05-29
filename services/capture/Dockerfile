FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install shared contracts first (copied from repo root build context).
COPY shared/veilwatch_contracts /opt/veilwatch_contracts
RUN pip install --no-cache-dir /opt/veilwatch_contracts

COPY services/capture/pyproject.toml ./pyproject.toml
COPY services/capture/src ./src
RUN pip install --no-cache-dir .

RUN mkdir -p /var/veilwatch/frames

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "capture.main"]
