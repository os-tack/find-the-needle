# needle-bench template Dockerfile
# Replace this with your real broken codebase.
#
# Requirements:
#   - Build time: < 5 minutes on 4-core runner
#   - Image size: < 500 MB
#   - Must contain a real bug
#   - Must NOT include the solution patch
#   - Must NOT require network access at test time

FROM alpine:3.19

# Install build dependencies
RUN apk add --no-cache build-base

# Copy the broken source code
WORKDIR /workspace
COPY src/ /workspace/src/

# Build the project (it should compile, but produce incorrect behavior)
# RUN cd src && make

# The test should FAIL in this state
CMD ["./test.sh"]
