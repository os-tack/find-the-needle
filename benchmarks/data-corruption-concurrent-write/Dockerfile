FROM rust:1.82-alpine AS builder

RUN true  # all packages pre-installed 

WORKDIR /build
COPY Cargo.toml .
COPY src/ src/

RUN cargo build --release

FROM debian:bookworm-slim

RUN apt-get update -qq && apt-get install -y --no-install-recommends bash python3 git && \
    git config --global user.email "bench@needle-bench.cc" && \
    git config --global user.name "needle-bench"

COPY --from=builder /build/target/release/concurrent-writer /usr/local/bin/concurrent-writer

WORKDIR /app

COPY . .
RUN chmod +x test.sh

# Install Rust toolchain in final image so agent can rebuild
RUN apt-get update -qq && apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.82.0 && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

CMD ["concurrent-writer"]
