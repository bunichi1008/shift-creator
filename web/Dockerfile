FROM golang:1.26-alpine AS build
WORKDIR /src
COPY web/ .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /shift-web .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates && addgroup -S app && adduser -S -G app app \
    && mkdir -p /data && chown app:app /data
COPY --from=build /shift-web /usr/local/bin/shift-web
COPY contracts/demo.json /app/contracts/demo.json
ENV DEMO_DATA_PATH=/app/contracts/demo.json DATA_DIR=/data LISTEN_ADDR=:8080
USER app
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/shift-web"]
