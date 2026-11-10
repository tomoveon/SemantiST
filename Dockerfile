FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG RUST_TOOLCHAIN=1.90.0

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    lsb-release \
    software-properties-common \
    make \
    build-essential \
    pkg-config \
    libzstd-dev \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    afl++ \
    clang \
    llvm \
    perl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://apt.llvm.org/llvm.sh -o /tmp/llvm.sh \
    && chmod +x /tmp/llvm.sh \
    && /tmp/llvm.sh 21 \
    && apt-get update \
    && apt-get install -y --no-install-recommends libpolly-21-dev \
    && rm -f /tmp/llvm.sh \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --default-toolchain "${RUST_TOOLCHAIN}"

ENV VIRTUAL_ENV="/opt/semantist-venv" \
    PATH="/usr/lib/llvm-21/bin:/opt/semantist-venv/bin:/root/.cargo/bin:${PATH}" \
    LLVM_CONFIG="/usr/lib/llvm-21/bin/llvm-config" \
    LLVM_SYS_211_PREFIX="/usr/lib/llvm-21" \
    SEMANTIST_LLVM_BIN="/usr/lib/llvm-21/bin" \
    SEMANTIST_SEMANTIC_LLVM_BIN="/usr/lib/llvm-21/bin" \
    SEMANTIST_AFL_RUNTIME="/usr/local/lib/afl/afl-compiler-rt.o" \
    SEMANTIST_RUSTY_SOURCE="https://github.com/PLC-lang/rusty.git" \
    SEMANTIST_RUSTY_SEMANTIC_DIR="/opt/rusty-semantic" \
    SEMANTIST_RUSTY_CARGO_TARGET_DIR="/opt/rusty-semantic/target" \
    RUSTY_COMPILER="/opt/rusty-semantic/target/release/plc" \
    SEMANTIST_RUSTY_STDLIB_MANIFEST="/opt/rusty-semantic/libs/stdlib/Cargo.toml" \
    SEMANTIST_RUSTY_STDLIB_GLOB="/opt/rusty-semantic/libs/stdlib/iec61131-st/*.st" \
    SEMANTIST_RUSTY_STDLIB_LIB="/opt/rusty-semantic/target/release/libiec61131std.a" \
    CARGO_NET_GIT_FETCH_WITH_CLI="true" \
    CARGO_TARGET_DIR="/opt/semantist/cargo-target"

RUN mkdir -p /usr/local/lib/afl \
    && if [ -e /usr/lib/afl/afl-compiler-rt.o ] && [ ! -e /usr/local/lib/afl/afl-compiler-rt.o ]; then \
      ln -s /usr/lib/afl/afl-compiler-rt.o /usr/local/lib/afl/afl-compiler-rt.o; \
    fi \
    && test -r "${SEMANTIST_AFL_RUNTIME}"

WORKDIR /work/SemantiST
COPY . .

RUN python3 -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e '.[dev]' \
    && cargo fetch --locked

# Build the exact RuSTy revision pinned by build_rusty_semantic.sh, apply the
# patch series in compiler/toolchain/rusty-patches, and keep the compiler outside the bind-mounted
# repository. The standard library is also prebuilt at the path consumed by
# build_target.sh.
RUN ./compiler/scripts/build_rusty_semantic.sh \
    && CARGO_TARGET_DIR="${SEMANTIST_RUSTY_CARGO_TARGET_DIR}" \
       cargo build \
         --manifest-path "${SEMANTIST_RUSTY_STDLIB_MANIFEST}" \
         --release \
         --locked \
    && test -x "${RUSTY_COMPILER}" \
    && test -r "${SEMANTIST_RUSTY_STDLIB_LIB}"

# Prebuild all release binaries. /opt is not hidden when users mount the
# repository's artifacts directory for persistent experiment output.
RUN cargo build --workspace --release --locked \
    && test -r "${CARGO_TARGET_DIR}/release/libsemantist_inprocess.a" \
    && test -x "${CARGO_TARGET_DIR}/release/semantist-stg" \
    && test -x "${CARGO_TARGET_DIR}/release/stg-ir-instrument"


# --- KLEE backend and virtual-clock standard library -------------------
# Build KLEE and the virtual-clock library in the main image so a fresh build
# has the same toolchain used by the current pipeline.
ARG DEBIAN_FRONTEND=noninteractive
ARG KLEE_VERSION=v3.3
ARG Z3_VERSION=z3-4.13.4

RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake ninja-build llvm-18 llvm-18-dev llvm-18-tools clang-18 \
    libpolly-18-dev lld-18 libgoogle-perftools-dev libsqlite3-dev \
    libboost-all-dev libcap-dev python3-tabulate file gdb \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${Z3_VERSION}" https://github.com/Z3Prover/z3.git /opt/z3-src \
    && cmake -S /opt/z3-src -B /opt/z3-src/build -G Ninja \
         -DCMAKE_BUILD_TYPE=Release -DZ3_BUILD_LIBZ3_SHARED=ON \
         -DZ3_BUILD_EXECUTABLE=ON -DZ3_ENABLE_EXAMPLE_TARGETS=OFF \
         -DCMAKE_INSTALL_PREFIX=/usr/local \
    && ninja -C /opt/z3-src/build install && ldconfig

RUN git clone --depth 1 --branch "${KLEE_VERSION}" https://github.com/klee/klee.git /opt/klee-src \
    && cmake -S /opt/klee-src -B /opt/klee-src/build -G Ninja \
         -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/klee \
         -DLLVM_DIR=/usr/lib/llvm-18/lib/cmake/llvm \
         -DENABLE_SOLVER_Z3=ON -DENABLE_SOLVER_STP=OFF -DENABLE_SOLVER_METASMT=OFF \
         -DENABLE_POSIX_RUNTIME=OFF -DENABLE_KLEE_UCLIBC=OFF \
         -DENABLE_UNIT_TESTS=OFF -DENABLE_SYSTEM_TESTS=OFF \
         -DKLEE_RUNTIME_BUILD_TYPE=Release \
         -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
    && ninja -C /opt/klee-src/build install && test -x /opt/klee/bin/klee

ENV SEMANTIST_KLEE=/opt/klee/bin/klee \
    SEMANTIST_KLEE_INCLUDE=/opt/klee/include \
    SEMANTIST_KLEE_LIB=/opt/klee/lib \
    SEMANTIST_CLANG18=/usr/bin/clang-18 \
    SEMANTIST_LLVM18_BIN=/usr/lib/llvm-18/bin \
    SEMANTIST_LLVM16_BIN=/usr/lib/llvm-18/bin \
    SEMANTIST_LLVM_AS16=/usr/lib/llvm-18/bin/llvm-as \
    SEMANTIST_LLVM_LINK16=/usr/lib/llvm-18/bin/llvm-link \
    SEMANTIST_LLVM_NM16=/usr/lib/llvm-18/bin/llvm-nm \
    SEMANTIST_RUSTY_STDLIB_VCLOCK_DIR=/opt/rusty-stdlib-vclock \
    SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB=/opt/rusty-stdlib-vclock/target/release/libiec61131std.a \
    PATH="/opt/klee/bin:/usr/lib/llvm-18/bin:${PATH}"

COPY compiler/toolchain/stdlib-patches/ /opt/stdlib-patches/
RUN rm -rf "${SEMANTIST_RUSTY_STDLIB_VCLOCK_DIR}" \
    && cp -a /opt/rusty-semantic "${SEMANTIST_RUSTY_STDLIB_VCLOCK_DIR}" \
    && cd "${SEMANTIST_RUSTY_STDLIB_VCLOCK_DIR}" \
    && git apply --verbose /opt/stdlib-patches/0007-semantist-virtual-clock.patch \
    && CARGO_TARGET_DIR="${SEMANTIST_RUSTY_STDLIB_VCLOCK_DIR}/target" \
       cargo build --manifest-path "${SEMANTIST_RUSTY_STDLIB_VCLOCK_DIR}/libs/stdlib/Cargo.toml" \
         --release --locked --features semantist_vclock \
    && test -r "${SEMANTIST_RUSTY_STDLIB_VCLOCK_LIB}" \
    && test -r "${SEMANTIST_RUSTY_STDLIB_LIB}"

# Keep the source revision label after expensive build layers so changing only
# the revision metadata does not invalidate toolchain caches.
ARG SEMANTIST_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="SemantiST" \
      org.opencontainers.image.revision="${SEMANTIST_SOURCE_REVISION}"

CMD ["/bin/bash"]
