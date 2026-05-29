FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# OpenALPR is not in modern Debian repos. To enable ALPR_BACKEND=openalpr:
#   1. Build a derived image FROM this one that compiles OpenALPR from source
#      (https://github.com/openalpr/openalpr), OR
#   2. Replace this base with an OpenALPR-bundled image and keep the rest.
# The mock backend is the default and requires no extra binary.

WORKDIR /app

COPY shared/veilwatch_contracts /opt/veilwatch_contracts
RUN pip install --no-cache-dir /opt/veilwatch_contracts

COPY services/detection/pyproject.toml ./pyproject.toml
COPY services/detection/src ./src
RUN pip install --no-cache-dir .

# Pre-fetch the default YOLO weights at build time so the first container
# start isn't gated on a download over the camera network.
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "detection.main"]
