# PICARD Terms of Use

This document governs use of the PICARD software. By running PICARD,
accepting these terms at the command line, or setting `PICARD_ACCEPT_TERMS=1`,
you agree to the terms below.

## What PICARD Is

PICARD is an experimental computer science research project. It is not a
product, a commercial service, or a production storage system. It has
dual-use capabilities: the same techniques that enable resilient personal
storage can, in principle, be misused. Publication serves the research
community's interest in understanding protocol storage surfaces, not in
enabling unauthorized access.

## Acceptable Use

You may use PICARD for:

- Personal or organizational storage on infrastructure you own or control.
- Academic research, security research, and protocol research on systems you
  own or have explicit written authorization to test.
- Educational demonstration and proof-of-concept work in isolated environments.

## Prohibited Use

Any use of PICARD that violates applicable law, computer fraud and abuse
statutes, electronic communications privacy law, or the Terms of Service of
any platform or network immediately and irrevocably voids acceptable use
under this project.

This includes but is not limited to:

- Accessing systems, accounts, or networks without authorization.
- Storing or transmitting data in violation of a platform's Terms of Service
  using accounts or infrastructure you do not own.
- Circumventing security controls on systems you do not own or have
  authorization to test.
- Any use prohibited by the Computer Fraud and Abuse Act (US), the Computer
  Misuse Act (UK), section 202c StGB (Germany), or equivalent statute in
  your jurisdiction.

If you use PICARD to break the law, you are entirely on your own. The authors
provide no support, defense, or indemnification for unlawful or unauthorized use.

## Per-Adapter Notices

Each Tier 2 and Tier 3 adapter includes a platform-specific Terms of Service
or regulatory notice in its source file. Read it before use. Ignorance of a
notice is not a defense.

## Indemnification

By using PICARD you accept full responsibility for your actions and their
consequences. You agree to indemnify, defend, and hold harmless the authors,
contributors, and maintainers of PICARD from and against any and all claims,
liabilities, damages, losses, costs, and expenses (including reasonable
attorneys' fees) arising out of or related to your use or misuse of this
software.

## Disclaimer of Warranties

PICARD is provided "as is," without warranty of any kind, express or implied.
The authors make no representations about suitability, fitness for a particular
purpose, or correctness. Use at your own risk.

## Accepting These Terms

On first run, PICARD will prompt you to accept these terms. Three ways to
proceed:

1. **First-run flag:** `picard --accept-terms <command>`
   Writes a timestamped acceptance record to `~/.picard/terms_accepted` and
   continues. You will not be prompted again on that machine.

2. **Environment variable:** `PICARD_ACCEPT_TERMS=1 picard <command>`
   Accepts for this invocation only. Suitable for CI and scripted use.

3. **Already accepted:** Once `~/.picard/terms_accepted` exists, all
   subsequent invocations proceed without prompting.

## Contact

Christopher Neitzert
https://github.com/neitzert/picard/issues
