# Nginx reverse proxy config

Copy of the live server's `/etc/nginx/sites-available/nova-api`,
including the HTTPS blocks added by certbot. Kept in the repo so the
server config can be rebuilt from a clone — it is not applied
automatically by the compose stack.

## Restore on a fresh server

1. `apt install nginx certbot python3-certbot-nginx`
2. Copy this file to `/etc/nginx/sites-available/nova-api`
   (temporarily remove the certbot-managed lines if certs don't exist yet)
3. `ln -s /etc/nginx/sites-available/nova-api /etc/nginx/sites-enabled/`
4. `nginx -t && systemctl reload nginx`
5. Point DNS at the server, then: `certbot --nginx -d <domain>`
   (re-adds the 443 block and cert paths)

Cert paths reference `/etc/letsencrypt/` — certs themselves are never
committed. API traffic proxies to `127.0.0.1:8000`; the app port is
bound to loopback only.
