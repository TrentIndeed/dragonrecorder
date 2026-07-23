"""Deploy DragonRecorder's server to the Hetzner box, following the same
pattern as meshToParametric/deploy.py: git clone/pull into /opt, write .env,
docker compose up --build, append a Caddy site block, reload Caddy.

Usage:
    python deploy.py --ip 5.161.100.230                 # serve on http://<ip>
    python deploy.py --ip 5.161.100.230 --domain rec.example.com

First run generates CAPTURE_TOKEN and a dashboard password and prints both —
put the token in the client's .env. Later runs keep the box's existing .env.
"""

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

# docker build output contains unicode the Windows console codepage can't map
sys.stdout.reconfigure(errors="replace")
sys.stderr.reconfigure(errors="replace")

REPO_URL = "https://github.com/TrentIndeed/dragonrecorder.git"
REMOTE_DIR = "/opt/dragonrecorder"
SSH_KEY = os.environ.get("SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_ed25519"))


class SshResult:
    def __init__(self, r: subprocess.CompletedProcess):
        self.returncode = r.returncode
        self.stdout = r.stdout.decode(errors="replace")
        self.stderr = r.stderr.decode(errors="replace")


def ssh(ip: str, script: str, timeout: int = 600) -> SshResult:
    # bytes, not text mode: Windows text mode would rewrite \n as \r\n and
    # remote bash chokes on the carriage returns
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
         "-i", SSH_KEY, f"root@{ip}", "bash", "-s"],
        input=script.replace("\r\n", "\n").encode(),
        capture_output=True, timeout=timeout)
    return SshResult(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="5.161.100.230")
    ap.add_argument("--domain", default="",
                    help="site domain for Caddy auto-HTTPS; omit to serve on http://<ip>")
    ap.add_argument("--branch", default=os.environ.get("DEPLOY_BRANCH", "main"))
    args = ap.parse_args()

    site = args.domain or f"http://{args.ip}"
    public_url = f"https://{args.domain}" if args.domain else f"http://{args.ip}"
    capture_token = secrets.token_urlsafe(32)
    dash_password = secrets.token_urlsafe(12)

    # secrets that only exist locally get forwarded on first deploy
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    script = f"""
set -e
if [ -d {REMOTE_DIR}/.git ]; then
    cd {REMOTE_DIR} && git fetch origin && git checkout {args.branch} && git pull origin {args.branch}
else
    cd /opt && git clone -b {args.branch} {REPO_URL}
fi
cd {REMOTE_DIR}
mkdir -p data

if [ ! -f .env ]; then
cat > .env << 'ENVEOF'
CAPTURE_TOKEN={capture_token}
SERVER_URL={public_url}
MIN_FREE_GB=3
RETENTION_DAYS=14
TELEGRAM_BOT_TOKEN={tg_token}
TELEGRAM_CHAT_ID={tg_chat}
ENVEOF
echo "WROTE_NEW_ENV"
fi

docker compose up -d --build

if ! grep -q 'dragonrecorder' /etc/caddy/Caddyfile 2>/dev/null; then
    HASH=$(caddy hash-password --plaintext '{dash_password}')
    cat >> /etc/caddy/Caddyfile << CADDYEOF

# dragonrecorder
{site} {{
    handle_path /media/* {{
        root * {REMOTE_DIR}/data
        file_server
    }}
    @dash path /dash /dash/* /api/dash/*
    basic_auth @dash {{
        trenton $HASH
    }}
    reverse_proxy localhost:8082
}}
CADDYEOF
    systemctl reload caddy
    echo "WROTE_CADDY_BLOCK"
fi

sleep 3
curl -sf http://127.0.0.1:8082/healthz && echo && echo "HEALTH_OK"
"""
    print(f"Deploying to root@{args.ip} ({site}) ...")
    r = ssh(args.ip, script)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr[-2000:] if r.returncode else "")
    if r.returncode != 0:
        print(f"DEPLOY FAILED (ssh exit {r.returncode})")
        return 1
    if "WROTE_NEW_ENV" in r.stdout:
        print(f"\nNew server credentials (save these now):")
        print(f"  CAPTURE_TOKEN={capture_token}   <- put in client .env")
        print(f"  dashboard login: trenton / {dash_password}")
    if "HEALTH_OK" not in r.stdout:
        print("WARNING: healthz did not answer — check `docker compose logs` on the box")
        return 1
    print(f"\nDeployed. Player at {public_url}/w/<slug>, dashboard at {public_url}/dash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
