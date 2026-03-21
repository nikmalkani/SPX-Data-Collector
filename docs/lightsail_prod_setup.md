# Lightsail Prod Setup

This repo is set up to run `marketplayground.io` from a single Lightsail instance using:

- `spx-collector.service` for data collection
- `spx-backtest-prod.service` for the public UI on `127.0.0.1:8789`
- Caddy on ports `80/443`
- the same local SQLite file for both collector and UI

## Assumed Server Paths

- Repo: `/home/ubuntu/SPX-Data-Collector`
- Venv: `/home/ubuntu/SPX-Data-Collector/.venv`
- DB file: `/home/ubuntu/SPX-Data-Collector/spx_options.db`

The `.env` file should use an absolute SQLite path:

```env
DB_URL=sqlite:////home/ubuntu/SPX-Data-Collector/spx_options.db
```

## 1. DNS

Point the DNS `A` record for `marketplayground.io` at the Lightsail static IP.

If you want `www`, point it too and let Caddy redirect it to the root domain.

## 2. Install Caddy

```bash
sudo apt-get update
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
```

## 3. Install service files

```bash
sudo cp deploy/systemd/spx-backtest-prod.service /etc/systemd/system/
sudo cp deploy/systemd/spx-sqlite-backup.service /etc/systemd/system/
sudo cp deploy/systemd/spx-sqlite-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spx-backtest-prod.service
sudo systemctl enable --now spx-sqlite-backup.timer
```

## 4. Install Caddy config

```bash
sudo cp deploy/caddy/marketplayground.io.Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

If DNS is not pointed yet, Caddy will keep retrying certificate issuance until the domain resolves to the instance.

## 5. Firewall

Open only:

- `22`
- `80`
- `443`

Do not open `8789` publicly. The prod app should stay on loopback only.

## 6. Functional checks

Local:

```bash
curl http://127.0.0.1:8789/api/health
curl -I http://127.0.0.1:8789/
```

Public:

```bash
curl -I http://marketplayground.io
curl -I https://marketplayground.io
```

## 7. Logs

```bash
journalctl -u spx-collector -n 100 --no-pager
journalctl -u spx-backtest-prod -n 100 --no-pager
journalctl -u caddy -n 100 --no-pager
journalctl -u spx-sqlite-backup -n 100 --no-pager
```
