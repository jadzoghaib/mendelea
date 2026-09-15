# The public evidence demo: one stateless container, one read-only file.
#
# There is no database service, no volume and no object storage, because the
# web layer never reads a Parquet file -- it only queries DuckDB tables. So
# the whole deployable artefact is the warehouse that `mendelea export-public`
# writes: the evidence timeline with the build intermediates and both private
# planes removed. On the 31-gene panel that is 58 MB against 308 MB.
#
# Build it first, then build this image:
#
#   mendelea export-public --out mendelea-public.duckdb
#   docker build -t mendelea .
#   docker run --rm -p 8000:8000 mendelea
#
# Stateless is the point: nothing is written at runtime, so a restart loses
# nothing, scale-to-zero costs nothing, and a compromised container holds no
# customer data because none was ever copied in.

# DuckDB is a ~40 MB wheel and pip's default 15s socket timeout gives up on a
# slow link mid-download, which failed this build once for no reason worth
# debugging twice.
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=5

FROM python:3.12-slim AS build
ARG PIP_DEFAULT_TIMEOUT
ARG PIP_RETRIES
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} PIP_RETRIES=${PIP_RETRIES}

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src/ ./src/
# --no-compile keeps the wheel free of .pyc that the runtime layer rebuilds anyway
RUN pip install --no-cache-dir --no-compile build \
 && python -m build --wheel --outdir /dist


FROM python:3.12-slim
ARG PIP_DEFAULT_TIMEOUT
ARG PIP_RETRIES
ENV PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} PIP_RETRIES=${PIP_RETRIES}

# Nothing here runs as root, and nothing needs to: the process reads one file
# and binds one port.
RUN useradd --create-home --uid 10001 mendelea
WORKDIR /app

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# The panel definitions travel with the image so the gene picker can filter to
# the panel's own genes rather than showing every flanking neighbour.
COPY panels/ /app/panels/

# The artefact. Built by `mendelea export-public`, deliberately not by this
# Dockerfile: producing it needs the full warehouse, which is tens of
# gigabytes of ingested evidence and has no business in a build context.
#
# Owned by the runtime user, because `config.load()` creates the cache and
# reference directories under MENDELEA_DATA_DIR on startup. It does not
# matter that nothing is ever written to them -- a root-owned /data means the
# process dies at boot rather than at first use, which is a worse way to find
# out.
RUN mkdir -p /data && chown mendelea:mendelea /data
COPY --chown=mendelea:mendelea mendelea-public.duckdb /data/mendelea.duckdb

ENV MENDELEA_DATA_DIR=/data \
    MENDELEA_PANEL_DIR=/app/panels \
    MENDELEA_RATE_PER_MINUTE=120 \
    MENDELEA_RATE_BURST=40 \
    PYTHONUNBUFFERED=1

USER mendelea
EXPOSE 8000

# The probe asks the database one question rather than building the context,
# which would be most of a second on a cold process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys,json; \
r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)); \
sys.exit(0 if r.get('timeline') else 1)"

CMD ["mendelea", "serve", "--host", "0.0.0.0", "--port", "8000"]
