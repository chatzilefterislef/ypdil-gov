#!/usr/bin/env python3
"""
Εκκίνηση της εφαρμογής με μία εντολή, σε macOS/Linux και Windows:

    python3 run.py        (macOS / Linux)
    python run.py         (Windows)

Φτιάχνει venv, εγκαθιστά πακέτα, και μετά ξαναεκτελείται μέσα στο venv για να
ανεβάσει τον server. Ίδιο μοτίβο με το gov-doc-fetcher / opske-aitisi — ένα
σημείο εκκίνησης, χωρίς απόλυτες διαδρομές.

Σημαίες:
  --no-open    μη ανοίξεις browser (το Claude Code ανοίγει μόνο του preview)
  --port 8020  άλλη πόρτα (προεπιλογή 8020)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def inside_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def run(cmd: list[str], what: str) -> None:
    print(f"\n▶ {what}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(f"❌ Απέτυχε: {what}\n   Εντολή: {' '.join(map(str, cmd))}")


def bootstrap() -> None:
    if sys.version_info < (3, 9):
        sys.exit(f"❌ Χρειάζεται Python 3.9+ (τρέχει {sys.version.split()[0]})")

    if not venv_python().exists():
        run([sys.executable, "-m", "venv", str(VENV)], "Δημιουργία virtual environment")
    else:
        print("✓ Το virtual environment υπάρχει", flush=True)

    py = str(venv_python())
    run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], "Ενημέρωση pip")
    run([py, "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt")],
        "Εγκατάσταση πακέτων")

    print("\n✅ Όλα έτοιμα — εκκίνηση…", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(py, [py, str(Path(__file__).resolve()), *sys.argv[1:]])


def lan_ip() -> str:
    """Η τοπική IP αυτού του υπολογιστή στο δίκτυο (όχι 127.0.0.1) -- για να
    δείξουμε στον χρήστη ποια διεύθυνση να μοιραστεί με συναδέλφους."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # δεν στέλνει πακέτα, μόνο διαλέγει interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def serve(port: int, open_browser: bool, lan: bool) -> None:
    import uvicorn   # διαθέσιμο μόνο μέσα στο venv

    host = "0.0.0.0" if lan else "127.0.0.1"
    url = f"http://localhost:{port}"
    if open_browser:
        def opener() -> None:
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=opener, daemon=True).start()

    print(f"\n🌐 Η εφαρμογή τρέχει: {url}")
    if lan:
        print(f"   Και στο δίκτυο: http://{lan_ip()}:{port}  (ΧΩΡΙΣ κωδικό πρόσβασης — προσβάσιμο σε όλους στο ίδιο δίκτυο)")
    print("   (Ctrl+C για διακοπή)\n")
    uvicorn.run("main:app", host=host, port=port, log_level="info")


def main() -> None:
    ap = argparse.ArgumentParser(description="Εκκίνηση της εφαρμογής υπεύθυνων δηλώσεων")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--no-open", action="store_true",
                    help="μη ανοίξεις browser (για preview μέσα από Claude Code)")
    ap.add_argument("--lan", action="store_true",
                    help="άκουσε σε όλο το δίκτυο (0.0.0.0) -- ΧΩΡΙΣ κωδικό, προσβάσιμο σε όλους στο ίδιο δίκτυο")
    args = ap.parse_args()

    os.chdir(ROOT)          # τα doc_templates/ και templates/ είναι σχετικά με τη ρίζα
    if not inside_venv():
        bootstrap()         # δεν επιστρέφει — κάνει execv
    serve(args.port, not args.no_open, args.lan)


if __name__ == "__main__":
    main()
