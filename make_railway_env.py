"""Build .env.railway - the variables to paste into Railway's "Raw Editor".

Run on the owner's PC:   python make_railway_env.py
It copies the local .env, drops what Railway provides itself (the public
domain, the port) and points the database and the connection store at the
persistent volume mounted at /data. The file is git-ignored and never leaves
this PC except through the Railway dashboard, pasted by the owner.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, ".env")
DST = os.path.join(HERE, ".env.railway")
DROP = {"PUBLIC_BASE_URL", "PORT", "OLLAMA_URL", "OLLAMA_MODEL"}
OVERRIDE = {
    "DB_PATH": "/data/cartrends.db",
    "CONNECTION_FILE": "/data/instagram_connection.json",
    "CARTRENDS_MAINTENANCE": "1",
    "PYTHONUNBUFFERED": "1",
}


def main() -> int:
    if not os.path.exists(SRC):
        print("no .env found next to this script")
        return 1
    values = {}
    with open(SRC, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key or key in DROP or value.upper() in ("FILL-IN", "FILL_IN", "CHANGEME", "TODO", ""):
                continue
            values[key] = value
    values.update(OVERRIDE)
    missing = [k for k in ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_APP_SECRET", "VERIFY_TOKEN", "DASHBOARD_PASSWORD")
               if k not in values]
    with open(DST, "w", encoding="utf-8", newline="\n") as fh:
        for key in sorted(values):
            fh.write(f"{key}={values[key]}\n")
    print(f"wrote {DST} with {len(values)} variables: {', '.join(sorted(values))}")
    if missing:
        print("WARNING missing:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
