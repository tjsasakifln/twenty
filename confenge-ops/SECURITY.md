# Security notes

Report vulnerabilities privately through the repository owner's GitHub contact
channel. Do not include credentials, customer data, or production evidence in a
public issue.

The examples in this directory require host-generated secrets. Never commit a
populated `.env`, Google credential JSON, Twenty application keys, TLS private
keys, DNS tokens, database dumps, or snapshots. The public tests are designed to
run without network access or production credentials.
