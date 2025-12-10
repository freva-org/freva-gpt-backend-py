# Pull from fedora because alpine and ubuntu both have issues with python env or podman
FROM fedora:latest

# ---------------------
# General installs
# ---------------------

# Install necessary dependencies for miniconda
RUN dnf install -y curl bzip2 awk git

RUN mkdir /app



# -------------------
# Conda setup
# -------------------

# Download and install Miniconda (Miniforge)
RUN curl -Lo /tmp/miniforge.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh && \
    bash /tmp/miniforge.sh -b -p /opt/conda && \
    rm /tmp/miniforge.sh

ENV PATH="/opt/conda/bin:$PATH"

# Create environment
RUN conda create --name env "python>=3.11.10,<3.12"
RUN conda init bash
RUN echo "conda activate env" >> ~/.bashrc

WORKDIR /app

# -----------------
# uv and deps
# -----------------

# Uv is a much faster package manager and installer, we'll use it over conda where possible to cut time.
RUN pip install uv

COPY pyproject.toml /app/pyproject.toml

# Export from pyproject and install into the current interpreter (Conda)
# --system is required when mutating a non-venv interpreter
RUN source activate env && uv export --format requirements-txt \
    | uv pip install --no-cache-dir --system --python "$(python -c 'import sys; print(sys.executable)')" -r -

# -------------------------
# Config + source
# -------------------------

# The litellm also needs to be copied over to the backend so that the list of chatbots can be extracted
COPY litellm_config.yaml /app/litellm_config.yaml

# Python env paths
ENV PYTHONUSERBASE /opt/conda/envs/env
ENV PYTHONPATH=/opt/conda/envs/env/lib/python3.11/site-packages
ENV PATH /opt/conda/envs/env/bin/:$PATH
ENV LD_LIBRARY_PATH /opt/conda/envs/env/lib/:$PATH

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