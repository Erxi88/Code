#!/usr/bin/env bash
# Einrichtung auf Ubuntu (22.04/24.04): Python 3.12, venv mit Abhängigkeiten, Ollama und Modell.
#
#   ./setup.sh                 # alles, Modell aus config/settings.yaml (llm.modell)
#   ./setup.sh --modell qwen3.6:27b
#   ./setup.sh --ohne-ollama   # nur Python-Teil (z. B. wenn Ollama auf einem anderen Server läuft)
set -euo pipefail

cd "$(dirname "$0")"
MODELL=""
MIT_OLLAMA=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --modell) MODELL="$2"; shift 2 ;;
    --ohne-ollama) MIT_OLLAMA=0; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

echo "==> Systempakete"
$SUDO apt-get update -y
$SUDO apt-get install -y curl ca-certificates software-properties-common
if ! command -v python3.12 >/dev/null 2>&1; then
  # Ubuntu 22.04 bringt kein Python 3.12 mit
  $SUDO add-apt-repository -y ppa:deadsnakes/ppa
  $SUDO apt-get update -y
fi
$SUDO apt-get install -y python3.12 python3.12-venv

echo "==> Python-Umgebung (.venv)"
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
mkdir -p data
[[ -f .env ]] || cp .env.example .env

if [[ $MIT_OLLAMA -eq 1 ]]; then
  echo "==> Ollama"
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  # Dienst starten, falls systemd fehlt (z. B. Container)
  if ! curl -fsS http://localhost:11434/api/version >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
      $SUDO systemctl enable --now ollama
    else
      nohup ollama serve >/tmp/ollama.log 2>&1 &
    fi
    for _ in $(seq 1 30); do
      curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  ollama --version

  if [[ -z "$MODELL" ]]; then
    MODELL=$(.venv/bin/python -c "from recherche.config import load_settings; print(load_settings().llm.modell)")
  fi
  echo "==> Modell $MODELL laden"
  ollama pull "$MODELL"
fi

echo "==> Tests"
.venv/bin/pytest

cat <<TXT

Fertig. Aktivieren mit:  source .venv/bin/activate
Pilot:                   recherche run --bundesland SH --orte Kiel,Husum
Suchvergleich:           recherche search-compare --orte Kiel,Flensburg,Lübeck,Neumünster,Husum
TXT
