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
