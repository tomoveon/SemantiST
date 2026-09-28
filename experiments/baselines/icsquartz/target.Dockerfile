ARG ICSQUARTZ_BASE_IMAGE=semantist-icsquartz-env:8021bd4
FROM ${ICSQUARTZ_BASE_IMAGE}

ARG SCAN_CYCLE=0
ARG ASAN_ALT=0
ARG CXXFLAGS="-DSEMANTIST_HARNESS_NO_MAIN"
ARG LINKLIBS=""

COPY --from=icsbuild /build /build
COPY --from=fuzztarget icsquartz/harness.c /build

WORKDIR /opt/icsquartz
RUN if [ "${SCAN_CYCLE}" = "1" ]; then \
      RUSTFLAGS="--cap-lints=warn" cargo build --release --locked --features "scan_cycle scan_cycle_mutations"; \
    elif [ "${ASAN_ALT}" = "1" ]; then \
      RUSTFLAGS="--cap-lints=warn" cargo build --release --locked --features "asan_crash_feedback"; \
    else \
      RUSTFLAGS="--cap-lints=warn" cargo build --release --locked; \
    fi

ENV CC=/opt/icsquartz/target/release/libafl_cc \
    CXX=/opt/icsquartz/target/release/libafl_cxx \
    SRC=/build \
    OUT=/out \
    SEED=1 \
    CORES=2 \
    FUZZ_TARGET=icsfuzz-demo

WORKDIR ${OUT}

RUN "${CXX}" ${CXXFLAGS} \
      -fsanitize=address \
      -fsanitize-coverage=trace-pc-guard \
      -L "${SRC}/libs/" \
      -no-pie \
      "${SRC}"/harness.* \
      -Wl,--allow-multiple-definition \
      -Wl,-Bstatic \
      ${LINKLIBS} \
      -lmain \
      -liec61131std \
      -lglibcWrappers \
      -Wl,-Bdynamic \
      -o "${OUT}/${FUZZ_TARGET}" \
    && chmod +x "${OUT}/${FUZZ_TARGET}"

CMD ["/bin/bash"]
