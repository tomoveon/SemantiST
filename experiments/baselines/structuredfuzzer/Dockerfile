ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
ARG RUST_IMAGE=docker.m.daocloud.io/library/rust:1.75.0-bookworm@sha256:87f3b2f93b82995443a1a558c234212dafe79cfdc3af956539610560369ddcd0

FROM ${PYTHON_IMAGE} AS upstream

ARG DEBIAN_FRONTEND=noninteractive
ARG STRUCTUREDFUZZER_REVISION=e648a52051fb1d6447a49c70d97cf140ecd861c3
ARG STRUCTUREDFUZZER_ARCHIVE_SHA256=1a3063c9ec7942ff83ce22fea98eef82e9828cb6ca198b9939698282ef47a270
ARG MATIEC_SNAPSHOT_REVISION=22695b91b609ba7aecf1774f3c197cc7a629a432
ARG MATIEC_SNAPSHOT_ARCHIVE_SHA256=3d3fde81ef5080b511868961a57d60e943f8b2e00264f305369b3b9ede7a5315

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL \
      "https://github.com/kandersonko/StructuredFuzzer/archive/${STRUCTUREDFUZZER_REVISION}.tar.gz" \
      -o /tmp/structuredfuzzer.tar.gz \
    && echo "${STRUCTUREDFUZZER_ARCHIVE_SHA256}  /tmp/structuredfuzzer.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/structuredfuzzer \
    && tar -xzf /tmp/structuredfuzzer.tar.gz \
      --strip-components=1 \
      -C /opt/structuredfuzzer \
    && curl -fsSL \
      "https://github.com/kandersonko/StructuredFuzzer/archive/${MATIEC_SNAPSHOT_REVISION}.tar.gz" \
      -o /tmp/matiec-snapshot.tar.gz \
    && echo "${MATIEC_SNAPSHOT_ARCHIVE_SHA256}  /tmp/matiec-snapshot.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/matiec \
    && tar -xzf /tmp/matiec-snapshot.tar.gz \
      --strip-components=2 \
      -C /opt/matiec \
      "StructuredFuzzer-${MATIEC_SNAPSHOT_REVISION}/matiec" \
    && rm -f /tmp/structuredfuzzer.tar.gz /tmp/matiec-snapshot.tar.gz

FROM ${RUST_IMAGE} AS fuzzer-builder

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      clang \
      libclang-dev \
      pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY --from=upstream /opt/structuredfuzzer/fuzzer /src/fuzzer

WORKDIR /src/fuzzer

RUN cargo build --release --locked --bin fuzzer \
    && test -x target/release/fuzzer

