#!/bin/zsh -l
# Read-only check of the Python / Tkinter / svtplay-dl / ffmpeg setup on this Mac.
# Installs nothing. Writes results to setup-check.log next to this file.
cd "$(dirname "$0")"
LOG="$PWD/setup-check.log"
[ -f ~/.zshrc ] && source ~/.zshrc >/dev/null 2>&1
{
echo "=== date: $(date)"
echo "=== macOS: $(sw_vers -productVersion) $(uname -m)"
echo "=== shell PATH:"; echo "$PATH" | tr ':' '\n'
echo "=== VIRTUAL_ENV: ${VIRTUAL_ENV:-none}"
echo "=== brew: $(command -v brew) $(brew --prefix 2>/dev/null)"
echo "=== brew python formulas:"; brew list --formula 2>/dev/null | grep -E '^(python|tcl-tk|svtplay|ffmpeg|pyenv)'
echo "=== which -a python3:"; which -a python3
echo "=== pyenv: $(command -v pyenv) $(pyenv version 2>/dev/null)"
echo "=== Tkinter test for each python found:"
for p in $(which -a python3 python3.9 python3.10 python3.11 python3.12 python3.13 python3.14 2>/dev/null | awk '!seen[$0]++') /usr/bin/python3; do
  [ -x "$p" ] || continue
  printf '%s -> ' "$p"
  "$p" -c 'import sys,tkinter; print(sys.version.split()[0], "Tk", tkinter.TkVersion, "prefix", sys.prefix)' 2>&1 | tail -1
done
echo "=== svtplay-dl:"; which -a svtplay-dl
SVT=$(command -v svtplay-dl)
if [ -n "$SVT" ]; then
  echo "first line: $(head -1 "$SVT" 2>/dev/null | cut -c1-200)"
  echo "resolved: $(readlink -f "$SVT" 2>/dev/null || python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$SVT")"
  echo "version: $(svtplay-dl --version 2>&1 | tail -1)"
fi
echo "=== ffmpeg:"; which -a ffmpeg; ffmpeg -version 2>&1 | head -1
echo "=== venv folders near repos:"; ls -d ~/GitRepos/*/{venv,.venv,env} ~/{venv,.venv} 2>/dev/null
echo "=== done"
} > "$LOG" 2>&1
cat "$LOG"
echo
echo "Saved to $LOG — you can close this window."
