FROM ghcr.io/osgeo/gdal:ubuntu-small-3.8.5

ENV CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    USE_PYGEOS=0

RUN apt-get update \
    && apt-get install -y \
    # Build tools
    build-essential \
    git \
    python3-pip \
    # For Psycopg2
    libpq-dev python3-dev \
    # For SSL
    ca-certificates \
    # Building wheel for shapely
    libgeos-dev \
    # Tidy up
    && apt-get autoclean && \
    apt-get autoremove && \
    rm -rf /var/lib/{apt,dpkg,cache,log}

COPY requirements.txt /tmp/
RUN python -m pip install  --no-cache-dir  --upgrade pip pip-tools
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

RUN mkdir -p /code
WORKDIR /code

COPY . /code/

RUN pip install /code

CMD ["python", "--version"]

RUN  deafricacoastlines-raster --help \
  && deafricacoastlines-vector --help