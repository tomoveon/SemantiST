ARG RUST_IMAGE=rust:1.76.0@sha256:d36f9d8a9a4c76da74c8d983d0d4cb146dd2d19bb9bd60b704cdcf70ef868d3a
FROM ${RUST_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG RUST_NIGHTLY=nightly-2024-11-30
ARG ICSQUARTZ_REVISION=8021bd44f47147776c6394e008bbea23ac993076
ARG ICSQUARTZ_ARCHIVE_SHA256=3860f1687ba41c646b73ae3725fe7275ff44c998bbbf44ab7bc6cc930097c7ff
ARG LIBAFL_REVISION=7c95afc42fd5e418a6efad3f39122bb915c0a10c
ARG LIBAFL_ARCHIVE_SHA256=be5a8c55334a438333e9fcccbe1603174fb9310d3aa808742921dcd3ec5fc995

LABEL org.opencontainers.image.title="SemantiST ICSQuartz baseline environment" \
      org.opencontainers.image.source="https://github.com/momalab/ICSQuartz" \
      org.opencontainers.image.revision="${ICSQUARTZ_REVISION}" \
      org.opencontainers.image.licenses="CC-BY-NC-SA-4.0"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      cmake \
      curl \
      git \
      gnupg \
      lsb-release \
      ninja-build \
      pkg-config \
      python3-venv \
      unzip \
    && curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/apt.llvm.org.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/apt.llvm.org.gpg] https://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-18 main" \
      > /etc/apt/sources.list.d/llvm-18.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      clang-18 \
      clang-tools-18 \
      clangd-18 \
      libc++-18-dev \
      libc++abi-18-dev \
      libclang-18-dev \
      libclang-common-18-dev \
      libclang-cpp18-dev \
      libclang-rt-18-dev \
      libomp-18-dev \
      libpolly-18-dev \
      libunwind-18-dev \
      lld-18 \
      lldb-18 \
      llvm-18-dev \
      llvm-18-tools \
    && update-alternatives --install /usr/bin/clang clang /usr/bin/clang-18 180 \
    && update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-18 180 \
    && update-alternatives --install /usr/bin/llvm-config llvm-config /usr/bin/llvm-config-18 180 \
    && rm -rf /var/lib/apt/lists/*

RUN rustup toolchain install "${RUST_NIGHTLY}" \
      --profile minimal \
      --component clippy \
      --component rustfmt \
    && rustup default "${RUST_NIGHTLY}"

RUN curl -fsSL \
      "https://github.com/momalab/ICSQuartz/archive/${ICSQUARTZ_REVISION}.tar.gz" \
      -o /tmp/icsquartz.tar.gz \
    && echo "${ICSQUARTZ_ARCHIVE_SHA256}  /tmp/icsquartz.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/icsquartz.tar.gz -C /tmp \
    && mv "/tmp/ICSQuartz-${ICSQUARTZ_REVISION}/fuzzers/icsquartz" /opt/icsquartz \
    && cp "/tmp/ICSQuartz-${ICSQUARTZ_REVISION}/LICENSE" /opt/icsquartz/UPSTREAM-LICENSE \
    && rm -rf "/tmp/ICSQuartz-${ICSQUARTZ_REVISION}" /tmp/icsquartz.tar.gz \
    && curl -fsSL \
      "https://github.com/AFLplusplus/LibAFL/archive/${LIBAFL_REVISION}.tar.gz" \
      -o /tmp/libafl.tar.gz \
    && echo "${LIBAFL_ARCHIVE_SHA256}  /tmp/libafl.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/libafl.tar.gz -C /tmp \
    && mv "/tmp/LibAFL-${LIBAFL_REVISION}" /opt/LibAFL \
    && rm -f /tmp/libafl.tar.gz

ENV ICSQUARTZ_HOME=/opt/icsquartz \
    LIBAFL_HOME=/opt/LibAFL \
    LLVM_CONFIG=/usr/bin/llvm-config-18 \
    LLVM_CONFIG_PATH=/usr/bin/llvm-config-18 \
    LIBCLANG_PATH=/usr/lib/llvm-18/lib \
    IS_DOCKER=1

WORKDIR /opt/icsquartz

RUN cargo fetch --locked \
    && RUSTFLAGS="--cap-lints=warn" cargo build --release --locked \
    && test -x target/release/libafl_cc \
    && test -x target/release/libafl_cxx \
    && test -r target/release/liblibfuzzer_icsquartz.a

ENV CC=/opt/icsquartz/target/release/libafl_cc \
    CXX=/opt/icsquartz/target/release/libafl_cxx

CMD ["/bin/bash"]
