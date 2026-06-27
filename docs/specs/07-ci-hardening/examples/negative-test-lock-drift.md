# ILLUSTRATIVE — the lock-drift negative test (proves AC #1)

The single most important verification: confirm CI **fails red** on lock drift.
A green run here would mean `--frozen` semantics leaked back in (a false pass).

## The throwaway PR

Edit `pyproject.toml` to add a dependency **without** re-locking:

```diff
 dependencies = [
     "openai>=1.40",
     "psycopg[binary]>=3.2",
     "redis>=5.0",
     "langgraph>=0.2.50",
     "opentelemetry-sdk>=1.27",
     "opentelemetry-exporter-otlp-proto-http>=1.27",
     "python-dotenv>=1.0",
+    "tenacity>=9.0",          # intentionally NOT added to uv.lock
 ]
```

Commit `pyproject.toml` only (leave `uv.lock` untouched) and open the PR.

## Expected result

`lint` fails at the **`uv lock --check`** step (before the slower `uv sync`),
with output naming the drift, e.g.:

```
error: The lockfile at `uv.lock` needs to be updated ...
       run `uv lock` to update it
```

## Reproduce the assertion locally (same as CI, no services needed)

```sh
# from repo root, on the throwaway branch
uv lock --check        # -> non-zero exit, "needs to be updated"
echo "exit=$?"         # must be non-zero
```

## Confirm the OLD behaviour would have masked it (one-time proof)

```sh
uv sync --frozen       # exits 0 — installs the stale lock silently == false pass
uv sync --locked       # exits non-zero — the behaviour this spec mandates
```

## Cleanup

Close the PR without merging (or `uv lock` to make it pass, then close). This is
a verification artifact, not a change to land.
