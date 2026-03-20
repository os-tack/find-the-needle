FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends bash curl jq git && \
    rm -rf /var/lib/apt/lists/* && \
    git config --global user.email "bench@needle-bench.cc" && \
    git config --global user.name "needle-bench"

WORKDIR /workspace

COPY app/ app/
RUN pip install --no-cache-dir -r app/requirements.txt

COPY test.sh .
RUN chmod +x test.sh

CMD ["python3", "app/server.py"]
