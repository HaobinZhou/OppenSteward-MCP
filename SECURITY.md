# Security

The authoritative exposure and authentication rules are the registered
[README contracts](README.md#gpt-能看到哪些文件). This server is intended for
one local owner. Governance document bodies can contain sensitive information;
the file allowlist does not redact their contents.

For a suspected authorization bypass or unintended file disclosure, use the
repository's private vulnerability reporting feature if enabled. If no private
reporting channel is available, open an issue requesting a private channel
without posting exploit details, private documents or credentials. There is no
guaranteed response-time commitment.

Use synthetic fixtures for a report. Include the affected commit, OS, transport
and the smallest reproduction. Do not attach `.env`, `config.local.json`,
`.runtime`, tunnel profiles with secrets, or live OAuth messages. If a deployed
credential is exposed, revoke it locally and rotate the appropriate credential
through its owner (this server's OAuth or OpenAI's Tunnel runtime key).

Windows and Linux branches have not been verified on those platforms. Reports
about their native path, ACL or process behavior are welcome; macOS test results
do not establish their security properties.
