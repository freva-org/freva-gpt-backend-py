# Pull from fedora because alpine and ubuntu both have issues with python env or podman
FROM fedora:latest

# ---------------------
# General installs
# ---------------------

# Install necessary dependencies for miniconda
RUN dnf install -y curl bzip2 awk git

RUN mkdir /app
WORKDIR /app

# -------------------
# Conda setup
# -------------------

# Download and install Miniconda (Miniforge)
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) MINIFORGE_ARCH="x86_64" ;; \
      arm64) MINIFORGE_ARCH="aarch64" ;; \
      *) echo "Unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/miniforge.sh \
      "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-${MINIFORGE_ARCH}.sh"; \
    bash /tmp/miniforge.sh -b -p /opt/conda; \
    rm -f /tmp/miniforge.sh

ENV PATH="/opt/conda/bin:$PATH"

# Create environment
RUN conda create --name env "python>=3.11.10,<3.12"
RUN conda init bash
RUN echo "conda activate env" >> ~/.bashrc

# -----------------
# uv and deps
# -----------------

# Install uv into the env 
RUN conda run -n env python -m pip install --no-cache-dir uv

COPY docker/rag-server/pyproject.toml /app/pyproject.toml

# Export requirements from pyproject.toml into a file
RUN conda run -n env uv export --format requirements-txt --no-hashes -o /tmp/requirements.txt
# Install exactly those requirements into the env's interpreter
RUN conda run -n env uv pip install --no-cache-dir --system \
    --python "/opt/conda/envs/env/bin/python" \
    -r /tmp/requirements.txt

# Python env paths
ENV PATH="/opt/conda/envs/env/bin/:$PATH"
ENV LD_LIBRARY_PATH="/opt/conda/envs/env/lib/:${LD_LIBRARY_PATH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

EXPOSE ${FREVAGPT_MCP_PORT:-8050}

COPY src /app/src

# Launch CI server
CMD uvicorn src.tools.rag.server:app \
    --host ${FREVAGPT_MCP_HOST:-0.0.0.0}\
    --port ${FREVAGPT_MCP_PORT:-8050} \
    --proxy-headers