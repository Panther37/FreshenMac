# Security Policy

Security is a top priority for **FreshenMac**. Because FreshenMac coordinates macOS system maintenance, interacts with administrative privileges (`sudo`), schedules background tasks (`launchd`), and manages system restarts (`fdesetup authrestart`), we take the security and integrity of this project very seriously.

---

## Supported Versions

Only the latest active minor release series receives security updates and patches:

| Version | Supported          |
| :------ | :----------------- |
| `1.0.x` | :white_check_mark: |
| `< 1.0` | :x:                |

We strongly encourage all users to run the latest version of FreshenMac.

---

## Reporting a Vulnerability

If you discover a potential security vulnerability in FreshenMac, please report it **responsibly and privately**. Please do **not** open a public GitHub issue, pull request, or public discussion.

### Preferred Method: GitHub Security Advisory

Report the vulnerability through GitHub's Private Vulnerability Reporting:
1. Navigate to the [FreshenMac Security Advisories](https://github.com/Panther37/FreshenMac/security/advisories) page.
2. Click **"Report a vulnerability"** to submit your findings privately.

### Alternative Method: Email

If you cannot use GitHub Security Advisories, send an email directly to the maintainer:
- **Contact:** Matt Logan
- **Email:** [matt@panther37.com](mailto:matt@panther37.com)
- **Subject:** `[SECURITY] FreshenMac Vulnerability Report - <Brief Description>`

### What to Include in Your Report

To help us triage and resolve the issue quickly, please provide as much information as possible:

1. **Description:** A detailed explanation of the vulnerability and its potential impact.
2. **Environment:**
   - macOS version (e.g., macOS 14 Sonoma, macOS 15 Sequoia).
   - Hardware architecture (Apple Silicon `arm64` or Intel `x86_64`).
   - Python version (`python3 --version`).
   - FreshenMac version (`src/freshenmac/config.py`).
3. **Reproduction Steps:** Clear, step-by-step instructions or a minimal Proof of Concept (PoC) demonstrating the issue.
4. **Impact Assessment:** What an attacker could achieve if the vulnerability is exploited (e.g., unauthorized privilege escalation, arbitrary code execution, denial of service).
5. **Mitigation / Proposed Fix:** Any patches, mitigations, or workarounds you have identified (optional).

---

## Our Response Process

When a security vulnerability is reported, we follow this coordinated disclosure workflow:

1. **Acknowledgment:** We will acknowledge receipt of your report within **48 hours**.
2. **Assessment & Triage:** We will investigate, reproduce the issue, and confirm its severity within **5 business days**.
3. **Patch Development:** We will prepare and validate a fix in a private branch.
4. **Coordinated Disclosure:** We will coordinate with you on a release date and publish a security advisory alongside the updated release.
5. **Credit:** With your permission, we will publicly credit you in the advisory and release notes.

We ask that you give us a reasonable amount of time to address the vulnerability before disclosing it publicly.

---

## Security Architecture & Design Principles

FreshenMac is built around core design choices intended to minimize attack surfaces:

- **Zero External Dependencies:** FreshenMac runs strictly on the Python 3.9+ standard library. It does not install or depend on third-party `pip` packages, eliminating supply chain compromise vectors.
- **Upstream Signature Verification:** FreshenMac acts as an orchestrator for official macOS CLI utilities (`softwareupdate`, `fdesetup`, `xcode-select`, `mas`) and Homebrew (`brew`). It relies entirely on native signature verification, checksums, and Apple Gatekeeper rather than downloading third-party binaries directly.
- **Privilege Scoping:** Privileged commands requiring root execution are clearly delineated. Dedicated sudoers drop-in files (`/etc/sudoers.d/freshenmac`) are validated and cleanly removable via `--uninstall`.
- **Hermetic Testing:** Automated test suites enforce architecture safety guards, preventing unintended command execution or shutdown actions during development and continuous integration.
