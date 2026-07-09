FROM debian:bookworm-slim AS builder

RUN apt-get update -qq && apt-get install -y --no-install-recommends gcc make libc6-dev linux-libc-dev

WORKDIR /build
COPY src/ .

RUN make

FROM debian:bookworm-slim

RUN apt-get update -qq && apt-get install -y --no-install-recommends bash git gcc make libc6-dev linux-libc-dev && \
    git config --global user.email "bench@needle-bench.cc" && \
    git config --global user.name "needle-bench"

COPY --from=builder /build/devctl /usr/local/bin/devctl

WORKDIR /app

COPY . .
RUN chmod +x test.sh

CMD ["devctl"]
