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

COPY pyproject.toml /app/pyproject.toml

# Export requirements from pyproject.toml into a file
RUN conda run -n env uv export --format requirements-txt --no-hashes -o /tmp/requirements.txt
# Install exactly those requirements into the env's interpreter
RUN conda run -n env uv pip install --no-cache-dir --system \
    --python "/opt/conda/envs/env/bin/python" \
    -r /tmp/requirements.txt

# -------------------------
# Config + source
# -------------------------

# The litellm also needs to be copied over to the backend so that the list of chatbots can be extracted
COPY litellm_config.yaml /app/litellm_config.yaml

# Python env paths
ENV PATH="/opt/conda/envs/env/bin/:$PATH"
ENV LD_LIBRARY_PATH="/opt/conda/envs/env/lib/:${LD_LIBRARY_PATH}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Copy source code
COPY src /app/src
COPY tests /app/tests

# -------------------------
# Launch
# -------------------------

# ENTRYPOINT ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8502", "--proxy-headers"]
ENTRYPOINT ["/bin/sh", "-c", "\
  if [ \"${FREVAGPT_DEBUG,,}\" = \"true\" ] || [ \"${FREVAGPT_DEBUG}\" = \"1\" ]; then \
    echo \"[backend] DEBUG on → waiting for debugger on ${FREVAGPT_DEBUG_PORT:-5678}\"; \
    exec python -m debugpy --listen 0.0.0.0:${FREVAGPT_DEBUG_PORT:-5678} --wait-for-client \
         -m uvicorn src.app:app --host 0.0.0.0 --port ${FREVAGPT_BACKEND_PORT:-8502} --proxy-headers; \
  else \
    echo \"[backend] DEBUG off → starting plain uvicorn\"; \
    exec uvicorn src.app:app --host 0.0.0.0 --port ${FREVAGPT_BACKEND_PORT:-8502} --proxy-headers; \
  fi"]