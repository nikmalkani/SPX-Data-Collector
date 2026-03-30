# Public Repo Checklist

Use this checklist before changing the GitHub repository visibility to public.

## Secrets

- Confirm `.env` is not tracked: `git ls-files .env`
- Confirm database files are not tracked: `git ls-files '*.db' '*.sqlite'`
- Confirm key and cert files are not tracked: `git ls-files '*.pem' '*.key' '*.crt' '*.p12' '*.pfx'`
- Confirm tracked files do not contain real secrets: run the repo secret scan workflow or `gitleaks` locally
- Rotate any credential that may have been pasted into Git history, logs, screenshots, or PR comments

## Deployment Details

- Keep public docs sanitized and generic
- Do not commit real hostnames, IP addresses, usernames, SSH commands, or private key paths
- Keep production-only notes in a private document or private repo

## App Surface

- Keep `.env` permission checks enabled in prod entrypoints
- Keep public app bound to `127.0.0.1` behind the reverse proxy
- Keep `/api/health` minimal
- Do not expose raw SQL execution or admin/debug endpoints publicly

## GitHub Settings

- Turn on secret scanning
- Turn on push protection
- Require pull requests before merging to `main`
- Restrict GitHub Actions permissions to the minimum needed

## Final Check

- Review `git diff --cached`
- Push the branch
- Merge to `main`
- Change repository visibility to public
