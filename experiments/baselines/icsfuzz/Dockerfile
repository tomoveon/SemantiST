ARG DEBIAN_IMAGE=debian:buster@sha256:58ce6f1271ae1c8a2006ff7d3e54e9874d839f573d8009c20154ad0f2fb0a225

FROM ${DEBIAN_IMAGE} AS upstream

ARG DEBIAN_FRONTEND=noninteractive
ARG ICSQUARTZ_REVISION=8021bd44f47147776c6394e008bbea23ac993076
ARG ICSQUARTZ_ARCHIVE_SHA256=3860f1687ba41c646b73ae3725fe7275ff44c998bbbf44ab7bc6cc930097c7ff

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; /buster-updates/d' /etc/apt/sources.list \
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive \
    && echo 'Acquire::Retries "5";' >> /etc/apt/apt.conf.d/99archive \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL \
      "https://github.com/momalab/ICSQuartz/archive/${ICSQUARTZ_REVISION}.tar.gz" \
      -o /tmp/icsquartz.tar.gz \
    && echo "${ICSQUARTZ_ARCHIVE_SHA256}  /tmp/icsquartz.tar.gz" | sha256sum -c - \
    && mkdir -p /upstream \
    && tar -xzf /tmp/icsquartz.tar.gz \
      --strip-components=1 \
      -C /upstream \
    && rm -f /tmp/icsquartz.tar.gz

FROM ${DEBIAN_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG ICSQUARTZ_REVISION=8021bd44f47147776c6394e008bbea23ac993076
ARG CODESYS_PACKAGE_URL=https://store-archive.codesys.com/ftp_download/3S/LinuxSL/2302000005/3.5.16.10/CODESYS%20Control%20for%20Linux%20SL%203.5.16.10.package
ARG CODESYS_PACKAGE_SHA256=97921a5b510ae370d6f46a2377529545e2cd974b6d137b1fa0964481e1f3b0a5
ARG SCAN_CYCLE_MS=35

LABEL org.opencontainers.image.title="SemantiST ICSFuzz baseline environment" \
      org.opencontainers.image.source="https://github.com/momalab/ICSQuartz" \
      org.opencontainers.image.revision="${ICSQUARTZ_REVISION}" \
      org.opencontainers.image.licenses="CC-BY-NC-SA-4.0"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; /buster-updates/d' /etc/apt/sources.list \
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive \
    && echo 'Acquire::Retries "5";' >> /etc/apt/apt.conf.d/99archive \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      inotify-tools \
      libpthread-stubs0-dev \
      procps \
      psmisc \
      unzip \
    && curl -fsSL "${CODESYS_PACKAGE_URL}" -o /tmp/codesys.package \
    && echo "${CODESYS_PACKAGE_SHA256}  /tmp/codesys.package" | sha256sum -c - \
    && unzip -p /tmp/codesys.package \
      'Delivery/codesyscontrol_x64_linux_V3.5.16.10.deb' \
      > /tmp/codesys.deb \
    && unzip -p /tmp/codesys.package \
      'Dependency/codemeter-lite_7.0.3918.500_amd64.deb' \
      > /tmp/codemeter.deb \
    && apt-get install -y /tmp/codemeter.deb \
    && dpkg --force-confdef --force-confold -i /tmp/codesys.deb \
    && rm -f /tmp/codesys.package /tmp/codesys.deb /tmp/codemeter.deb \
    && rm -rf /var/lib/apt/lists/*

ENV CODESYS_HOME=/var/opt/codesys \
    CODESYS_LOG=/wrapper.log \
    PLCTASK=MainTask \
    CODESYS_AREA_ZERO=0x7ffff53da000 \
    ICSFUZZ_HOME=/opt/icsfuzz

COPY --from=upstream /upstream/compiler/codesys/configs/CODESYSControl.cfg /etc/CODESYSControl.cfg
COPY --from=upstream /upstream/compiler/codesys/configs/CODESYSControl_User.cfg /etc/CODESYSControl_User.cfg
COPY --from=upstream /upstream/compiler/codesys/start.sh /usr/local/bin/start-codesys.sh
COPY --from=upstream /upstream/compiler/codesys/locate-area-zero.sh /usr/local/bin/locate-codesys-area-zero.sh
COPY --from=upstream /upstream/fuzzers/icsfuzz/icsfuzz.c /opt/icsfuzz/icsfuzz.c
COPY --from=upstream /upstream/fuzzers/icsfuzz/mutator.c /opt/icsfuzz/mutator.c
COPY --from=upstream /upstream/fuzzers/icsfuzz/mutator.h /opt/icsfuzz/mutator.h
COPY --from=upstream /upstream/fuzzers/icsfuzz/start-fuzz.sh /usr/local/bin/start-icsfuzz.sh
COPY --from=upstream /upstream/LICENSE /opt/icsfuzz/UPSTREAM-LICENSE

WORKDIR /opt/icsfuzz

RUN gcc icsfuzz.c mutator.c -o fuzzer -DSCAN_CYCLE_MS="${SCAN_CYCLE_MS}" \
    && chmod +x \
      fuzzer \
      /usr/local/bin/start-codesys.sh \
      /usr/local/bin/locate-codesys-area-zero.sh \
      /usr/local/bin/start-icsfuzz.sh \
    && test -x /opt/codesys/bin/codesyscontrol.bin \
    && test -x /usr/sbin/CodeMeterLin

CMD ["/bin/bash"]
