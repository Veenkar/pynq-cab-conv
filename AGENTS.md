# AGENTS.md

Instructions for coding agents live in [CLAUDE.md](CLAUDE.md); read it first.

Key rule for Python on the PC: use the repo venv `.venv`, created from the Python on PATH with
`setup_venv.bat` (Windows) or `./setup_venv.sh` (Linux/macOS), `--reinstall` to rebuild it.
Dependencies go in `requirements.txt`. Run scripts with `.venv/Scripts/python.exe` /
`.venv/bin/python`; repo scripts re-exec into `.venv` when it exists (see `sim/run_sim.py`).
Don't install packages into the global interpreter, pyenv versions or conda.
