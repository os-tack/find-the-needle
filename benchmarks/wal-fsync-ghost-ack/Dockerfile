FROM golang:1.22-alpine AS builder

WORKDIR /build
COPY app/ .

RUN go build -o kvwal ./...

FROM alpine:3.20

RUN apk add --no-cache bash coreutils git && \
    git config --global user.email "bench@needle-bench.cc" && \
    git config --global user.name "needle-bench"

# Install Go toolchain so the agent can rebuild after edits
RUN apk add --no-cache go

COPY --from=builder /build/kvwal /usr/local/bin/kvwal

WORKDIR /workspace
COPY app/ .

COPY test.sh .
RUN chmod +x test.sh

CMD ["kvwal", "serve"]
