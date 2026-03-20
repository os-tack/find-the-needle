FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends nginx curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask

WORKDIR /workspace

COPY app/ /workspace/app/
COPY nginx/app.conf /etc/nginx/sites-available/default
COPY start.sh /workspace/start.sh
COPY test.sh /workspace/test.sh

RUN chmod +x /workspace/start.sh /workspace/test.sh

# Remove the default nginx config that may conflict
RUN rm -f /etc/nginx/sites-enabled/default && \
    ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default

CMD ["/workspace/start.sh"]
