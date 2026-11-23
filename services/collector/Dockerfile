# Read-only Binance USD-M market data recorder.
# Build from the repository root:  docker build -f services/collector/Dockerfile -t whale-trader/collector:0.1.0 .
FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /opt/app
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --no-editable --package wt-collector

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin wt \
    && mkdir -p /data && chown wt /data
COPY --from=build /opt/app/.venv /opt/app/.venv
ENV PATH=/opt/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER wt
VOLUME ["/data"]
EXPOSE 9108
ENTRYPOINT ["wt-collector"]
CMD ["run", "--config", "/etc/wt/collector.yaml"]
