ARG ICSQUARTZ_BASE_IMAGE=semantist-icsquartz-env:8021bd4
FROM ${ICSQUARTZ_BASE_IMAGE}

ARG SCAN_CYCLE=0
ARG ASAN_ALT=0
ARG CXXFLAGS="-DSEMANTIST_HARNESS_NO_MAIN"
ARG LINKLIBS=""

COPY --from=fuzztarget target.prepared.ll /build/target.prepared.ll
COPY --from=fuzztarget libiec61131std.a /build/libs/libiec61131std.a
COPY --from=fuzztarget icsquartz/harness.c /build/harness.c
COPY --from=fuzztarget compatibility/ /build/compatibility/

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

RUN mkdir -p "${SRC}/libs" \
    && "${CXX}" -g -O1 \
      -fsanitize=address \
      -fno-omit-frame-pointer \
      -fsanitize-coverage=trace-pc-guard \
      -c "${SRC}/target.prepared.ll" \
      -o "${SRC}/libs/main.o" \
    && ar rcs "${SRC}/libs/libmain.a" "${SRC}/libs/main.o" \
    && ar rcs "${SRC}/libs/libcompatibility.a" \
    && for source in "${SRC}"/compatibility/*.c; do \
         [ -f "${source}" ] || continue; \
         object="${SRC}/libs/$(basename "${source}" .c).o"; \
         "${CXX}" -x c -g -O1 -fsanitize=address -fno-omit-frame-pointer \
           -c "${source}" -o "${object}"; \
         ar r "${SRC}/libs/libcompatibility.a" "${object}"; \
       done \
    && "${CXX}" ${CXXFLAGS} \
      -O1 \
      -fsanitize=address \
      -fno-omit-frame-pointer \
      -fsanitize-coverage=trace-pc-guard \
      -L "${SRC}/libs/" \
      -no-pie \
      "${SRC}"/harness.* \
      -Wl,--allow-multiple-definition \
      -Wl,-Bstatic \
      ${LINKLIBS} \
      -lmain \
      -lcompatibility \
      -liec61131std \
      -Wl,-Bdynamic \
      -ldl -lpthread -lm \
      -o "${OUT}/${FUZZ_TARGET}" \
    && chmod +x "${OUT}/${FUZZ_TARGET}"

CMD ["/bin/bash"]
