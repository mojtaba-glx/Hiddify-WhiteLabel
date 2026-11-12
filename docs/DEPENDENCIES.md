# Dependency and Third-Party License Review

**Repository version reviewed:** `1.3.18`  
**Review basis:** `requirements.txt`, the current tracked source tree and the documented installation workflow.  
**Purpose:** preliminary engineering inventory to inform the maintainer's license choice. This is not legal advice or a complete Software Bill of Materials (SBOM).

## 1. Important scope limitation

The repository pins six top-level requirements, but it does **not** contain a lockfile or a fully resolved dependency graph. Several transitive packages are therefore selected by pip at installation time and can change without a repository commit. The licenses below identify the declared direct requirements and their known dependency families; they are not a version-exact inventory of every package installed on every host.

The installer runs `pip install -r requirements.txt`. It does not vendor third-party package source into this repository. A future wheel, container image, zipapp or other bundled distribution would need a fresh inventory of the exact artifacts being shipped.

## 2. Declared direct requirements

| Requirement in `requirements.txt` | Role indicated by source/configuration | Published license | Preliminary notes |
|---|---|---|---|
| `python-telegram-bot[job-queue]==22.8` | Core Telegram bot framework; JobQueue extra | LGPL-3.0-only | The PTB maintainers state that applications using the library do not have to be LGPL. Preserve its license/notices if redistributing the library itself; modifications to PTB itself remain subject to LGPL terms. |
| `python-dotenv==1.0.1` | Declared configuration helper | BSD-3-Clause | Its direct use was not established in the source files sampled for this review; verify whether it is still needed. |
| `cryptography==43.0.3` | Fernet token encryption | Apache-2.0 OR BSD-3-Clause | The upstream release offers a choice between these licenses. Retain the upstream notices when redistributing the package. |
| `pytest==8.3.4` | Automated tests | MIT | Development/test dependency, not needed by the production service at runtime. It is currently installed by the production installer because it shares the same requirements file. |
| `httpx==0.28.1` | HTTP client for panel adapters | BSD-3-Clause | Also a dependency of python-telegram-bot; listed separately in this repository. |
| `qrcode[pil]==8.2` | QR-code generation dependency | BSD (upstream package metadata) | The `pil` extra brings in Pillow. The current tracked tree references `Shared.qr_utils.make_qr_image`, but the tree reviewed did not contain `Shared/qr_utils.py`; confirm whether the QR helper is supplied elsewhere or this requirement is stale. |

Sources for the package license declarations: python-telegram-bot 22.8, python-dotenv, cryptography 43.0.3, httpx 0.28.1, qrcode 8.2 and pytest's license documentation (links are in the review notes below).

## 3. Important transitive dependency families

These packages are not all pinned directly in this repository. Exact versions depend on the Python version, platform, package metadata and pip's resolver.

| Parent / extra | Known transitive packages | License identifiers commonly declared upstream | Follow-up |
|---|---|---|---|
| `httpx` | `httpcore`, `anyio`, `certifi`, `idna`; `httpcore` also brings `h11` and `certifi` | BSD-3-Clause, MIT, MPL-2.0, BSD-3-Clause, MIT | Include `certifi` (MPL-2.0) in the eventual resolved SBOM/notices. |
| PTB `[job-queue]` | `APScheduler` (3.10.x/3.11.x range), `tzlocal`, and their dependencies | MIT for APScheduler; verify the resolved versions of all transitive packages | The extra is declared in the production requirements. |
| `cryptography` | On CPython, commonly `cffi` and `pycparser` in relevant environments | MIT / BSD-family upstream metadata | Record exact platform-resolved versions before producing a binary bundle. |
| `qrcode[pil]` | `Pillow` | MIT-CMU in current Pillow releases; older Pillow releases have used different identifiers | The Pillow version is not pinned. Record the resolved version/license rather than hard-coding a version in this document. |
| `pytest` | `iniconfig`, `packaging`, `pluggy`, `Pygments` (plus platform/Python-conditional packages) | MIT, Apache-2.0 OR BSD-2-Clause, MIT, BSD-2-Clause | Test-only family; separate from runtime dependencies in a future packaging cleanup. |

