ARG MUSETALK_BASE_IMAGE
ARG MUSETALK_BASE_DIGEST
FROM ${MUSETALK_BASE_IMAGE}@${MUSETALK_BASE_DIGEST}

ARG MUSETALK_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/MuseTalk \
    PATH=/usr/bin:/bin:/opt/conda/bin

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg git gcc g++ libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone https://github.com/TMElyralab/MuseTalk.git \
 && git -C MuseTalk checkout --detach "${MUSETALK_COMMIT}"

WORKDIR /opt/MuseTalk
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.txt \
 && python -m pip install --no-cache-dir -U openmim \
 && mim install mmengine \
 && mim install "mmcv==2.0.1" \
 && mim install "mmdet==3.1.0" \
 && python -m pip install --no-build-isolation "chumpy==0.70" \
 && mim install "mmpose==1.1.0"

COPY docker/musetalk-run.sh /usr/local/bin/nvg-musetalk
RUN chmod 0755 /usr/local/bin/nvg-musetalk

ARG MUSETALK_BUILD_SHA
LABEL io.narration-video-gen.musetalk-build-sha="${MUSETALK_BUILD_SHA}"

ENTRYPOINT ["/usr/local/bin/nvg-musetalk"]
