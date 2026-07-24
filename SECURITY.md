# Security Policy

## Supported version

This repository is a research prototype. Security fixes are applied to the
latest version on the default branch.

## Reporting a vulnerability

Do not publish credentials, private research data, or a working exploit in a
public issue. Contact the maintainer privately and include the affected
component, reproduction steps, possible impact, and a suggested mitigation if
available.

## Safe deployment checklist

- Copy `deepdiver_v2/config/.env.example` to
  `deepdiver_v2/config/.env`; never commit the resulting `.env`.
- Use a unique, long random value for `SECRET_KEY`.
- Create a dedicated MySQL user with access only to the SciAssistant database.
- Keep MCP, Flask, and task-service ports behind a firewall or reverse proxy.
- Apply upload size limits and periodically remove expired workspaces.
- Do not use production API keys in demonstrations, screenshots, or logs.
- Treat uploaded manuscripts, experimental archives, and generated reports as
  private research data.
- Rotate a key immediately if it has ever been committed to Git history.

The public repository intentionally excludes local uploads, workspaces, logs,
generated papers, database credentials, and model-provider credentials.