**MPL-2.0 note:** `certifi` is part of the HTTP dependency chain. MPL-2.0 is file-level copyleft, not a rule that automatically relicenses every independent file in a larger application. If MPL-covered source is modified and distributed, the relevant MPL source/modifications and notices must be handled under the MPL. The current installer installs the dependency separately through pip; this repository does not currently vendor or modify certifi. See Mozilla's official [MPL 2.0 FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

## 4. Effect on choosing a license for Hiddify-WhiteLabel

Based on the declared dependency set reviewed:

1. **No immediate dependency-based blocker to choosing a permissive project license was identified.** The direct dependencies are used as separately installed Python libraries; their licenses do not, by themselves, automatically impose their license on all of Hiddify-WhiteLabel's own source files.
2. **python-telegram-bot is LGPL-3.0-only.** Its own PyPI license notice expressly distinguishes an application using the library from a derivative work of the library. Do not copy PTB source into this repository or modify/vendor PTB without separately satisfying the LGPL requirements.
3. **certifi is MPL-2.0 in the httpx dependency chain.** Keep the package's own notices and source availability obligations in view if a future distribution bundles it or if its covered files are modified.
4. **cryptography is dual-licensed (Apache-2.0 OR BSD-3-Clause).** That is a choice available for the dependency, not a requirement to dual-license this repository.
5. **The project license must cover only material the maintainer has rights to license.** The maintainer has confirmed ownership of the Hiddify-SellBot-derived module. That confirmation resolves the specific provenance concern raised in the previous review, subject to retaining any third-party notices already present in that source and checking the rest of the repository for other copied material.

This is a preliminary compatibility assessment, not a formal legal opinion. A license selection should still be reviewed against the full source history, file-level notices, all contributors and any third-party code embedded in source files.

## 5. Required work before a final dependency notice is published

- [ ] Generate a fully resolved dependency report for Python 3.10 and 3.12 on the target Linux platform (including extras and environment markers).
- [ ] Record package name, exact version, SPDX license expression, source URL and whether it is runtime or test-only.
- [ ] Verify the actual resolved versions of `certifi`, `Pillow`, `APScheduler`, `tzlocal`, `cffi`, `pycparser`, `anyio`, `httpcore`, `h11`, `idna`, `pluggy`, `iniconfig`, `packaging` and `Pygments`.
- [ ] Check the license text shipped inside each resolved wheel/sdist, not just the package's current homepage metadata.
- [ ] Decide whether `python-dotenv` and `qrcode[pil]` are actually needed; inspect the QR helper reference before removing or changing either requirement.
- [ ] Split runtime and development/test requirements so production installation does not install pytest unnecessarily.
- [ ] Add a generated third-party notices file if the project begins distributing bundled artifacts. For source-only publication, keep this dependency inventory and upstream notices easy to find.
- [ ] Re-run the inventory whenever dependencies or version pins change.

## 6. References

- [python-telegram-bot 22.8 on PyPI — LGPL-3.0-only and application-use clarification](https://pypi.org/project/python-telegram-bot/22.8/)
- [python-dotenv on PyPI — BSD-3-Clause metadata](https://pypi.org/project/python-dotenv/)
- [cryptography 43.0.3 on PyPI — Apache-2.0 OR BSD-3-Clause](https://pypi.org/project/cryptography/43.0.3/)
- [httpx 0.28.1 on PyPI — BSD-3-Clause](https://pypi.org/project/httpx/0.28.1/)
- [qrcode 8.2 on PyPI — BSD metadata and Pillow extra](https://pypi.org/project/qrcode/8.2/)
- [pytest license documentation — MIT](https://pytest.org/en/8.0.x/license.html)
- [Mozilla MPL 2.0 FAQ — official distribution and file-level copyleft guidance](https://www.mozilla.org/en-US/MPL/2.0/FAQ/)
- [Python Packaging User Guide — SPDX expressions and combined dependency licenses](https://packaging.python.org/en/latest/guides/licensing-examples-and-user-scenarios/)

---
*Last reviewed: 2026-11-12. Revalidate against the exact dependency graph before release.*
