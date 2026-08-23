#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REVISION="be1de6f175ec1bed7928904dfb694208a24043fb"
SOURCE="${SEMANTIST_RUSTY_SOURCE:-/opt/rusty}"
DESTINATION="${SEMANTIST_RUSTY_SEMANTIC_DIR:-${ROOT}/artifacts/rusty-semantic}"
PATCH="${ROOT}/compiler/toolchain/rusty-patches/0001-semantist-semantic-metadata.patch"
PROFILE="${SEMANTIST_RUSTY_PROFILE:-release}"
CARGO_OUTPUT="${SEMANTIST_RUSTY_CARGO_TARGET_DIR:-${DESTINATION}/target}"
PATCH_SHA="$(sha256sum "${PATCH}" | awk '{print $1}')"
STAMP="${DESTINATION}/.semantist-semantic-patch.sha256"

clone_destination() {
  mkdir -p "$(dirname "${DESTINATION}")"
  git clone --no-hardlinks "${SOURCE}" "${DESTINATION}"
  git -C "${DESTINATION}" checkout --detach "${REVISION}"
}

if [[ -d "${DESTINATION}/.git" ]]; then
  if [[ ! -f "${STAMP}" ]] \
    && ! git -C "${DESTINATION}" apply --check "${PATCH}" >/dev/null 2>&1 \
    && ! git -C "${DESTINATION}" apply --reverse --check "${PATCH}" >/dev/null 2>&1; then
    backup="${DESTINATION}.pre-semantist.$(date +%s)"
    echo "[build_rusty_semantic] preserving pre-SemantiST managed clone at ${backup}"
    mv "${DESTINATION}" "${backup}"
  elif [[ -f "${STAMP}" ]]; then
    installed_patch_sha="$(tr -d '[:space:]' < "${STAMP}")"
    if [[ "${installed_patch_sha}" != "${PATCH_SHA}" ]]; then
      backup="${DESTINATION}.stale.$(date +%s)"
      echo "[build_rusty_semantic] preserving stale managed clone at ${backup}"
      mv "${DESTINATION}" "${backup}"
    fi
  fi
fi

if [[ ! -d "${DESTINATION}/.git" ]]; then
  clone_destination
fi

actual_revision="$(git -C "${DESTINATION}" rev-parse HEAD)"
if [[ "${actual_revision}" != "${REVISION}" ]]; then
  echo "[build_rusty_semantic] expected ${REVISION}, found ${actual_revision}" >&2
  exit 1
fi

if git -C "${DESTINATION}" apply --reverse --check "${PATCH}" >/dev/null 2>&1; then
    echo "[build_rusty_semantic] semantic metadata patch already applied"
elif git -C "${DESTINATION}" apply --check "${PATCH}"; then
    git -C "${DESTINATION}" apply "${PATCH}"
else
  echo "[build_rusty_semantic] worktree is not compatible with the pinned patch" >&2
    exit 1
fi
printf '%s\n' "${PATCH_SHA}" > "${STAMP}"

export LLVM_SYS_211_PREFIX="${LLVM_SYS_211_PREFIX:-/usr/lib/llvm-21}"
export LLVM_CONFIG="${LLVM_CONFIG:-${LLVM_SYS_211_PREFIX}/bin/llvm-config}"
export PATH="${LLVM_SYS_211_PREFIX}/bin:${PATH}"
export CARGO_TARGET_DIR="${CARGO_OUTPUT}"

if [[ ! -x "${LLVM_CONFIG}" ]]; then
  echo "[build_rusty_semantic] missing LLVM_CONFIG: ${LLVM_CONFIG}" >&2
  exit 1
fi

LLVM_MAJOR="$("${LLVM_CONFIG}" --version | cut -d. -f1)"
if [[ "${LLVM_MAJOR}" != "21" ]]; then
  echo "[build_rusty_semantic] LLVM 21 required, found $("${LLVM_CONFIG}" --version)" >&2
  exit 1
fi


if [[ "${PROFILE}" == "release" ]]; then
  cargo build --manifest-path "${DESTINATION}/Cargo.toml" --release --locked --bin plc
else
  cargo build --manifest-path "${DESTINATION}/Cargo.toml" --locked --bin plc
fi

echo "${CARGO_OUTPUT}/${PROFILE}/plc"
