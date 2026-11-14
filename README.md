# stem-to-master

Mix and master a song from its aligned stems, with signal processing only.

## Install

Requirements: a Mac with Apple Silicon, and an internet connection for the first run.

```sh
./setup.sh
```

It installs:

- [mise](https://mise.jdx.dev) into `~/.local/bin`, if it isn't installed yet
- Python and uv, at the versions in `mise.toml`, through mise
- the Python packages in `uv.lock`, into `.venv/`
- the plugins in `plugins.lock`, into `plugins/`, each checked against its SHA-256

Running it again only installs what is missing.
