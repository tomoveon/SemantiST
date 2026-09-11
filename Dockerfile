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
# SemantiST metadata patch, and keep the compiler outside the bind-mounted
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
    && test -x "${CARGO_TARGET_DIR}/release/semantist" \
    && test -x "${CARGO_TARGET_DIR}/release/semantist-stg" \
    && test -x "${CARGO_TARGET_DIR}/release/stg-ir-instrument"

CMD ["/bin/bash"]
