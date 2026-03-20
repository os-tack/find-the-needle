FROM golang:1.22-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postgresql \
        postgresql-client \
        curl \
        python3 \
        sudo && \
    rm -rf /var/lib/apt/lists/*

# Set up PostgreSQL data directory with correct permissions
RUN mkdir -p /var/lib/postgresql/data /var/run/postgresql /var/log && \
    chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql && \
    chmod 700 /var/lib/postgresql/data && \
    touch /var/log/postgresql.log && \
    chown postgres:postgres /var/log/postgresql.log

WORKDIR /workspace

# Copy Go source and build
COPY app/go.mod app/go.sum /workspace/app/
RUN cd /workspace/app && go mod download

COPY app/ /workspace/app/
RUN cd /workspace/app && go build -o /workspace/orders-api .

# Copy migrations
COPY migrations/ /workspace/migrations/

# Copy scripts
COPY start.sh /workspace/start.sh
COPY test.sh /workspace/test.sh
RUN chmod +x /workspace/start.sh /workspace/test.sh

CMD ["/workspace/start.sh"]
