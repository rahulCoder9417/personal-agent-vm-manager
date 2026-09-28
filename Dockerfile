# vm-manager image: static Go binary on distroless.
# Build: docker build -t personal-agent/vm-manager:dev .
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
COPY gen ./gen
COPY migrations ./migrations
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/vm-manager ./cmd/vm-manager

FROM gcr.io/distroless/static-debian12
# Runs as root on purpose: it needs the Docker socket (root-equivalent anyway) and
# writes the Squid allowlists + snapshots into mounted volumes. Keep this container
# on the platform + sandbox networks only; never publish its port.
COPY --from=build /out/vm-manager /vm-manager
ENV GRPC_ADDR=:9090
EXPOSE 9090
ENTRYPOINT ["/vm-manager"]
