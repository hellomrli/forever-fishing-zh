set shell := ["bash", "-cu"]

default:
    @just --list

sync:
    uv sync --locked --group dev

lint:
    uv run ruff check .
    uv run ruff format --check .

fmt:
    uv run ruff check --fix .
    uv run ruff format .

test:
    uv run pytest

build:
    uv run pyinstaller --clean --noconfirm packaging/wowfish.spec

# Native Windows builds also exercise screen capture, minimize/restore and global hotkeys.
smoke:
    uv run python -m wowfish.gui.launcher --smoke-test checks/gui.json

rust-lint:
    cargo fmt --all -- --check
    cargo clippy --workspace --all-targets --locked -- -D warnings

rust-test:
    cargo test --workspace --locked

rust-build:
    cargo build --workspace --release --locked

rust-gui:
    cargo run --locked --bin ForeverFishing-Rust

rust-smoke:
    cargo run --locked --bin ForeverFishing-Rust -- --smoke-test checks/rust-gui.json

upstream-check:
    uv run python scripts/check_upstream.py --fail-on-changes