FROM ${PYTHON_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG STRUCTUREDFUZZER_REVISION=e648a52051fb1d6447a49c70d97cf140ecd861c3
ARG AFLPLUSPLUS_VERSION=v4.21c
ARG AFLPLUSPLUS_ARCHIVE_SHA256=11f7c77d37cff6e7f65ac7cc55bab7901e0c6208e845a38764394d04ed567b30
ARG BUILD_JOBS=2

LABEL org.opencontainers.image.title="SemantiST StructuredFuzzer baseline environment" \
      org.opencontainers.image.source="https://github.com/kandersonko/StructuredFuzzer" \
      org.opencontainers.image.revision="${STRUCTUREDFUZZER_REVISION}" \
      org.opencontainers.image.licenses="LicenseRef-StructuredFuzzer-Notice"

ENV NO_CORESIGHT=1 \
    NO_NYX=1 \
    NO_ARCH_OPT=1 \
    IS_DOCKER=1 \
    AFL_SKIP_CPUFREQ=1 \
    AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
    LLVM_CONFIG=/usr/bin/llvm-config \
    MATIEC_INCLUDE_PATH=/usr/local/bin/stfuzz/matiec/lib \
    MATIEC_C_INCLUDE_PATH=/usr/local/bin/stfuzz/matiec/lib/C \
    STRUCTUREDFUZZER_HOME=/opt/structuredfuzzer \
    PATH=/usr/local/bin/stfuzz/matiec:/usr/local/bin/stfuzz:${PATH}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      autoconf \
      automake \
      bison \
      build-essential \
      ca-certificates \
      clang \
      curl \
      flex \
      gcc-12-plugin-dev \
      libclang-dev \
      libglib2.0-dev \
      libpixman-1-dev \
      libtool \
      lld \
      llvm \
      llvm-dev \
      make \
      ninja-build \
      pkg-config \
      xz-utils \
      zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=upstream /opt/structuredfuzzer /opt/structuredfuzzer
COPY --from=upstream /opt/matiec /usr/local/bin/stfuzz/matiec
COPY --from=fuzzer-builder /src/fuzzer/target/release/fuzzer /usr/local/bin/stfuzz/stfuzz

WORKDIR /usr/local/bin/stfuzz/matiec

RUN autoreconf -fi \
    && ./configure \
    && make clean \
    && make -j "${BUILD_JOBS}" \
    && test -x iec2c

WORKDIR /tmp

RUN curl -fsSL \
      "https://github.com/AFLplusplus/AFLplusplus/archive/refs/tags/${AFLPLUSPLUS_VERSION}.tar.gz" \
      -o /tmp/aflplusplus.tar.gz \
    && echo "${AFLPLUSPLUS_ARCHIVE_SHA256}  /tmp/aflplusplus.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/aflplusplus \
    && tar -xzf /tmp/aflplusplus.tar.gz \
      --strip-components=1 \
      -C /tmp/aflplusplus \
    && make -C /tmp/aflplusplus -j "${BUILD_JOBS}" source-only \
    && make -C /tmp/aflplusplus install \
    && rm -rf /tmp/aflplusplus /tmp/aflplusplus.tar.gz \
    && command -v afl-fuzz \
    && command -v afl-clang-fast

RUN cp -a /opt/structuredfuzzer/runtime /usr/local/bin/stfuzz/runtime \
    && ln -s runtime/build.sh /usr/local/bin/stfuzz/build.sh \
    && ln -s runtime/stcompile.sh /usr/local/bin/stfuzz/stcompile \
    && chmod +x \
      /usr/local/bin/stfuzz/stfuzz \
      /usr/local/bin/stfuzz/runtime/build.sh \
      /usr/local/bin/stfuzz/runtime/compile.sh \
      /usr/local/bin/stfuzz/runtime/stcompile.sh \
    && python3 -m pip install --no-cache-dir \
      tree-sitter==0.21.3 \
      z3-solver==4.15.1.0 \
    && python3 -m pip install --no-cache-dir --no-deps \
      /opt/structuredfuzzer/code_analysis \
    && analyzer_root="$(python3 -c 'from pathlib import Path; import analyzer; print(Path(analyzer.__file__).resolve().parent.parent)')" \
    && cp -a \
      /opt/structuredfuzzer/code_analysis/tree-sitter-structured-text \
      "${analyzer_root}/tree-sitter-structured-text" \
    && gcc -shared -fPIC \
      -I "${analyzer_root}/tree-sitter-structured-text/src" \
      "${analyzer_root}/tree-sitter-structured-text/src/parser.c" \
      -o "${analyzer_root}/tree-sitter-structured-text/parser.so" \
    && ln -s /usr/local/bin/analyzer /usr/local/bin/stcode

RUN printf '%s\n' \
      'export PATH="/usr/local/bin/stfuzz/matiec:/usr/local/bin/stfuzz:${PATH}"' \
      > /etc/profile.d/structuredfuzzer-path.sh

WORKDIR /work

RUN command -v stfuzz \
    && command -v stcode \
    && command -v afl-fuzz \
    && command -v afl-clang-fast \
    && command -v iec2c \
    && test -r /opt/structuredfuzzer/README.md \
    && test -r /usr/local/bin/stfuzz/matiec/COPYING \
    && python3 -c "import analyzer, tree_sitter, z3" \
    && stfuzz --help >/dev/null \
    && stcode --help >/dev/null

CMD ["/bin/bash"]
