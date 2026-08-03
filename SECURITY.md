# Security policy

Citefold handles personal memory and source media, so confidentiality and
deletion behavior are part of the security boundary.

## Supported versions

| Version | Security updates |
| --- | --- |
| `0.1.x` | Yes |
| `< 0.1` | No |

Only the latest patch release in a supported minor series receives security
fixes.

## Reporting a vulnerability

Use GitHub's
[private vulnerability reporting](https://github.com/VIAIM/citefold/security/advisories/new)
for suspected vulnerabilities. Do not put vulnerability details, credentials,
personal data, or private memory in a public issue. Include:

- affected version and operating system;
- minimal reproduction steps or a proof of concept;
- expected impact and the scope boundary crossed;
- any suggested mitigation;
- whether the report contains sensitive data.

After the private report is received, maintainers aim to acknowledge it within
five business days, provide an initial assessment within ten business days,
and coordinate disclosure after a fix is available. These are response
targets, not a service-level agreement.

## Security boundaries

- Treat all imported media, transcripts, and model output as untrusted input.
- Keep API keys in environment variables or a secret manager, never in a
  memory store or repository.
- Scope isolation prevents accidental cross-tenant retrieval through public
  APIs; Citefold is not a sandbox against malicious code running in the same
  Python process or with filesystem access.
- Local filesystem locking is intended for supported Linux and macOS filesystems.
  Network filesystems and Windows process locking are not currently supported.
- Citefold enforces restrictive POSIX modes for its local state: scope
  directories are private to the owner and sensitive files are owner-readable
  and owner-writable. This is defense in depth, not encryption at rest.
- Users remain responsible for host access, encryption at rest, backups,
  retention policy, and provider data-handling settings.

The project does not currently operate a bug-bounty program.
