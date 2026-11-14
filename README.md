# stem-to-master

Mix and master a song from its aligned stems, with signal processing only.

## Install

Requirements: a Mac with Apple Silicon, and an internet connection for the first run.

```sh
./setup.sh
```

It installs:

- [mise](https://mise.jdx.dev) into `~/.local/bin`, if it isn't installed yet
- Python and uv, at the versions in `mise.toml`, into mise's folder (`~/.local/share/mise`)
- the Python packages in `uv.lock`, into `.venv/`
- the plugins in `plugins.lock`, into `plugins/`, each checked against its SHA-256

Running it again only installs what is missing.

Then run commands through mise, which puts that Python and uv on the path:

```sh
mise exec -- uv run tools/run.py songs/<song>
```

If setup installed mise, it is `~/.local/bin/mise` until `~/.local/bin` is on your `PATH`.
