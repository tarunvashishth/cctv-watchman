# baresip (internet phone calls) built from source: Debian's 2021 version can't
# log in to sip.linphone.org (no SHA-256 digest auth).
FROM python:3.12-slim AS baresip
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates libssl-dev libopus-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 -b v4.12.0 https://github.com/baresip/re.git /src/re \
    && git clone --depth 1 -b v4.12.0 https://github.com/baresip/baresip.git /src/baresip \
    && cmake -S /src/re -B /src/re/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /src/re/build -j4 && cmake --install /src/re/build --prefix /opt/baresip \
    && cmake -S /src/baresip -B /src/baresip/build -DCMAKE_BUILD_TYPE=Release \
         -DCMAKE_PREFIX_PATH=/opt/baresip -DMODULES="account;menu;g711;opus;aufile;srtp" \
    && cmake --build /src/baresip/build -j4 && cmake --install /src/baresip/build --prefix /usr/local

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopus0 espeak-ng ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=baresip /opt/baresip/lib/ /usr/local/lib/
COPY --from=baresip /usr/local/bin/baresip /usr/local/bin/baresip
COPY --from=baresip /usr/local/lib/baresip/ /usr/local/lib/baresip/
COPY --from=baresip /usr/local/lib/libbaresip.so* /usr/local/lib/
RUN ldconfig && baresip -h 2>&1 | grep -q "Usage: baresip"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
CMD ["python", "-u", "app.py"]
