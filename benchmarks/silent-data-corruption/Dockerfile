FROM rust:1.82-alpine AS builder

RUN apk add --no-cache musl-dev

WORKDIR /build
COPY Cargo.toml .
COPY src/ src/

RUN cargo build --release

FROM alpine:3.20

RUN apk add --no-cache bash coreutils python3

COPY --from=builder /build/target/release/fileproc /usr/local/bin/fileproc

WORKDIR /work

COPY test.sh /test.sh
RUN chmod +x /test.sh

# Copy source for the agent to inspect and fix
COPY Cargo.toml /work/Cargo.toml
COPY src/ /work/src/

# Install Rust toolchain in final image so agent can rebuild
RUN apk add --no-cache curl gcc musl-dev && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.82.0 && \
    rm -rf /var/cache/apk/*

ENV PATH="/root/.cargo/bin:${PATH}"

CMD ["fileproc"]
