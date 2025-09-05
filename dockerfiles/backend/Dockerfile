# Pull from fedora because alpine and ubuntu both have issues with python env or podman
FROM fedora:latest

# ---------------------
# General installs
# ---------------------

# Install necessary dependencies for miniconda
RUN dnf install -y curl bzip2 awk git

RUN mkdir /app

# # Install unzip for Crocodash
# RUN dnf install unzip -y

# # Download the Bathymatry dataset (GEBCO 2024) for Crocodash. It's up here to be cache-friendly.
# RUN mkdir /app
# RUN curl https://www.bodc.ac.uk/data/open_download/gebco/gebco_2024/zip/ -o /app/gebco_2024.zip

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

# Install Python deps via uv (this replaces `pip install ...`)
RUN source activate env && uv pip install \
    freva-client dill numpy matplotlib pandas xarray xesmf scipy netcdf4 cartopy contourpy \
    geopy aiohttp requests scikit-learn geopandas healpy easygems astropy 

# These libraries cannot be installed by uv, so we use conda.
RUN conda install --yes -c conda-forge -n env pytorch

# # Install necessary dependencies for CrocoDash, also not found on pypi.
# RUN conda install --yes -c conda-forge -n env "esmpy>=8.6.1,<8.7.0"

# -----------------
# Crocodash setup
# -----------------

# RUN git config --global user.email "freva@dkrz.de"
# RUN git config --global user.name "clint"
# RUN git clone --recurse-submodules https://github.com/CROCODILE-CESM/CrocoDash.git
# RUN source activate env && uv pip install -e CrocoDash/CrocoDash/rm6/ \
#     -e CrocoDash/CrocoDash/visualCaseGen/external/ipyfilechooser/ \
#     -e CrocoDash/CrocoDash/visualCaseGen/external/mom6_bathy/ \
#     -e CrocoDash/CrocoDash/visualCaseGen/ \
#     "myst_parser>=4.0.0,<4.1.0" \
#     -e CrocoDash/
# RUN source activate env && pytest CrocoDash/tests/test_installation.py

# RUN git clone https://github.com/ESCOMP/CESM.git
# RUN git -C CESM checkout cesm3_0_beta03
# RUN source activate env && ./CESM/bin/git-fleximod -C /app/CESM/ update
# ENV CIME_MACHINE="ubuntu-latest"

# -------------------------
# Config + source
# -------------------------

# Copy over the secrets file to have access to the openai account and so that the backend is configured correctly
COPY .env /app/.env
# The litellm also needs to be copied over to the backend so that the list of chatbots can be extracted
COPY litellm_config.yaml /app/litellm_config.yaml

# Python env paths
ENV PYTHONUSERBASE /opt/conda/envs/env
ENV PYTHONPATH=/opt/conda/envs/env/lib/python3.11/site-packages
ENV PATH /opt/conda/envs/env/bin/:$PATH
ENV LD_LIBRARY_PATH /opt/conda/envs/env/lib/:$PATH

# Copy source code
COPY src /app/src
COPY test /app/test

# -------------------------
# Launch
# -------------------------

ENTRYPOINT ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8502", "--proxy-headers"]
