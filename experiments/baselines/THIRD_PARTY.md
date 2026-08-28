# Third-party baseline provenance

SemantiST does not copy the baseline source repositories into its Git history.
The build recipes retrieve the exact upstream artifacts recorded in
`baselines.lock.json`.

## AFL++

- Source: <https://github.com/AFLplusplus/AFLplusplus>
- Image: `aflplusplus/aflplusplus:v4.21c`
- License: Apache-2.0; retain the upstream notices when redistributing.

## ICSQuartz and its ICSFuzz reproduction

- Source: <https://github.com/momalab/ICSQuartz>
- Revision: `8021bd44f47147776c6394e008bbea23ac993076`
- LibAFL revision: `7c95afc42fd5e418a6efad3f39122bb915c0a10c`
- License: CC BY-NC-SA 4.0. The upstream license is copied into both images.

The `semantist-icsfuzz-env` recipe uses the ICSFuzz reproduction published as
part of the ICSQuartz artifact, rather than importing the separate hardware-only
ICSFuzz repository.

## CODESYS

The ICSFuzz environment downloads CODESYS Control for Linux SL 3.5.16.10 from
the CODESYS archive and verifies its SHA-256 digest. CODESYS is proprietary and
subject to its own license terms. Do not publish or redistribute an image that
contains CODESYS without confirming that the intended distribution is allowed.
For a paper artifact, it may be necessary to publish the Dockerfile and build
recipe while requiring evaluators to download CODESYS from its official source.

## StructuredFuzzer

- Source: <https://github.com/kandersonko/StructuredFuzzer>
- Revision: `e648a52051fb1d6447a49c70d97cf140ecd861c3`
- LibAFL revision: `ee447468c6070a8548a2ad6366ae6ebb28a3e3d2`
- AFL++ version: `v4.21c` (Apache-2.0)
- MatIEC snapshot: the `matiec/` directory published in StructuredFuzzer
  revision `22695b91b609ba7aecf1774f3c197cc7a629a432` (GPL-3.0)

The StructuredFuzzer README instructs users to build a Docker image, but the
referenced Dockerfile is absent from the repository. Its installer also fetches
MatIEC from an unpinned repository that is no longer available. The SemantiST
recipe reconstructs the environment without changing StructuredFuzzer's Rust
fuzzer or mutation algorithm: it downloads the current pinned source and the
authors' own historical bundled MatIEC snapshot, verifies both archives, and
installs their required runtime components.

StructuredFuzzer does not provide a conventional standalone open-source
license file; its README contains a Battelle/DOE software notice. Treat that
notice as `LicenseRef-StructuredFuzzer-Notice` and review the applicable terms
before redistributing the source or a built image. Publishing only this build
recipe and requiring evaluators to fetch the upstream artifacts is the safer
default.
